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

"""Running steps for real in an environment: step tests, draft test activations and the environment test policy.

These are platform API bodies, so every field is snake_case. The server enforces every gate (capability, test
policy, connection binding, side-effect acknowledgement, ownership); these models only fix the wire shapes.
"""

from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.definitions import ContractModel, PositiveInt, ResourceName, SideEffect, VersionedReference
from firefly_weave.contracts.instance_keys import InstanceKeyText, InstanceKeyTextOrEmpty
from firefly_weave.contracts.values import JsonData, JsonObjectData, UnicodeString

if TYPE_CHECKING:
    from firefly_weave.compiler.catalog import CatalogSnapshot

STEP_TEST_TIMEOUT_SECONDS: Final = 300
AGENT_STEP_TEST_TIMEOUT_SECONDS: Final = 900
DRAFT_TEST_LIFETIME_SECONDS: Final = 900
DRAFT_TEST_MAX_LIFETIME_SECONDS: Final = 3600

type ToolName = Annotated[UnicodeString, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
type StepTestStatus = Literal["running", "waiting", "succeeded", "failed", "timed_out", "cancelled"]
type StepTestCalls = Literal["enabled", "disabled"]


def _absent(value: object) -> bool:
    return value is None


class StepTestRequest(ContractModel):
    """``POST {ENV}/step-tests``: run one step of a saved draft for real."""

    kind: ResourceName
    step_id: ResourceName
    uses: VersionedReference | None = None
    input: JsonData
    connections: dict[ResourceName, UUID] = Field(default_factory=dict)
    profile: ResourceName | None = None
    acknowledged_side_effect: SideEffect | None = None
    releases: dict[VersionedReference, UUID | None] = Field(default_factory=dict)
    tool_mocks: dict[ToolName, JsonData] = Field(default_factory=dict)
    real_tools: list[ToolName] = Field(default_factory=list)
    draft_id: UUID
    draft_revision: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        cap = AGENT_STEP_TEST_TIMEOUT_SECONDS if self.kind == "agent" else STEP_TEST_TIMEOUT_SECONDS
        if self.timeout_seconds > cap:
            raise ValueError(f"A {self.kind} step test runs for at most {cap} seconds")
        if len(set(self.real_tools)) != len(self.real_tools):
            raise ValueError("real_tools lists each tool once")
        return self


class StepTestAccepted(ContractModel):
    id: UUID
    run_id: UUID
    status: Literal["running"]
    side_effect: SideEffect
    expires_at: AwareDatetime


class StepTestPending(ContractModel):
    """Something a waiting step test needs from its creator: a confirmation, a review or a person's answer."""

    node_id: ResourceName
    instance_key: InstanceKeyTextOrEmpty
    kind: Literal["confirm", "review", "person"]
    tool: ToolName | None = Field(default=None, exclude_if=_absent)
    side_effect: SideEffect | None = Field(default=None, exclude_if=_absent)
    arguments: JsonObjectData | None = Field(default=None, exclude_if=_absent)
    task_id: UUID | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def item_shape(self) -> Self:
        if self.kind == "confirm" and (
            self.tool is None or self.side_effect is None or self.arguments is None or self.task_id is not None
        ):
            raise ValueError("A confirm item names its tool, side effect and arguments, and no task")
        if self.kind != "confirm" and self.task_id is None:
            raise ValueError("Review and person items name their human task")
        return self


class StepTestError(ContractModel):
    code: Annotated[UnicodeString, Field(pattern=r"^WV-[A-Z0-9]+(?:-[A-Z0-9_]+)+$")]
    message: Annotated[UnicodeString, Field(min_length=1)]


class StepTestView(ContractModel):
    """``GET {ENV}/step-tests/{id}``; only the creating principal can read it."""

    id: UUID
    run_id: UUID
    status: StepTestStatus
    side_effect: SideEffect
    pending: list[StepTestPending] = Field(default_factory=list)
    output: JsonData = None
    error: StepTestError | None = None
    attempts: int = Field(ge=0)
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def pending_while_waiting(self) -> Self:
        if (self.status == "waiting") != bool(self.pending):
            raise ValueError("pending lists items exactly while the step test is waiting")
        return self


class StepTestAnswer(ContractModel):
    """``POST {ENV}/step-tests/{id}/answers``: true runs the tool call for real, false returns its mock."""

    instance_key: InstanceKeyText
    confirm: bool


class DraftTestBindings(ContractModel):
    connection_revision_ids: dict[ResourceName, UUID] = Field(default_factory=dict)
    connector_release_ids: dict[UUID, UUID] = Field(default_factory=dict)
    worker_release_ids: dict[ResourceName, UUID] = Field(default_factory=dict)
    assignment_binding_ids: dict[ResourceName, UUID] = Field(default_factory=dict)
    workflow_activation_ids: dict[VersionedReference, UUID] = Field(default_factory=dict)


class DraftTestActivationRequest(ContractModel):
    """``POST {ENV}/test-activations``: a short-lived activation of a saved draft for Execute workflow."""

    draft_id: UUID
    draft_revision: int = Field(ge=1)
    bindings: DraftTestBindings = Field(default_factory=DraftTestBindings)
    acknowledged_side_effect: SideEffect | None = None
    lifetime_seconds: int = Field(default=DRAFT_TEST_LIFETIME_SECONDS, ge=1, le=DRAFT_TEST_MAX_LIFETIME_SECONDS)


class DraftTestActivation(ContractModel):
    id: UUID
    activation_id: UUID
    artifact_digest: Digest
    side_effect: SideEffect
    expires_at: AwareDatetime


class DraftTestRunRequest(ContractModel):
    input: JsonData


class DraftTestListenRequest(ContractModel):
    """``POST {ENV}/test-activations/{id}/listen``: a signed webhook that expires with the activation."""

    secret_ref: ResourceName
    max_body_bytes: int = Field(default=1_048_576, ge=1, le=1_048_576)
    tolerance_seconds: int = Field(default=300, ge=1, le=300)


class DraftTestListen(ContractModel):
    # No query string or fragment: the URL never carries a secret.
    test_url: Annotated[UnicodeString, Field(max_length=2048, pattern=r"^https?://[^?#\s]+$")]
    expires_at: AwareDatetime


class EnvironmentTestPolicy(ContractModel):
    """``GET {ENV}/test-policy``; an environment without a stored policy reads as disabled at revision 0."""

    test_calls: StepTestCalls
    revision: int = Field(ge=0)


class EnvironmentTestPolicyUpdate(ContractModel):
    test_calls: StepTestCalls


class SynthesizedTest(ContractModel):
    """The one-step workflow a synthesizer builds for a step test, and what it may do."""

    workflow: JsonObjectData
    input: JsonData
    side_effect: SideEffect
    slots: dict[ResourceName, UUID]
    timeout_seconds: PositiveInt


class StepTestSynthesizer(Protocol):
    """Builds the workflow for step tests of some step kinds (actions and AI tasks here; AI agents elsewhere)."""

    kinds: frozenset[str]

    def synthesize(self, request: StepTestRequest, catalog: "CatalogSnapshot") -> SynthesizedTest: ...
