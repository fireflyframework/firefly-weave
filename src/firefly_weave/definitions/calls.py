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


"""Workflow call pins: the exact activation each call of an activated workflow starts its child runs with.

An activation of a workflow that calls others pins one activation of every called version in the same environment,
checked by the rules ``call_pins`` applies. Test activations of drafts use the same rules.
``call_tree_side_effect`` summarizes the strongest side effect a run can cause through the pinned calls.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast, get_args
from uuid import UUID

from firefly_weave.compiler.ir import allows_caller
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, WorkflowCallPin
from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository

# Side effects from the weakest to the strongest.
SIDE_EFFECTS: tuple[SideEffect, ...] = get_args(SideEffect.__value__)

type CallTreeResolver = Callable[[UUID], Awaitable[tuple[JsonObject, Mapping[str, UUID]]]]
type CalleeLoader = Callable[[UUID], Awaitable["PinnedCallee | None"]]


@dataclass(frozen=True)
class PinnedCallee:
    """A pinned activation as stored, the retirement of its name's latest revision, and its version row."""

    activation: Activation
    latest_retired: bool
    version: Mapping[str, Any]


def readiness(message: str) -> CatalogError:
    return CatalogError(422, "WV-READINESS-CALL", message)


def called_dependencies(executable: JsonObject) -> dict[str, str]:
    """Each workflow version an executable calls, by reference, with the definition digest it was compiled against."""
    return {
        cast(str, dependency["reference"]): cast(str, dependency["digest"])
        for dependency in cast(list[JsonObject], executable.get("dependencies", []))
        if dependency["kind"] == "Workflow"
    }


async def call_pins(
    caller: str,
    scope: Scope,
    executable: JsonObject,
    supplied: Mapping[str, UUID],
    load: CalleeLoader,
    *,
    max_call_depth: int = MAX_CALL_DEPTH,
) -> tuple[list[WorkflowCallPin], int]:
    """Check the activations pinned for ``caller``'s calls; returns the pins and the activation's call depth.

    ``supplied`` maps each called reference to an activation ID; ``load`` reads one pinned activation of
    ``scope``'s environment (``None`` when there is none), and runs only once the pinned references match the calls.
    Every failure is ``WV-READINESS-CALL``.
    """
    called = called_dependencies(executable)
    if set(supplied) != set(called):
        raise readiness("Pin exactly one activation of each workflow version this workflow calls")
    pins: list[WorkflowCallPin] = []
    depth = 0
    for reference in sorted(called):
        callee = await load(supplied[reference])
        if callee is None or callee.activation.id != supplied[reference] or callee.activation.request.scope != scope:
            raise readiness("A pinned workflow activation was not found in this environment")
        activation, version = callee.activation, callee.version
        name, number = reference.split("@")
        if activation.name != name or version["kind"] != "Workflow" or version["version"] != number:
            raise readiness("A pinned activation does not run the called workflow version")
        if activation.retired or callee.latest_retired or version["retired"]:
            raise readiness("A pinned workflow activation or its version is retired")
        if version["digest"] != activation.request.artifact_digest:
            raise readiness("A pinned activation does not match its published version")
        if version["definition_digest"] != called[reference]:
            raise readiness("A pinned version differs from the version this workflow was compiled against")
        if not allows_caller(cast(JsonObject, version["document"]["spec"]).get("callable"), caller):
            raise readiness("A called workflow version does not allow calls from this workflow")
        if activation.call_depth + 1 > max_call_depth:
            raise readiness(f"The chain of workflow calls is deeper than {max_call_depth:,}")
        depth = max(depth, activation.call_depth + 1)
        pins.append(
            WorkflowCallPin(
                reference=reference,
                activation_id=activation.id,
                activation_revision=activation.revision,
                version_id=activation.request.version_id,
                artifact_digest=activation.request.artifact_digest,
                definition_digest=version["definition_digest"],
            )
        )
    return pins, depth


async def pinned_callee(repository: DefinitionRepository, scope: Scope, identifier: UUID) -> PinnedCallee | None:
    """Load a pinned activation of ``scope``'s environment, the latest revision of its name, and its version."""
    rows = await repository.rows(
        "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND project_id=:project "
        "AND environment_id=:environment AND id=:id",
        environment=scope.environment_id,
        id=identifier,
    )
    if not rows:
        return None
    activation = Activation.model_validate_json(json.dumps(rows[0]["payload"]))
    latest = await repository.rows(
        "SELECT payload FROM activation_revisions WHERE tenant_id=:tenant AND project_id=:project "
        "AND environment_id=:environment AND name=:name ORDER BY revision DESC LIMIT 1",
        environment=scope.environment_id,
        name=activation.name,
    )
    version = await repository.version(activation.request.version_id)
    return PinnedCallee(activation, bool(latest[0]["payload"]["retired"]), version)


def side_effect(executable: JsonObject) -> SideEffect:
    """The strongest side effect among an executable's actions; ``read_only`` when it has none."""
    strongest: SideEffect = SIDE_EFFECTS[0]
    for dependency in cast(list[JsonObject], executable.get("dependencies", [])):
        if dependency["kind"] == "Action":
            effect = cast(SideEffect, cast(JsonObject, cast(JsonObject, dependency["document"])["spec"])["sideEffect"])
            strongest = max(strongest, effect, key=SIDE_EFFECTS.index)
    return strongest


async def call_tree_side_effect(
    executable: JsonObject, pins: Mapping[str, UUID], resolve: CallTreeResolver
) -> SideEffect:
    """The strongest side effect of a workflow and of every workflow its pinned calls start, transitively.

    ``resolve`` returns a pinned activation's executable and its own pins, by reference.
    """
    strongest = side_effect(executable)
    seen: set[UUID] = set()
    pending = list(pins.values())
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        callee, callee_pins = await resolve(identifier)
        strongest = max(strongest, side_effect(callee), key=SIDE_EFFECTS.index)
        pending.extend(callee_pins.values())
    return strongest


def pinned_call_tree(repository: DefinitionRepository, scope: Scope) -> CallTreeResolver:
    """A resolver for ``call_tree_side_effect`` over the activations of ``scope``'s environment."""

    async def resolve(identifier: UUID) -> tuple[JsonObject, Mapping[str, UUID]]:
        callee = await pinned_callee(repository, scope, identifier)
        if callee is None:
            raise readiness("A pinned workflow activation was not found in this environment")
        executable = cast(JsonObject, cast(JsonObject, callee.version["artifact"])["executable"])
        return executable, {pin.reference: pin.activation_id for pin in callee.activation.workflow_call_pins}

    return resolve
