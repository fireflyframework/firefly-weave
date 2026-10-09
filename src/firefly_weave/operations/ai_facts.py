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

"""Bounded stored AI evidence read within the caller's repeatable snapshot."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, String, Uuid, bindparam, text
from sqlalchemy.sql.elements import TextClause

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.diagnostics import is_agentic_revision
from firefly_weave.contracts import language_features
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, action_definition, task_capability
from firefly_weave.contracts.catalog import DefinitionKind
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.lumi import LumiConfiguration
from firefly_weave.contracts.values import JsonObject
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.persistence.uow import Transaction

MAX_FACTS = 1000
MAX_BYTES = 8 * 1024 * 1024
SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


class FactLimit(Exception):
    """The complete observation would exceed the request's bounded allocation."""


@dataclass(frozen=True)
class BuiltinAI:
    kind: DefinitionKind
    reference: str
    document: JsonObject
    definition_digest: str


def builtin_ai_definitions() -> tuple[BuiltinAI, ...]:
    result = []
    for value in (AGENTIC_DESCRIPTOR.manifest.value, action_definition()):
        model = load_definition(value)
        document = model.model_dump(by_alias=True)
        result.append(
            BuiltinAI(
                model.kind,
                f"{model.metadata.name}@{model.metadata.version}",
                document,
                FrozenDocument.from_value(document).digest,
            )
        )
    return tuple(result)


def required_ai_capabilities() -> frozenset[str]:
    references = {"weave-agentic.generate@1.0.0"}
    if "ai.agent" in language_features.ADVERTISED_FEATURES:
        references.add("weave-agentic.turn@1.0.0")
    return frozenset(references)


def test_time(payload: dict[str, Any]) -> datetime:
    raw = payload.get("tested_at")
    if not isinstance(raw, str):
        raise ValueError("Test time unavailable")
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Test time unavailable")
    return result


class AIFacts:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = dict(tenant=tx.scope.tenant_id, project=tx.scope.project_id, environment=tx.scope.environment_id)
        self._bytes = 0
        self._releases: list[WorkerRelease] | None = None

    async def _rows(
        self,
        selection: str,
        *,
        values: dict[str, Any] | None = None,
        arrays: tuple[str, ...] = (),
        strings: tuple[str, ...] = (),
        size: str = "octet_length(payload::text)",
        order: str = "id",
        limit: int = MAX_FACTS,
    ) -> list[dict[str, Any]]:
        params = {**self.params, **(values or {})}

        def statement(sql: str) -> TextClause:
            query = text(sql)
            for name in arrays:
                query = query.bindparams(bindparam(name, type_=ARRAY(Uuid)))
            for name in strings:
                query = query.bindparams(bindparam(name, type_=ARRAY(String)))
            return query

        count, size_bytes = (
            await self.tx.session.execute(
                statement(f"SELECT count(*),coalesce(sum({size}),0) FROM ({selection}) candidate"), params
            )
        ).one()
        if not isinstance(count, int) or not isinstance(size_bytes, int) or count < 0 or size_bytes < 0:
            raise ValueError("Fact size unavailable")
        if count > limit or self._bytes + size_bytes > MAX_BYTES:
            raise FactLimit()
        self._bytes += size_bytes
        result = await self.tx.session.execute(statement(f"{selection} ORDER BY {order} LIMIT {limit + 1}"), params)
        rows = [dict(row) for row in result.mappings()]
        if len(rows) > limit:
            raise FactLimit()
        return rows

    async def releases(self) -> list[WorkerRelease]:
        if self._releases is None:
            rows = await self._rows(
                f"SELECT id,payload FROM worker_releases WHERE {SCOPE} AND EXISTS ("
                "SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(payload->'capabilities')='array' "
                "THEN payload->'capabilities' ELSE '[]'::jsonb END) cap "
                "WHERE (cap->>'taskType') || '@' || (cap->>'taskVersion')=ANY(:capabilities))",
                values={"capabilities": sorted(required_ai_capabilities())},
                strings=("capabilities",),
            )
            result = []
            expected = task_capability()
            reference = f"{expected.task_type}@{expected.task_version}"
            for row in rows:
                release = WorkerRelease.model_validate_json(json.dumps(row["payload"]))
                if release.id != row["id"]:
                    raise ValueError("Release identity unavailable")
                if any(
                    f"{cap.task_type}@{cap.task_version}" == reference
                    and FrozenDocument.from_value(cap.model_dump(by_alias=True)).digest
                    == FrozenDocument.from_value(expected.model_dump(by_alias=True)).digest
                    for cap in release.capabilities
                ):
                    result.append(release)
            self._releases = result
        return self._releases

    async def latest_connections(self) -> list[ConnectionRevision]:
        rows = await self._rows(
            "SELECT id,payload FROM (SELECT DISTINCT ON (name) id,name,revision,payload "
            f"FROM connection_revisions WHERE {SCOPE} ORDER BY name, revision DESC, id DESC) latest "
            "WHERE payload->>'connector'=:connector",
            values={"connector": CONNECTOR_REFERENCE},
        )
        result = []
        for row in rows:
            revision = ConnectionRevision.model_validate_json(json.dumps(row["payload"]))
            if revision.id != row["id"] or not is_agentic_revision(revision):
                raise ValueError("Connection record unavailable")
            result.append(revision)
        return result

    async def published(self) -> dict[str, tuple[str, bool]]:
        definitions = builtin_ai_definitions()
        values = {}
        matches = []
        for index, definition in enumerate(definitions):
            name, version = definition.reference.split("@")
            values.update({f"kind{index}": definition.kind, f"name{index}": name, f"version{index}": version})
            matches.append(f"(v.kind=:kind{index} AND v.name=:name{index} AND v.version=:version{index})")
        rows = await self._rows(
            "SELECT v.kind,v.name,v.version,v.definition_digest,EXISTS(SELECT 1 FROM definition_retirements r "
            "WHERE r.tenant_id=v.tenant_id AND r.project_id=v.project_id AND r.version_id=v.id) AS retired "
            "FROM definition_versions v WHERE v.tenant_id=:tenant AND v.project_id=:project AND ("
            + " OR ".join(matches)
            + ")",
            values=values,
            size="octet_length(to_jsonb(candidate)::text)",
            order="v.kind,v.name,v.version",
            limit=2,
        )
        result = {}
        for row in rows:
            digest, retired = row["definition_digest"], row["retired"]
            if not isinstance(digest, str) or len(digest) != 64 or type(retired) is not bool:
                raise ValueError("Publication record unavailable")
            result[f"{row['name']}@{row['version']}"] = (digest, retired)
        return result

    async def latest_tests(self, revision_ids: tuple[UUID, ...]) -> dict[UUID, dict[str, Any]]:
        if not revision_ids:
            return {}
        rows = await self._rows(
            "SELECT j.revision_id,j.id AS job_id,r.payload FROM connection_test_jobs j "
            "JOIN connection_test_results r ON r.job_id=j.id AND r.tenant_id=j.tenant_id "
            "AND r.project_id=j.project_id AND r.environment_id=j.environment_id "
            "WHERE j.tenant_id=:tenant AND j.project_id=:project AND j.environment_id=:environment "
            "AND j.revision_id=ANY(:revision_ids) AND r.payload->>'kind'='ai'",
            values={"revision_ids": list(revision_ids)},
            arrays=("revision_ids",),
            order="j.revision_id,j.id",
        )
        selected: dict[UUID, tuple[datetime, UUID, dict[str, Any]]] = {}
        damaged = set()
        for row in rows:
            identifier, payload = row["revision_id"], row["payload"]
            try:
                if not isinstance(payload, dict):
                    raise ValueError("Test record unavailable")
                timestamp = test_time(payload)
            except ValueError:
                damaged.add(identifier)
                continue
            prior = selected.get(identifier)
            if prior is None or (timestamp, row["job_id"]) > prior[:2]:
                selected[identifier] = (timestamp, row["job_id"], payload)
        result = {identifier: value[2] for identifier, value in selected.items()}
        # Damaged history cannot be ordered safely behind an older successful test.
        result.update({identifier: {} for identifier in damaged})
        return result

    async def granted_pairs(self, revision_ids: tuple[UUID, ...]) -> set[tuple[UUID, UUID, str]]:
        if not revision_ids:
            return set()
        rows = await self._rows(
            f"SELECT release_id,connection_id,capability FROM worker_connection_grants WHERE {SCOPE} "
            "AND NOT revoked AND connection_id=ANY(:revision_ids) AND capability=ANY(:capabilities)",
            values={"revision_ids": list(revision_ids), "capabilities": sorted(required_ai_capabilities())},
            arrays=("revision_ids",),
            strings=("capabilities",),
            size="octet_length(to_jsonb(candidate)::text)",
            order="release_id,connection_id,capability",
        )
        return {(row["release_id"], row["connection_id"], row["capability"]) for row in rows}

    async def worker_ids(self) -> list[UUID]:
        releases = await self.releases()
        if not releases:
            return []
        # Measure the whole row that WorkerRepository.status will read after per-worker authorization.
        rows = await self._rows(
            "SELECT id,jsonb_typeof(payload) AS payload_type,octet_length(to_jsonb(w)::text) AS logical_bytes "
            f"FROM worker_instances w WHERE {SCOPE} "
            "AND NOT revoked AND release_id=ANY(:release_ids)",
            values={"release_ids": [row.id for row in releases]},
            arrays=("release_ids",),
            size="logical_bytes",
        )
        if any(row["payload_type"] != "object" for row in rows):
            raise ValueError("Worker record unavailable")
        return [row["id"] for row in rows]

    async def lumi(self) -> LumiConfiguration | None:
        rows = await self._rows(
            f"SELECT payload FROM lumi_configurations WHERE {SCOPE}", order="environment_id", limit=1
        )
        return LumiConfiguration.model_validate_json(json.dumps(rows[0]["payload"])) if rows else None

    async def now(self) -> datetime:
        value = await self.tx.session.scalar(text("SELECT transaction_timestamp()"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Observation time unavailable")
        return value
