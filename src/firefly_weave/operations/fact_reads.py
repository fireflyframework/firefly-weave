# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Scoped, bounded operational metadata reads; execution remains runtime-owned."""

import asyncio
import heapq
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from firefly_weave.compiler.ir import accepted_ir_versions
from firefly_weave.contracts import language_features
from firefly_weave.contracts.instance_keys import split_instance
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import (
    RunListQuery,
    RunStepQuery,
    RunSummary,
    RunSummaryQuery,
    StepFact,
    StepFactWithOutput,
    StepKind,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.fact_positions import RunPosition, StepPosition
from firefly_weave.operations.facts import FactRepository, author_instance
from firefly_weave.operations.redaction import Omission, SafeProjection
from firefly_weave.runtime import admission

_RUN_SCOPE = "f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment"
_RUN_SELECT = (
    "SELECT f.*,coalesce(a.archived,false) AS archived,EXISTS(SELECT 1 FROM run_policy_blocks b "
    "WHERE b.tenant_id=f.tenant_id AND b.project_id=f.project_id AND b.environment_id=f.environment_id "
    "AND b.run_id=f.run_id) AS policy_blocked FROM run_facts f LEFT JOIN run_archives a ON "
    "a.tenant_id=f.tenant_id AND a.project_id=f.project_id AND a.environment_id=f.environment_id "
    "AND a.run_id=f.run_id WHERE " + _RUN_SCOPE
)
_MISSING = (
    "s.tenant_id=:tenant AND s.project_id=:project AND s.environment_id=:environment AND s.run_id=:run "
    "AND left(s.node_id,1)<>'@' AND NOT EXISTS(SELECT 1 FROM step_facts f "
    "WHERE f.tenant_id=s.tenant_id AND f.project_id=s.project_id AND f.environment_id=s.environment_id "
    "AND f.run_id=s.run_id AND (f.instance_key=s.node_id OR (f.instance_key='' AND f.node_id=s.node_id)))"
)
_STEP_ORDER = 'scheduled_at ASC NULLS LAST,node_id COLLATE "C",instance_key COLLATE "C"'
LEGACY_KEYS = 100_000
LEGACY_SECONDS = 2
_KIND: TypeAdapter[StepKind] = TypeAdapter(StepKind)


def unavailable(identifier: UUID) -> UnavailableResource:
    return UnavailableResource(id=identifier, omissions=[Omission(path="", reason="classification_unavailable")])


def available(row: Mapping[str, Any]) -> bool:
    features = row.get("pinned_features")
    return (
        row.get("classification_state") == "available"
        and row.get("classification_policy") == admission.POLICY
        and row.get("policy_blocked") is False
        and isinstance(features, list)
        and len(features) <= 256
        and all(type(value) is str for value in features)
        and len(json.dumps(features, ensure_ascii=False).encode()) <= 16384
        and set(features) <= set(language_features.ADVERTISED_FEATURES)
        and row.get("pinned_ir_version") in accepted_ir_versions(language_features.ADVERTISED_FEATURES)
    )


def milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    return int((end - start).total_seconds() * 1000) if start is not None and end is not None else None


def summary_of(row: Mapping[str, Any]) -> RunSummary | UnavailableResource:
    identifier = row["run_id"]
    if not available(row):
        return unavailable(identifier)
    try:
        return RunSummary.model_validate(
            dict(
                id=identifier,
                workflow=dict(
                    name=row["workflow_name"],
                    version=row["workflow_version"],
                    definition_version_id=row["definition_version_id"],
                ),
                activation_id=row["activation_id"],
                activation=dict(name=row["activation_name"], revision=row["activation_revision"]),
                status=row["status"],
                paused=row["paused"],
                test=row["test"],
                origin=row["origin"],
                caller=(
                    dict(
                        run_id=row["caller_run_id"],
                        node_id=row["caller_node_id"],
                        instance_key=row["caller_instance_key"],
                    )
                    if row["caller_run_id"] is not None
                    else None
                ),
                retried_from_run_id=row["retried_from_run_id"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                ended_at=row["ended_at"],
                duration_ms=milliseconds(row["started_at"], row["ended_at"]),
                business_key=row["business_key"],
                correlation_key=row["correlation_key"],
                failed_step=(
                    dict(
                        node_id=row["failed_node_id"],
                        instance_key=row["failed_instance_key"],
                        error_code=row["failed_error_code"],
                    )
                    if row["failed_node_id"] is not None
                    else None
                ),
                active_incidents=row["active_incidents"],
                handled_errors=row["handled_errors"],
                archived=row["archived"],
            )
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return unavailable(identifier)


def step_of(row: Mapping[str, Any], *, output: SafeProjection | None = None) -> StepFact | StepFactWithOutput:
    try:
        values: dict[str, Any] = {
            key: row.get(key)
            for key in (
                "node_id",
                "instance_key",
                "kind",
                "status",
                "scheduled_at",
                "started_at",
                "ended_at",
                "attempts",
                "worker_id",
                "error_code",
                "handled",
                "child_run_id",
            )
        }
        values.update(
            iteration=list(split_instance(row["instance_key"] or row["node_id"]).indexes),
            duration_ms=milliseconds(row["started_at"], row["ended_at"]),
            queue_wait_ms=milliseconds(row["scheduled_at"], row["started_at"]),
            log_entries=0,
        )
        if output is not None:
            if output.available:
                return StepFactWithOutput(**values, output=output.value)
            values["omissions"] = [
                omission.model_copy(update={"path": "/output" + omission.path}) for omission in output.omissions
            ]
        return StepFact(**values)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise legacy_unavailable() from None


def legacy_unavailable() -> CatalogError:
    return CatalogError(409, "WV-LEGACY-UNAVAILABLE", "Step classification is unavailable")


def step_order(row: Mapping[str, Any]) -> tuple[bool, datetime, str, str]:
    at = row["scheduled_at"]
    return at is None, at or datetime.max.replace(tzinfo=UTC), row["node_id"], row["instance_key"]


@dataclass
class _Candidate:
    order: tuple[bool, datetime, str, str]
    row: dict[str, Any] = field(compare=False)

    def __lt__(self, other: "_Candidate") -> bool:
        return self.order > other.order


class FactReadRepository(FactRepository):
    async def summary_rows(self, query: RunSummaryQuery, position: RunPosition | None) -> list[dict[str, Any]]:
        return await self._run_rows(query, position)

    async def legacy_rows(self, query: RunListQuery, position: RunPosition | UUID | None) -> list[dict[str, Any]]:
        return await self._run_rows(query, position)

    async def _run_rows(self, query: RunSummaryQuery, position: RunPosition | UUID | None) -> list[dict[str, Any]]:
        clauses = ["(:include_archived OR NOT coalesce(a.archived,false))"]
        params: dict[str, Any] = dict(include_archived=query.include_archived, limit=query.limit + 1)
        for name, column in (
            ("workflow", "workflow_name"),
            ("version", "workflow_version"),
            ("origin", "origin"),
            ("caller_run_id", "caller_run_id"),
            ("retried_from_run_id", "retried_from_run_id"),
            ("business_key", "business_key"),
            ("correlation_key", "correlation_key"),
            ("activation_id", "activation_id"),
        ):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.{column}=:{name}")
                params[name] = value
        if query.status:
            clauses.append("f.status=ANY(:statuses)")
            params["statuses"] = query.status
        if not query.include_test:
            clauses.append("NOT f.test")
        if query.top_level_only:
            clauses.append("f.caller_run_id IS NULL")
        for name, column in (("has_active_incident", "active_incidents"), ("has_handled_errors", "handled_errors")):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.{column}>0" if value else f"f.{column}=0")
        for name, operator in (("started_after", ">="), ("started_before", "<")):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.started_at{operator}:{name}")
                params[name] = value
        column, direction, operator = {
            "id": ("run_id", "ASC", ">"),
            "started_desc": ("started_at", "DESC", "<"),
            "started_asc": ("started_at", "ASC", ">"),
            "updated_desc": ("updated_at", "DESC", "<"),
        }[query.order]
        if column == "run_id":
            if position is not None:
                if not isinstance(position, UUID):
                    raise ValueError("ID order requires an ID position")
                clauses.append("f.run_id>:position_id")
                params["position_id"] = position
            order = "f.run_id ASC"
        else:
            if position is not None:
                if not isinstance(position, RunPosition):
                    raise ValueError("Chronological order requires a time position")
                params["position_id"] = position.id
                if position.at is None:
                    clauses.append(f"f.{column} IS NULL AND f.run_id{operator}:position_id")
                else:
                    params["position_at"] = position.at
                    clauses.append(
                        f"((f.{column},f.run_id){operator}(:position_at,:position_id) OR f.{column} IS NULL)"
                    )
            order = f"f.{column} {direction} NULLS LAST,f.run_id {direction}"
        return await self.rows(
            _RUN_SELECT + " AND " + " AND ".join(clauses) + " ORDER BY " + order + " LIMIT :limit", **params
        )

    async def metadata(self, run_id: UUID) -> dict[str, Any]:
        rows = await self.rows(_RUN_SELECT + " AND f.run_id=:run", run=run_id)
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
        row = rows[0]
        if isinstance(summary_of(row), UnavailableResource):
            raise legacy_unavailable()
        kinds = row.get("node_kinds")
        if not isinstance(kinds, dict) or len(kinds) > 10000 or len(json.dumps(kinds).encode()) > 1048576:
            raise legacy_unavailable()
        return row

    async def step_rows(
        self, run_id: UUID, query: RunStepQuery, position: StepPosition | None
    ) -> tuple[list[dict[str, Any]], bool]:
        metadata = await self.metadata(run_id)
        clauses = [_RUN_SCOPE, "f.run_id=:run", "left(f.node_id,1)<>'@'", "left(f.instance_key,1)<>'@'"]
        params: dict[str, Any] = dict(run=run_id, limit=query.limit + 1)
        if query.step is not None:
            clauses.append("f.node_id=:step")
            params["step"] = query.step
        if position is not None:
            params.update(node=position.node_id, instance=position.instance_key)
            identity_predicate = (
                '(f.node_id COLLATE "C",f.instance_key COLLATE "C")>'
                '(cast(:node AS text) COLLATE "C",cast(:instance AS text) COLLATE "C")'
            )
            if position.scheduled_at is None:
                clauses.append("(f.scheduled_at IS NULL AND " + identity_predicate + ")")
            else:
                params["scheduled"] = position.scheduled_at
                clauses.append(
                    "(f.scheduled_at>:scheduled OR f.scheduled_at IS NULL OR (f.scheduled_at=:scheduled AND "
                    + identity_predicate
                    + "))"
                )
        rows = await self.rows(
            "SELECT f.* FROM step_facts f WHERE "
            + " AND ".join(clauses)
            + " ORDER BY "
            + _STEP_ORDER
            + " LIMIT :limit",
            **params,
        )
        for row in rows:
            try:
                identity = author_instance(row["instance_key"] or row["node_id"])
            except ValueError:
                raise legacy_unavailable() from None
            if (
                identity is None
                or identity.node_id != row["node_id"]
                or metadata["node_kinds"].get(row["node_id"]) != row["kind"]
            ):
                raise legacy_unavailable()
        missing = await self.tx.session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM step_instances s WHERE " + _MISSING + ")"), {**self.params, "run": run_id}
        )
        if not missing:
            return rows, True
        historical, complete = await self.historical_steps(run_id, query.step, position, query.limit)
        return sorted([*rows, *historical], key=step_order)[: query.limit + 1], complete

    async def historical_steps(
        self, run_id: UUID, step: str | None, position: StepPosition | None, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        metadata = await self.metadata(run_id)
        boundary = step_order(vars(position)) if position is not None else None
        heap: list[_Candidate] = []
        stream = None
        scanned = 0
        try:
            async with asyncio.timeout(LEGACY_SECONDS):
                stream = await self.tx.session.stream(
                    text(
                        "SELECT s.node_id,s.status FROM step_instances s WHERE "
                        + _MISSING
                        + ' ORDER BY s.node_id COLLATE "C"'
                    ),
                    {**self.params, "run": run_id},
                    execution_options={"yield_per": 500},
                )
                async for batch in stream.mappings().partitions(500):
                    for source in batch:
                        scanned += 1
                        if scanned > LEGACY_KEYS:
                            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Legacy step scan exceeds its bounded budget")
                        identity = author_instance(source["node_id"])
                        if identity is None:
                            continue
                        kind: StepKind = _KIND.validate_python(
                            metadata["node_kinds"].get(identity.node_id), strict=True
                        )
                        if source["status"] not in {"completed", "waiting"}:
                            raise legacy_unavailable()
                        row = dict(
                            node_id=identity.node_id,
                            instance_key=identity.instance_key,
                            kind=kind,
                            status="succeeded" if source["status"] == "completed" else "waiting",
                            scheduled_at=None,
                            started_at=None,
                            ended_at=None,
                            attempts=0,
                            worker_id=None,
                            error_code=None,
                            handled=None,
                            child_run_id=None,
                        )
                        order = step_order(row)
                        if (step is not None and identity.node_id != step) or (
                            boundary is not None and order <= boundary
                        ):
                            continue
                        candidate = _Candidate(order, row)
                        if len(heap) < limit + 1:
                            heapq.heappush(heap, candidate)
                        elif order < heap[0].order:
                            heapq.heapreplace(heap, candidate)
                    await asyncio.sleep(0)
        except TimeoutError:
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Legacy step scan exceeds its bounded budget") from None
        except (ValueError, TypeError, KeyError):
            raise legacy_unavailable() from None
        finally:
            if stream is not None:
                await stream.close()
        return [candidate.row for candidate in sorted(heap, key=lambda value: value.order)], scanned == 0

    async def outputs(self, run_id: UUID, rows: list[dict[str, Any]]) -> dict[str, SafeProjection]:
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.compiler.ir import ActionNode, HumanTaskNode, SignalNode, WaitNode
        from firefly_weave.contracts.definitions import ActionDefinition, load_definition
        from firefly_weave.contracts.values import JsonObject
        from firefly_weave.operations.redaction import project
        from firefly_weave.runtime.kernel import action_schemas, workflow
        from firefly_weave.runtime.repository import RUN_FETCH_BYTES, RuntimeRepository

        await self.metadata(run_id)
        retained = await RuntimeRepository(self.tx).run(run_id)
        admission.require_available(retained)
        ir = workflow(import_artifact(retained["artifact"]))
        bundle = {
            dependency.reference: dependency.document for dependency in ir.dependencies if dependency.kind == "Schema"
        }
        nodes = {node.id: node for node in ir.graph.nodes}
        keys = [row["instance_key"] or row["node_id"] for row in rows]
        withheld = SafeProjection(available=False, omissions=[Omission(path="", reason="classification_unavailable")])
        result = dict.fromkeys(keys, withheld)
        if not keys:
            return result
        predicate = (
            "tenant_id=:tenant AND project_id=:project AND environment_id=:environment AND run_id=:run "
            "AND left(node_id,1)<>'@' AND node_id=ANY(:keys)"
        )
        outputs = await self.rows(
            "WITH sized AS (SELECT node_id,output,sum(octet_length(output::text)) OVER () AS total_bytes "
            "FROM step_instances WHERE " + predicate + ") "
            "SELECT node_id,CASE WHEN total_bytes<=:budget THEN output ELSE NULL END AS output,"
            "total_bytes>:budget AS oversized FROM sized",
            run=run_id,
            keys=keys,
            budget=RUN_FETCH_BYTES,
        )
        if any(output["oversized"] for output in outputs):
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Step outputs exceed the bounded fetch budget")
        for output in outputs:
            identity = author_instance(output["node_id"])
            if identity is None:
                continue
            node = nodes.get(identity.node_id)
            schemas: list[JsonObject] = []
            if isinstance(node, ActionNode):
                definition = load_definition(
                    next(dependency.document for dependency in ir.dependencies if dependency.digest == node.dependency)
                )
                schemas = action_schemas(ir, cast(ActionDefinition, definition), "output", node=node)
            elif isinstance(node, SignalNode):
                schemas = [ir.schemas[node.schema_ref]]
            elif isinstance(node, HumanTaskNode):
                schemas = [{"type": "object", "properties": {"data": node.form_schema, "decision": {"type": "string"}}}]
            elif isinstance(node, WaitNode):
                schemas = [{"type": "null"}]
            elif node is not None:
                schemas = [
                    ir.schemas[guard.schema_ref]
                    for guard in ir.guards
                    if guard.path in {node.path, node.path + "/value"}
                    and guard.purpose in {"transform_output", "decision_output", "branch_output"}
                ]
            if schemas:
                projections = [project(output["output"], schema, bundle) for schema in schemas]
                result[output["node_id"]] = next(
                    (projection for projection in projections if not projection.available), projections[0]
                )
        return result
