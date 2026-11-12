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
"""The pure checks a step test passes before it runs: the side-effect gate, the step in its draft, release pins."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.step_tests import StepTestRequest
from firefly_weave.contracts.values import JsonData, JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import SECRET_VALUE

# Steps that change data run for real only when the person acknowledged exactly their effect, every time.
GATED: Final = frozenset({"idempotency_key", "non_idempotent"})
# Per person and environment: step tests running at once, and step tests started in any minute.
MAX_RUNNING: Final = 4
MAX_PER_MINUTE: Final = 30
WINDOW: Final = timedelta(seconds=60)


def confirmation_needed(effect: SideEffect, acknowledged: SideEffect | None) -> bool:
    return effect in GATED and acknowledged != effect


def input_refusal(schema: JsonObject, value: JsonData, schemas: dict[str, JsonObject]) -> CatalogError | None:
    """The refusal for an input that breaks the action's input schema or holds a secret value; paths, never values."""
    diagnostics = validate_payload(schema, value, schemas)
    if not diagnostics:
        return None
    secret = [diagnostic.path for diagnostic in diagnostics if diagnostic.code == SECRET_VALUE]
    if secret:
        return CatalogError(
            422, SECRET_VALUE, "A secret value can't be sent as a step's input", result={"paths": sorted(set(secret))}
        )
    return CatalogError(
        422,
        "WV-STEPTEST-INPUT",
        "The input doesn't match the step's input schema",
        result={"diagnostics": [{"path": diagnostic.path, "code": diagnostic.code} for diagnostic in diagnostics]},
    )


def draft_step(workflow: JsonObject, step_id: str) -> JsonObject | None:
    """The step with this ID anywhere in the draft, blocks included; None when there is none or several."""
    found = [step for step, _ in workflow_steps(workflow) if step.get("id") == step_id]
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class ReleaseChoice:
    id: UUID
    label: str


@dataclass(frozen=True)
class ReleasePins:
    """The release that runs each dependency of the step."""

    pins: Mapping[str, UUID]


@dataclass(frozen=True)
class ReleaseChoices:
    """The dependencies whose release the person must choose, with the releases admitted for each (maybe none)."""

    choices: Mapping[str, Sequence[ReleaseChoice]]

    def result(self) -> JsonObject:
        return {
            "choices": {
                dependency: [{"id": str(choice.id), "label": choice.label} for choice in options]
                for dependency, options in self.choices.items()
            }
        }

    def message(self) -> str:
        missing = sorted(dependency for dependency, options in self.choices.items() if not options)
        if missing:
            return "No release in this environment runs " + ", ".join(missing) + "."
        return "Choose which release runs " + ", ".join(sorted(self.choices)) + " in this environment."


def release_pins(
    needed: Mapping[str, Sequence[ReleaseChoice]], requested: Mapping[str, UUID | None]
) -> ReleasePins | ReleaseChoices:
    """Pin the requested release, else the only admitted one; ask for each dependency where that isn't possible."""
    pins: dict[str, UUID] = {}
    unresolved: dict[str, tuple[ReleaseChoice, ...]] = {}
    for dependency, options in needed.items():
        admitted = tuple(options)
        wanted = requested.get(dependency)
        if wanted is not None and any(choice.id == wanted for choice in admitted):
            pins[dependency] = wanted
        elif wanted is None and len(admitted) == 1:
            pins[dependency] = admitted[0].id
        else:
            unresolved[dependency] = admitted
    return ReleaseChoices(unresolved) if unresolved else ReleasePins(pins)


def _wait(until: datetime, now: datetime) -> int:
    return max(1, math.ceil((until - now).total_seconds()))


def busy_refusal(running: Sequence[datetime], recent: Sequence[datetime], now: datetime) -> CatalogError | None:
    """429 when the person already runs ``MAX_RUNNING`` step tests here or started ``MAX_PER_MINUTE`` in a minute.

    ``running`` holds when each running step test expires, ``recent`` when each of the last minute's started.
    Retry-After is the time until the first counted one leaves.
    """
    if len(running) >= MAX_RUNNING:
        return CatalogError(
            429,
            "WV-STEPTEST-BUSY",
            f"You already have {MAX_RUNNING} steps executing here.",
            retry_after=_wait(min(running), now),
        )
    if len(recent) >= MAX_PER_MINUTE:
        return CatalogError(
            429,
            "WV-STEPTEST-BUSY",
            f"You started {MAX_PER_MINUTE} step tests here in the last minute.",
            retry_after=_wait(min(recent) + WINDOW, now),
        )
    return None


def start_audit(request: StepTestRequest, side_effect: SideEffect, run_id: UUID) -> dict[str, Any]:
    """What a step test's audit record keeps: the step, its effect, its connections, draft and run; no values."""
    return {
        "step_id": request.step_id,
        "uses": request.uses,
        "kind": request.kind,
        "side_effect": side_effect,
        "acknowledged_side_effect": request.acknowledged_side_effect,
        "connection_revision_ids": sorted(str(revision) for revision in request.connections.values()),
        "draft_id": str(request.draft_id),
        "draft_revision": request.draft_revision,
        "run_id": str(run_id),
    }
