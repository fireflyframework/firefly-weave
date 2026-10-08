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

"""Bodies of the editor's ``/studio/local/*`` routes: simulated runs, test-data checks and decision tables.

These are Studio host payloads, so fields are camelCase like the existing host routes. The debug session routes keep
the platform ``debug.*`` shapes and are not repeated here. Hosts serialize responses with
``model_dump(mode="json", by_alias=True, exclude_unset=True)``, so a field the simulator did not report is absent.
The simulator's owners review every change to this module.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.definitions import DecisionTableDefinition, JsonPointer, ResourceName, VersionedReference
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN, InstanceKeyText, InstanceKeyTextOrEmpty, node_of
from firefly_weave.contracts.studio_documents import (
    REFERENCE_PATTERN,
    AiTurnsEntry,
    CalleeReference,
    FrameKey,
    HumanEntry,
    SignalEntry,
    StudioTestData,
)
from firefly_weave.contracts.values import JsonData, JsonObjectData

SOURCE_LIMIT = 1024 * 1024
MOCK_KEY_PATTERN = (
    rf"^(?:node:(?:{INSTANCE_KEY_PATTERN})|agent:(?:{INSTANCE_KEY_PATTERN})"
    rf"|tool:(?:{INSTANCE_KEY_PATTERN})/[A-Za-z][A-Za-z0-9_-]{{0,63}}|action:{REFERENCE_PATTERN[1:-1]})$"
)

type MockKey = Annotated[str, Field(max_length=1024, pattern=MOCK_KEY_PATTERN)]
type TraceStatus = Literal["running", "waiting", "completed", "failed"]
type CheckCode = Literal[
    "WV-SCHEMA-SECRET_VALUE", "WV-SCHEMA-CLASSIFICATION", "WV-STUDIO-TESTDATA-SCHEMA", "WV-STUDIO-TESTDATA-UNRESOLVED"
]


def _instance_of(node_id: str, instance_key: str) -> bool:
    """An empty key stands for the step itself; a non-empty one is a longer key of that step."""
    return instance_key == "" or (instance_key != node_id and node_of(instance_key) == node_id)


class _HostBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CalleeSource(_HostBody):
    """A called workflow: a local draft's source and format, or only mocks for a published version."""

    source: str | None = Field(default=None, max_length=SOURCE_LIMIT)
    format: Literal["yaml", "json"] | None = None
    mocks: dict[MockKey, JsonData] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode="after")
    def source_with_format(self) -> Self:
        if (self.source is None) != (self.format is None):
            raise ValueError("A callee source and its format come together")
        return self


class ExecuteScripts(_HostBody):
    signals: dict[InstanceKeyText, SignalEntry] = Field(default_factory=dict, max_length=1000)
    humans: dict[InstanceKeyText, HumanEntry] = Field(default_factory=dict, max_length=1000)
    aiTurns: dict[InstanceKeyText, AiTurnsEntry] = Field(default_factory=dict, max_length=1000)
    autoAdvanceWaits: bool = True


class ExecuteTarget(_HostBody):
    mode: Literal["workflow", "step", "before"]
    stepId: ResourceName | None = None
    instanceKey: InstanceKeyText | None = None

    @model_validator(mode="after")
    def target_rules(self) -> Self:
        if self.mode == "workflow" and (self.stepId is not None or self.instanceKey is not None):
            raise ValueError("Execute workflow names no step")
        if self.mode != "workflow" and self.stepId is None:
            raise ValueError("Execute step and Execute previous steps name stepId")
        if self.instanceKey is not None and node_of(self.instanceKey) != self.stepId:
            raise ValueError("instanceKey must be an instance of stepId")
        return self


class SimulationRequest(_HostBody):
    """The body of a debug session; Execute adds a target."""

    source: str = Field(max_length=SOURCE_LIMIT)
    format: Literal["yaml", "json"]
    definitions: list[JsonObjectData] = Field(default_factory=list)
    workflows: dict[CalleeReference, CalleeSource] = Field(default_factory=dict, max_length=50)
    catalog: Literal["project", "none"]
    refreshCatalog: bool = False
    stubUnresolved: bool = False
    input: JsonData = None
    mocks: dict[MockKey, JsonData] = Field(default_factory=dict, max_length=1000)
    scripts: ExecuteScripts = Field(default_factory=ExecuteScripts)
    now: AwareDatetime


class ExecuteRequest(SimulationRequest):
    target: ExecuteTarget


class CompileSummary(_HostBody):
    ok: bool
    sliced: bool
    artifactDigest: Digest | None = None
    diagnostics: list[Diagnostic]
    stubs: list[VersionedReference]


class TraceEntry(_HostBody):
    index: int = Field(ge=0)
    nodeId: ResourceName
    instanceKey: InstanceKeyTextOrEmpty
    kind: ResourceName
    status: TraceStatus
    input: JsonData = None
    output: JsonData = None
    virtualTime: AwareDatetime | None = None
    frame: FrameKey | None = None

    @model_validator(mode="after")
    def key_of_node(self) -> Self:
        if not _instance_of(self.nodeId, self.instanceKey):
            raise ValueError("instanceKey is empty or an instance of nodeId")
        return self


class LoopProgress(_HostBody):
    nodeId: ResourceName
    instanceKey: InstanceKeyTextOrEmpty
    count: int = Field(ge=0)
    completed: int = Field(ge=0)
    running: int = Field(ge=0)
    nextIndex: int = Field(ge=0)
    status: Literal["running", "completed", "failed"]


class FrameView(_HostBody):
    key: FrameKey
    workflow: CalleeReference
    callNode: InstanceKeyText
    status: TraceStatus


class LoopRoot(_HostBody):
    item: JsonData
    index: int = Field(ge=0)


class SelectedScope(_HostBody):
    item: JsonData = None
    index: int | None = Field(default=None, ge=0)
    loops: dict[ResourceName, LoopRoot] = Field(default_factory=dict)


class Blocked(_HostBody):
    reason: Literal["missing_mock", "signal", "human", "path", "callee"]
    nodeId: ResourceName
    instanceKey: InstanceKeyTextOrEmpty
    message: str = Field(min_length=1, max_length=2000)


class ExecuteResponse(_HostBody):
    status: Literal["completed", "blocked", "failed", "limit"]
    compile: CompileSummary
    trace: list[TraceEntry]
    loops: list[LoopProgress] = Field(default_factory=list)
    frames: list[FrameView] = Field(default_factory=list)
    selectedScope: SelectedScope | None = None
    blocked: Blocked | None = None
    variables: JsonObjectData = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_rules(self) -> Self:
        if (self.status == "blocked") != (self.blocked is not None):
            raise ValueError("blocked is present exactly when status is blocked")
        if not self.compile.ok and self.status != "failed":
            raise ValueError("A definition that does not compile reports status failed")
        return self


class CheckTestDataRequest(_HostBody):
    source: str = Field(max_length=SOURCE_LIMIT)
    format: Literal["yaml", "json"]
    definitions: list[JsonObjectData] = Field(default_factory=list)
    catalog: Literal["project", "none"]
    testData: StudioTestData


class CheckTestDataEntry(_HostBody):
    path: JsonPointer
    ok: bool
    code: CheckCode | None = None

    @model_validator(mode="after")
    def code_when_refused(self) -> Self:
        if self.ok == (self.code is not None):
            raise ValueError("A refused entry names its code; an accepted one names none")
        return self


class CheckTestDataResult(_HostBody):
    entries: list[CheckTestDataEntry]


class DecisionEvaluateRequest(_HostBody):
    """Try an input against a local decision table; the result is ``DecisionEvaluation``."""

    table: DecisionTableDefinition
    input: JsonData
