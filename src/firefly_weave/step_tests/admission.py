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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.values import JsonObject

# Steps that change data run for real only when the person acknowledged exactly their effect, every time.
GATED: Final = frozenset({"idempotency_key", "non_idempotent"})


def confirmation_needed(effect: SideEffect, acknowledged: SideEffect | None) -> bool:
    return effect in GATED and acknowledged != effect


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
