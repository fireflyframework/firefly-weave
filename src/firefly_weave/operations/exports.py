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

"""Pure safe evidence projection and stable comparison; no storage or executors."""

import json
from typing import cast

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import ActionNode, SignalNode
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import TaskError
from firefly_weave.operations.redaction import Omission, project
from firefly_weave.runtime.kernel import action_schemas, workflow
from firefly_weave.runtime.models import RuntimeEvent, Transition


def lock_digest(artifact: CompiledArtifact) -> str:
    return canonical_digest({"dependencies": artifact.executable["dependencies"]})


def transition_digest(result: Transition) -> str:
    # Evidence is transport metadata, not a kernel operand; exclude it from deferred facts.
    value = result.model_dump(mode="json")
    for event in value["state"]["deferred_results"]:
        event.pop("evidence", None)
        event["data"].pop("reason", None)
        event["data"].pop("evidence_reference", None)
    return canonical_digest(
        {
            "algorithm": "sha256-rfc8785-v1",
            "policy": "classified-v1",
            "projection": {"available": True, "value": value, "omissions": []},
        }
    )


def safe_event(event: RuntimeEvent, artifact: CompiledArtifact | None, *, unavailable: bool = False) -> RuntimeEvent:
    data = dict(event.data)
    omissions: list[Omission] = []
    fields = {
        "started": {"admission_policy"},
        "task_completed": {"node_id", "generation", "output"},
        "task_failed": {"node_id", "generation", "output"},
        "incident_opened": {"node_id", "generation", "code", "next_attempt_at"},
        "recovery_scheduled": {"node_id", "generation", "code", "next_attempt_at"},
        "incident_resolved": {
            "receipt_id",
            "node_id",
            "generation",
            "incident_key",
            "kind",
            "output",
            "actor_id",
            "reason",
            "evidence_reference",
        },
        "cancelled": {"actor_id", "reason", "code"},
        "timed_out": {"node_id", "deadline", "wait_id", "code"},
        "wait_elapsed": {"node_id", "deadline", "wait_id"},
        "signal_received": {"node_id", "output", "accepted_at", "deadline"},
    }[event.type]
    if any(key not in fields for key in data):
        data = {key: value for key, value in data.items() if key in fields}
        omissions.append(Omission(path="/data/*", reason="classification_unavailable"))
    for name in ("reason", "evidence_reference"):
        if name in data:
            data.pop(name)
            omissions.append(Omission(path="/data/" + name, reason="confidential_text"))
    if unavailable:
        return event.model_copy(update={"data": {}, "evidence": None})
    if event.type == "task_failed" and "output" in data:
        try:
            TaskError.model_validate_json(json.dumps(data["output"]))
            return event.model_copy(update={"data": data})
        except ValueError:
            data.pop("output")
            omissions.append(Omission(path="/data/output", reason="classification_unavailable"))
    if "output" in data and not (
        event.type == "incident_resolved"
        and event.data.get("kind") != "accept_reconciled_result"
        and data["output"] is None
    ):
        assert artifact is not None
        ir = workflow(artifact)
        node = next((n for n in ir.graph.nodes if n.id == data.get("node_id")), None)
        schemas: list[JsonObject] = []
        if isinstance(node, ActionNode) and event.type in {"task_completed", "incident_resolved"}:
            definition = load_definition(next(d.document for d in ir.dependencies if d.digest == node.dependency))
            schemas = action_schemas(ir, cast(ActionDefinition, definition), "output")
        elif isinstance(node, SignalNode) and event.type == "signal_received":
            schemas = [ir.schemas[node.schema_ref]]
        bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
        if not schemas or any(not project(data["output"], schema, bundle).available for schema in schemas):
            data.pop("output")
            omissions.append(Omission(path="/data/output", reason="classified_secret"))
    evidence = dict(event.evidence) if event.evidence is not None else None
    if evidence is not None:
        evidence["omissions"] = cast(
            JsonValue, [*cast(list[JsonValue], evidence.get("omissions", [])), *(o.model_dump() for o in omissions)]
        )
    return event.model_copy(update={"data": data, "evidence": evidence})


def bounded_size(value: object, limit: int, *, by_alias: bool = False) -> int | None:
    """Count compact UTF-8 wire JSON without materializing model/state containers.

    Only contract fields and JSON values are inspected. A single scalar is encoded
    at a time; accepted payload scalars retain the existing 1 MiB admission bound.
    """
    from datetime import datetime
    from uuid import UUID

    from pydantic import TypeAdapter

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.definitions import ContractModel

    total = 0

    def charge(amount: int) -> None:
        nonlocal total
        total += amount
        if total > limit:
            raise OverflowError

    def scalar(item: object) -> None:
        charge(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()))

    def visit(item: object) -> None:
        if isinstance(item, (ContractModel, Scope)):
            charge(2)
            count = 0
            for name, field in type(item).model_fields.items():
                child = item.__dict__[name]
                if field.exclude or (field.exclude_if is not None and field.exclude_if(child)):
                    continue
                if count:
                    charge(1)
                scalar((field.serialization_alias or field.alias or name) if by_alias else name)
                charge(1)
                visit(child)
                count += 1
        elif isinstance(item, dict):
            charge(2)
            for index, (key, child) in enumerate(item.items()):
                if index:
                    charge(1)
                scalar(key)
                charge(1)
                visit(child)
        elif isinstance(item, (list, tuple)):
            charge(2)
            for index, child in enumerate(item):
                if index:
                    charge(1)
                visit(child)
        elif isinstance(item, datetime):
            scalar(TypeAdapter(datetime).dump_python(item, mode="json"))
        elif isinstance(item, UUID):
            scalar(str(item))
        elif item is None or type(item) in {str, int, float, bool}:
            scalar(item)
        else:
            raise TypeError("Wire size requires contract models and JSON values")

    try:
        visit(value)
    except OverflowError:
        return None
    return total
