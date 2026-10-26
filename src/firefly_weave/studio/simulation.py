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

"""Execute step, Execute previous steps and Execute workflow on the Studio host: the slice of a workflow a target
needs, the catalog a simulation compiles against, stubs for what isn't there, and the simulated run as a trace.

Pure: no network, no clock and no files. The host fetches the project catalog itself and passes it in.
"""

import copy
from collections.abc import Iterator
from typing import cast

from firefly_weave.contracts.values import JsonObject, JsonValue

NULL: JsonObject = {"literal": None}


def _branches(step: JsonObject) -> list[JsonObject]:
    """The step lists a container holds, each with its own ``steps`` and ``output``."""
    kind = step.get("kind")
    if kind == "switch":
        return [*cast(list[JsonObject], step.get("cases") or []), cast(JsonObject, step.get("default") or {})]
    if kind == "parallel":
        return list(cast(dict[str, JsonObject], step.get("branches") or {}).values())
    if kind == "forEach":
        return [cast(JsonObject, step.get("body") or {})]
    if kind == "transaction":
        return [step]
    return []


def steps_of(steps: object) -> Iterator[JsonObject]:
    """Every step of a step list, nested ones included, in document order."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if isinstance(step, dict):
            yield step
            for branch in _branches(step):
                yield from steps_of(branch.get("steps"))


def _cut(steps: list[JsonObject], step_id: str) -> list[JsonObject] | None:
    """The steps up to ``step_id``: those before it, then it (or the container holding it, cut inside)."""
    for index, step in enumerate(steps):
        if step.get("id") == step_id:
            return steps[: index + 1]
        inner = _cut_inside(step, step_id)
        if inner is not None:
            return [*steps[:index], inner]
    return None


def _cut_inside(step: JsonObject, step_id: str) -> JsonObject | None:
    for position, branch in enumerate(_branches(step)):
        steps = branch.get("steps")
        cut = _cut(cast(list[JsonObject], steps), step_id) if isinstance(steps, list) else None
        if cut is None:
            continue
        kept = copy.deepcopy(step)
        for other, sibling in enumerate(_branches(kept)):
            # The branch holding the target ends after it; its siblings run nothing. Conditions stay, so the
            # test data still decides which path a decision takes.
            sibling["steps"] = cast(JsonValue, cut if other == position else [])
            sibling["output"] = copy.deepcopy(NULL)
        return kept
    return None


def slice_for_target(document: JsonObject, step_id: str) -> JsonObject | None:
    """The workflow a target step needs, or None when the step isn't in it.

    Kept: every step before the target in its sequence and in each enclosing sequence, and the enclosing groups with
    their other branches emptied and the target's branch cut after it. The workflow's output becomes null, so a step
    later in the workflow, misconfigured or not, never blocks testing an earlier one.
    """
    spec = document.get("spec")
    steps = spec.get("steps") if isinstance(spec, dict) else None
    if not isinstance(steps, list):
        return None
    cut = _cut(cast(list[JsonObject], steps), step_id)
    if cut is None:
        return None
    sliced = copy.deepcopy(document)
    sliced_spec = cast(JsonObject, sliced["spec"])
    sliced_spec["steps"] = cast(JsonValue, copy.deepcopy(cut))
    sliced_spec["output"] = copy.deepcopy(NULL)
    sliced_spec["outputSchema"] = {}
    return sliced
