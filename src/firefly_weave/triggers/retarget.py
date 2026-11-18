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

"""Moving a trigger to another activation of its workflow: the rules every source applies before it writes.

A retarget keeps a trigger's identity (so a webhook keeps its URL) and points it at another activation of the same
workflow in the same environment. It never reaches a test activation. The new version must take what the trigger
delivers: each source states that with its own input rule here. The trigger then runs as the person who moved it,
never as whoever created it, so nobody can point someone else's authority at a version of their choosing; the
caller needs every capability creating that trigger needs. The audit records identities only.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import Literal, cast
from uuid import UUID

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expression_types import infer_expression
from firefly_weave.compiler.typecheck import check_compatibility
from firefly_weave.contracts.email import NormalizedEmail
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import RuntimeRepository

SourceKind = Literal["webhook", "kafka", "provider", "email", "event"]


@dataclass(frozen=True)
class ActivationFacts:
    """What a retarget compares about an activation: its workflow's name, its environment, and its test flag."""

    workflow: str
    environment_id: UUID
    test: bool


@dataclass(frozen=True)
class RetargetOutcome:
    """Who and what a retarget moved; a source without a binding leaves the binding IDs out."""

    kind: SourceKind
    resource_id: UUID
    from_activation_id: UUID
    to_activation_id: UUID
    from_principal_id: UUID
    to_principal_id: UUID
    from_binding_id: UUID | None = None
    to_binding_id: UUID | None = None


def not_found() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "Activation not found")


def schema_changed() -> CatalogError:
    return CatalogError(409, "WV-TRIGGER-SCHEMA-CHANGED", "Create a new trigger for the changed input")


def disabled() -> CatalogError:
    return CatalogError(409, "WV-TRIGGER-DISABLED", "Create a new trigger; this one is stopped.")


def signal_target() -> CatalogError:
    return CatalogError(409, "WV-TRIGGER-KIND", "Only a trigger that starts runs moves to another activation")


def same_workflow(current: ActivationFacts, target: ActivationFacts | None) -> None:
    """The target is another activation of the current one's workflow, in its environment, and never a test."""
    if target is None or target.test or target.environment_id != current.environment_id:
        raise not_found()
    if target.workflow != current.workflow:
        raise CatalogError(409, "WV-TRIGGER-WORKFLOW", "Retarget it to a version of the same workflow")


def payload_rule(payload_schema: JsonObject, *, current_input: str, new_input: str) -> None:
    """A webhook or Kafka trigger keeps delivering what it admits: the new input is its payload schema, or unchanged.

    ``current_input`` and ``new_input`` are the digests of the current and the new version's input schemas, as
    their compiled artifacts name them.
    """
    if new_input not in {canonical_digest(payload_schema), current_input}:
        raise schema_changed()


def mapping_rule(
    mapping: JsonObject, event_schemas: Iterable[JsonObject], target: JsonObject, bundle: dict[str, JsonObject]
) -> None:
    """An app event source keeps its mapping: what it makes of every event it dispatches must fit the new input."""
    for schema in event_schemas:
        inferred = infer_expression(mapping, {"payload": schema})
        if check_compatibility(inferred.schema, target, bundle=bundle) == "incompatible":
            raise schema_changed()


def _inlined(value: JsonValue, definitions: dict[str, JsonValue]) -> JsonValue:
    if isinstance(value, list):
        return [_inlined(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = cast(str, value["$ref"]).removeprefix("#/$defs/")
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        return {**cast(JsonObject, _inlined(definitions[reference], definitions)), **siblings}
    return {key: _inlined(item, definitions) for key, item in value.items() if key != "$defs"}


@cache
def _email_input() -> str:
    schema = cast(JsonObject, json.loads(json.dumps(NormalizedEmail.model_json_schema())))
    return json.dumps(_inlined(schema, cast(dict[str, JsonValue], schema.get("$defs", {}))))


def email_input_schema() -> JsonObject:
    """The schema of what an email source starts a run with: one received message, without references."""
    return cast(JsonObject, json.loads(_email_input()))


def email_rule(target: JsonObject, bundle: dict[str, JsonObject]) -> None:
    """An email source keeps starting runs with a received message: the new input must accept every message."""
    if check_compatibility(email_input_schema(), target, bundle=bundle) == "incompatible":
        raise schema_changed()


def require_revision(expected: int | None, current: int) -> None:
    """A retarget names the revision it read: none is refused, and an older one means the trigger changed since."""
    if expected is None:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision")
    if expected != current:
        raise CatalogError(409, "WV-REVISION", "The trigger changed; read it again before moving it")


def retarget_audit(outcome: RetargetOutcome) -> dict[str, str]:
    """The audit's details: identities only, never a schema, a mapping or a payload."""
    details = {
        "kind": outcome.kind,
        "from_activation_id": str(outcome.from_activation_id),
        "to_activation_id": str(outcome.to_activation_id),
        "from_principal_id": str(outcome.from_principal_id),
        "to_principal_id": str(outcome.to_principal_id),
    }
    if outcome.from_binding_id is not None and outcome.to_binding_id is not None:
        details |= {"from_binding_id": str(outcome.from_binding_id), "to_binding_id": str(outcome.to_binding_id)}
    return details


async def activation_facts(tx: Transaction, identifier: UUID) -> ActivationFacts | None:
    """An activation of this project, in any of its environments, with its version's workflow name."""
    rows = await RuntimeRepository(tx).rows(
        "SELECT a.environment_id,a.test,v.name FROM activation_revisions a JOIN definition_versions v "
        "ON v.tenant_id=a.tenant_id AND v.project_id=a.project_id AND v.id=a.version_id "
        "WHERE a.tenant_id=:tenant AND a.project_id=:project AND a.id=:id",
        id=identifier,
    )
    if not rows:
        return None
    return ActivationFacts(workflow=rows[0]["name"], environment_id=rows[0]["environment_id"], test=rows[0]["test"])


async def checked_target(tx: Transaction, current: UUID | None, target: UUID) -> None:
    """Refuse a trigger without a current activation, and a target that isn't its workflow's here."""
    if current is None:
        raise signal_target()
    facts = await activation_facts(tx, current)
    if facts is None:
        raise not_found()
    same_workflow(facts, await activation_facts(tx, target))
