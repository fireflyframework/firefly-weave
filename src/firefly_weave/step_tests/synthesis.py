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
"""The one-step workflow a step test runs: the draft's step alone, reading its resolved input.

Synthesis is pure. What a step may change comes from its action in the catalog, never from the request; only the
step's own connection slot is declared, and its connection is the one the request chose for that slot.
"""

import re
from typing import Final, cast
from uuid import UUID

from pydantic import ValidationError
from pyfly.container import service

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.llm import llm_output_schema
from firefly_weave.contracts.definitions import SEMVER_PATTERN, SideEffect
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.step_tests import DraftStep, StepTestRequest, StepTestSynthesizer, SynthesizedTest
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError

API_VERSION: Final = "weave/v1alpha1"
# An AI task's prompt and context arrive resolved, as the step test's input; context may be null but not absent.
AI_TASK_INPUT: Final[JsonObject] = {
    "type": "object",
    "required": ["prompt", "context"],
    "properties": {"prompt": {"type": "string", "minLength": 1}, "context": {}},
}


def test_version(draft_version: str, activation_id: UUID) -> str:
    """The draft's version without build metadata, marked as this test activation's: ``1.3.0+test.0123456789ab``."""
    if not re.fullmatch(SEMVER_PATTERN, draft_version):
        raise ValueError("The draft's version isn't a semantic version")
    version = draft_version.partition("+")[0] + "+test." + activation_id.hex[:12]
    if not re.fullmatch(SEMVER_PATTERN, version):
        raise ValueError("The test version isn't a semantic version")
    return version


MISMATCH: Final = "The saved draft's step doesn't match this request; save and try again"


def _mismatch() -> CatalogError:
    return CatalogError(422, "WV-STEPTEST-KIND", MISMATCH)


def _action_spec(catalog: CatalogSnapshot, uses: str) -> JsonObject:
    resource = catalog.resolve("Action", uses)
    if resource is None:
        raise CatalogError(404, "WV-NOT-FOUND", "The step's action isn't published in this project")
    return cast(JsonObject, resource.definition.value["spec"])


def _slot(draft: DraftStep, request: StepTestRequest) -> tuple[JsonObject, dict[str, UUID]]:
    """The step's slot declaration, copied from the draft, and the connection the request chose for it, if any."""
    slot = draft.step.get("connection")
    if slot is None:
        return {}, {}
    declared = cast(JsonObject, cast(JsonObject, draft.workflow.get("spec", {})).get("connections", {}))
    if not isinstance(slot, str) or not isinstance(declared.get(slot), dict):
        raise _mismatch()
    chosen = request.connections.get(slot)
    return {slot: declared[slot]}, ({slot: chosen} if chosen is not None else {})


def _workflow(draft: DraftStep, spec: JsonObject) -> JsonObject:
    metadata = cast(JsonObject, draft.workflow.get("metadata", {}))
    name, version = metadata.get("name"), metadata.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise _mismatch()
    return {"apiVersion": API_VERSION, "kind": "Workflow", "metadata": {"name": name, "version": version}, "spec": spec}


class ActionSynthesizer:
    """An action step, alone, with ``with: {ref: /input}`` and its action's own retry and timeout."""

    kinds: frozenset[str] = frozenset({"action"})

    def synthesize(self, request: StepTestRequest, draft: DraftStep, catalog: CatalogSnapshot) -> SynthesizedTest:
        uses = draft.step.get("uses")
        if not isinstance(uses, str):
            raise _mismatch()
        action = _action_spec(catalog, uses)
        connections, slots = _slot(draft, request)
        step: JsonObject = {"id": request.step_id, "kind": "action", "uses": uses, "with": {"ref": "/input"}}
        if connections:
            step["connection"] = next(iter(connections))
        spec: JsonObject = {
            "inputSchema": action["inputSchema"],
            "outputSchema": action["outputSchema"],
            "connections": connections,
            "timeoutSeconds": request.timeout_seconds,
            "steps": [step],
            "output": {"ref": f"/steps/{request.step_id}/output"},
        }
        return SynthesizedTest(
            workflow=_workflow(draft, spec),
            input=request.input,
            side_effect=cast(SideEffect, action["sideEffect"]),
            slots=slots,
            timeout_seconds=request.timeout_seconds,
        )


class AiTaskSynthesizer:
    """An AI task, alone, with its profile and its prompt and context read from the input."""

    kinds: frozenset[str] = frozenset({"llm"})

    def synthesize(self, request: StepTestRequest, draft: DraftStep, catalog: CatalogSnapshot) -> SynthesizedTest:
        uses, name = draft.step.get("uses"), draft.step.get("profile")
        if not isinstance(uses, str) or not isinstance(name, str) or draft.step.get("connection") is None:
            raise _mismatch()
        profiles = cast(JsonObject, cast(JsonObject, draft.workflow.get("spec", {})).get("llmProfiles", {}))
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            raise _mismatch()
        try:
            normalized = LLMProfile.model_validate(profile).model_dump(by_alias=True, mode="json", exclude_none=True)
        except ValidationError:
            raise _mismatch() from None
        action = _action_spec(catalog, uses)
        connections, slots = _slot(draft, request)
        step: JsonObject = {
            "id": request.step_id,
            "kind": "llm",
            "uses": uses,
            "profile": name,
            "prompt": {"ref": "/input/prompt"},
            "context": {"ref": "/input/context"},
            "connection": next(iter(connections)),
        }
        spec: JsonObject = {
            "inputSchema": AI_TASK_INPUT,
            "outputSchema": llm_output_schema(normalized),
            "connections": connections,
            "timeoutSeconds": request.timeout_seconds,
            "llmProfiles": {name: profile},
            "steps": [step],
            "output": {"ref": f"/steps/{request.step_id}/output"},
        }
        return SynthesizedTest(
            workflow=_workflow(draft, spec),
            input=request.input,
            side_effect=cast(SideEffect, action["sideEffect"]),
            slots=slots,
            timeout_seconds=request.timeout_seconds,
        )


@service
class StepTestSynthesizers:
    """The synthesizer for each step kind that can run for real; actions and AI tasks are built in."""

    def __init__(self) -> None:
        self._by_kind: dict[str, StepTestSynthesizer] = {}
        self.register(ActionSynthesizer())
        self.register(AiTaskSynthesizer())

    def register(self, synthesizer: StepTestSynthesizer) -> None:
        taken = synthesizer.kinds & self._by_kind.keys()
        if taken:
            raise ValueError("A synthesizer is already registered for " + ", ".join(sorted(taken)))
        for kind in synthesizer.kinds:
            self._by_kind[kind] = synthesizer

    def for_kind(self, kind: str) -> StepTestSynthesizer | None:
        return self._by_kind.get(kind)
