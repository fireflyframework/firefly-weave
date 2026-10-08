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

"""Strict wire models; schema validation and semantic compilation are separate stages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Discriminator, Field, Tag, TypeAdapter, model_validator
from pydantic.json_schema import JsonSchemaValue, SkipJsonSchema

from firefly_weave.contracts.language_features import DEFAULT_LOOP_MAX_ITEMS
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonData, JsonObject, JsonObjectData, UnicodeString

SEMVER_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
type DistributionVersion = Annotated[UnicodeString, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")]

type SemVer = Annotated[UnicodeString, Field(pattern=rf"^{SEMVER_PATTERN}$")]
type ResourceName = Annotated[UnicodeString, Field(pattern=rf"^{NAME_PATTERN}$")]
type VersionedReference = Annotated[UnicodeString, Field(pattern=rf"^{NAME_PATTERN}@{SEMVER_PATTERN}$")]
type JsonPointer = Annotated[UnicodeString, Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]
type PositiveInt = Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER)]
type SideEffect = Literal["read_only", "idempotent", "idempotency_key", "non_idempotent"]
type OperatorName = Literal[
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "and",
    "or",
    "not",
    "exists",
    "coalesce",
    "contains",
    "notContains",
    "in",
    "notIn",
    "startsWith",
    "endsWith",
    "concat",
    "join",
]


def _reject_explicit_null(value: object) -> object:
    if value is None:
        raise ValueError("Field may be omitted, but must not be null when supplied")
    return value


def _is_absent(value: object) -> bool:
    return value is None


def _omit_absent_default(schema: JsonSchemaValue) -> None:
    schema.pop("default", None)


# None represents an omitted field internally, never a supplied JSON null.
type OmissionOnly[T] = Annotated[T | SkipJsonSchema[None], BeforeValidator(_reject_explicit_null)]


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Metadata(ContractModel):
    name: ResourceName
    version: SemVer


class LiteralExpression(ContractModel):
    literal: JsonData


class RefExpression(ContractModel):
    ref: JsonPointer


class ObjectExpression(ContractModel):
    object: dict[UnicodeString, Expression]


class ArrayExpression(ContractModel):
    array: list[Expression]


class Operation(ContractModel):
    name: OperatorName
    args: list[Expression]


class OpExpression(ContractModel):
    op: Operation


def expression_tag(value: object) -> str | None:
    if isinstance(value, dict):
        if len(value) == 1:
            return str(next(iter(value)))
        return None
    for tag, model in (
        ("literal", LiteralExpression),
        ("ref", RefExpression),
        ("object", ObjectExpression),
        ("array", ArrayExpression),
        ("op", OpExpression),
    ):
        if isinstance(value, model):
            return tag
    return None


type Expression = Annotated[
    Annotated[LiteralExpression, Tag("literal")]
    | Annotated[RefExpression, Tag("ref")]
    | Annotated[ObjectExpression, Tag("object")]
    | Annotated[ArrayExpression, Tag("array")]
    | Annotated[OpExpression, Tag("op")],
    Discriminator(expression_tag),
]


class RetryPolicy(ContractModel):
    max_attempts: PositiveInt = Field(default=1, alias="maxAttempts")
    initial_delay_seconds: PositiveInt = Field(default=1, alias="initialDelaySeconds")
    max_delay_seconds: PositiveInt = Field(default=30, alias="maxDelaySeconds")

    @model_validator(mode="after")
    def delay_is_capped(self) -> RetryPolicy:
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("maxDelaySeconds must be at least initialDelaySeconds")
        return self


class WorkerImplementation(ContractModel):
    kind: Literal["worker"]
    task_type: ResourceName = Field(alias="taskType")
    task_version: SemVer = Field(alias="taskVersion")


class ConnectorImplementation(ContractModel):
    kind: Literal["connector"]
    uses: VersionedReference
    action: ResourceName
    config: OmissionOnly[JsonObjectData] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


type ActionImplementation = Annotated[WorkerImplementation | ConnectorImplementation, Field(discriminator="kind")]


class ConnectionRequirement(ContractModel):
    connector: VersionedReference
    required: bool = True


class WorkerRouting(ContractModel):
    queue: ResourceName


class ActionSpec(ContractModel):
    implementation: ActionImplementation
    side_effect: SideEffect = Field(alias="sideEffect")
    timeout_seconds: PositiveInt = Field(alias="timeoutSeconds")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    input_schema: JsonObjectData = Field(alias="inputSchema")
    output_schema: JsonObjectData = Field(alias="outputSchema")
    connection: OmissionOnly[ConnectionRequirement] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    routing: OmissionOnly[WorkerRouting] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class ActionStep(ContractModel):
    id: ResourceName
    kind: Literal["action"]
    uses: VersionedReference
    input: Expression = Field(alias="with")
    connection: OmissionOnly[ResourceName] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class LLMStep(ContractModel):
    id: ResourceName
    kind: Literal["llm"]
    uses: VersionedReference
    profile: ResourceName
    prompt: Expression
    context: Expression
    connection: ResourceName


class TransformStep(ContractModel):
    id: ResourceName
    kind: Literal["transform"]
    value: Expression


class DecisionRule(ContractModel):
    id: ResourceName
    when: Expression
    output: Expression


class DecisionTableSpec(ContractModel):
    input_schema: JsonObjectData = Field(alias="inputSchema")
    output_schema: JsonObjectData = Field(alias="outputSchema")
    hit_policy: Literal["first", "unique", "collect"] = Field(alias="hitPolicy")
    rules: list[DecisionRule] = Field(min_length=1, max_length=1000)
    default_output: OmissionOnly[Expression] = Field(
        default=None, alias="defaultOutput", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )

    @model_validator(mode="after")
    def policy_contract(self) -> DecisionTableSpec:
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Decision rule IDs must be unique")
        if self.hit_policy == "collect":
            if self.default_output is not None:
                raise ValueError("Collect returns an empty list when no rules match; omit defaultOutput")
            if (
                self.output_schema.get("type") != "array"
                or not isinstance(self.output_schema.get("items"), dict)
                or "prefixItems" in self.output_schema
            ):
                raise ValueError("Collect requires an array outputSchema with items and without prefixItems")
        return self


class DecisionTableStep(ContractModel):
    id: ResourceName
    kind: Literal["decisionTable"]
    uses: VersionedReference
    input: Expression = Field(alias="with")


class Branch(ContractModel):
    steps: list[Step]
    output: Expression


class SwitchCase(Branch):
    when: Expression


class SwitchStep(ContractModel):
    id: ResourceName
    kind: Literal["switch"]
    cases: list[SwitchCase] = Field(min_length=1)
    default: Branch


class ParallelStep(ContractModel):
    id: ResourceName
    kind: Literal["parallel"]
    branches: dict[ResourceName, Branch] = Field(min_length=1)
    concurrency: PositiveInt


class WaitStep(ContractModel):
    id: ResourceName
    kind: Literal["wait"]
    duration_seconds: PositiveInt = Field(alias="durationSeconds")


class SignalStep(ContractModel):
    id: ResourceName
    kind: Literal["signal"]
    name: ResourceName
    timeout_seconds: PositiveInt = Field(alias="timeoutSeconds")
    payload_schema: JsonObjectData = Field(alias="payloadSchema")


class HumanTaskStep(ContractModel):
    id: ResourceName
    kind: Literal["humanTask"]
    assignment: ResourceName
    title: Expression
    context: Expression
    form_schema: JsonObjectData = Field(alias="formSchema")
    decisions: list[ResourceName] = Field(default_factory=lambda: ["approve", "reject"], min_length=1, max_length=32)
    due_seconds: OmissionOnly[PositiveInt] = Field(
        default=None, alias="dueSeconds", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    expiry_seconds: OmissionOnly[PositiveInt] = Field(
        default=None, alias="expirySeconds", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )

    @model_validator(mode="after")
    def unique_decisions(self) -> HumanTaskStep:
        if len(set(self.decisions)) != len(self.decisions):
            raise ValueError("Decision identifiers must be unique")
        return self


class FailStep(ContractModel):
    id: ResourceName
    kind: Literal["fail"]
    code: ResourceName
    message: Annotated[UnicodeString, Field(min_length=1)]


class ForEachStep(ContractModel):
    """Run ``body`` once per element of ``items`` and collect one result per element, in element order."""

    id: ResourceName
    kind: Literal["forEach"]
    items: Expression
    # Defaults are materialized in the canonical document, like ``retry`` and ``decisions``.
    concurrency: PositiveInt = 1
    max_items: PositiveInt = Field(default=DEFAULT_LOOP_MAX_ITEMS, alias="maxItems")
    collect: Literal["all", "nonNull"] = "all"
    body: Branch


class CallWorkflowStep(ContractModel):
    """Run an exact published workflow version as a separate child run."""

    id: ResourceName
    kind: Literal["callWorkflow"]
    uses: VersionedReference
    input: Expression = Field(alias="with")
    mode: Literal["wait", "detach"] = "wait"
    on_failure: OmissionOnly[Literal["stop", "continue"]] = Field(
        default=None, alias="onFailure", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    business_key: OmissionOnly[Expression] = Field(
        default=None, alias="businessKey", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )

    @model_validator(mode="after")
    def failure_needs_wait(self) -> CallWorkflowStep:
        if self.mode == "detach" and self.on_failure is not None:
            raise ValueError("onFailure applies only when the call waits for its result")
        return self


type Step = Annotated[
    ActionStep
    | LLMStep
    | TransformStep
    | DecisionTableStep
    | SwitchStep
    | ParallelStep
    | WaitStep
    | SignalStep
    | HumanTaskStep
    | FailStep
    | ForEachStep
    | CallWorkflowStep,
    Field(discriminator="kind"),
]


type CallerNames = Annotated[
    list[ResourceName], Field(min_length=1, max_length=100, json_schema_extra={"uniqueItems": True})
]


class CallableSpec(ContractModel):
    """A workflow's opt-in to being called; omitted allowedCallers means any workflow in the project."""

    allowed_callers: OmissionOnly[CallerNames] = Field(
        default=None, alias="allowedCallers", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )

    @model_validator(mode="after")
    def unique_callers(self) -> CallableSpec:
        if self.allowed_callers is not None and len(set(self.allowed_callers)) != len(self.allowed_callers):
            raise ValueError("allowedCallers must be unique")
        return self


class WorkflowSpec(ContractModel):
    input_schema: JsonObjectData = Field(alias="inputSchema")
    output_schema: JsonObjectData = Field(alias="outputSchema")
    connections: dict[ResourceName, ConnectionRequirement] = Field(default_factory=dict)
    timeout_seconds: OmissionOnly[PositiveInt] = Field(
        default=None, alias="timeoutSeconds", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    llm_profiles: OmissionOnly[dict[ResourceName, LLMProfile]] = Field(
        default=None, alias="llmProfiles", max_length=32, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    steps: list[Step]
    output: Expression
    callable: OmissionOnly[CallableSpec] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class ConnectorAction(ContractModel):
    config_schema: OmissionOnly[JsonObjectData] = Field(
        default=None, alias="configSchema", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    input_schema: JsonObjectData = Field(alias="inputSchema")
    output_schema: JsonObjectData = Field(alias="outputSchema")
    side_effect: SideEffect = Field(alias="sideEffect")
    timeout_seconds: PositiveInt = Field(alias="timeoutSeconds")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class ConnectorCompatibility(ContractModel):
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")


class ConnectorLimits(ContractModel):
    """Published adapter I/O bounds, distinct from compiler operator budgets."""

    max_request_bytes: PositiveInt = Field(alias="maxRequestBytes")
    max_response_bytes: PositiveInt = Field(alias="maxResponseBytes")
    max_timeout_seconds: PositiveInt = Field(alias="maxTimeoutSeconds")


class ConnectorSpec(ContractModel):
    adapter: ResourceName
    config_schema: JsonObjectData = Field(alias="configSchema")
    auth_schema: JsonObjectData = Field(alias="authSchema")
    actions: dict[ResourceName, ConnectorAction] = Field(min_length=1)
    compatibility: ConnectorCompatibility
    limits: ConnectorLimits


class WorkflowDefinition(ContractModel):
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Workflow"]
    metadata: Metadata
    spec: WorkflowSpec


class ActionDefinition(ContractModel):
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Action"]
    metadata: Metadata
    spec: ActionSpec


class ConnectorDefinition(ContractModel):
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Connector"]
    metadata: Metadata
    spec: ConnectorSpec


class DecisionTableDefinition(ContractModel):
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["DecisionTable"]
    metadata: Metadata
    spec: DecisionTableSpec


type Definition = Annotated[
    WorkflowDefinition | ActionDefinition | ConnectorDefinition | DecisionTableDefinition, Field(discriminator="kind")
]

for _model in (
    ObjectExpression,
    ArrayExpression,
    Operation,
    OpExpression,
    Branch,
    SwitchCase,
    SwitchStep,
    ParallelStep,
    ForEachStep,
    CallWorkflowStep,
):
    _model.model_rebuild()

_definition_adapter: TypeAdapter[
    WorkflowDefinition | ActionDefinition | ConnectorDefinition | DecisionTableDefinition
] = TypeAdapter(Definition)


def load_definition(
    value: JsonObject,
) -> WorkflowDefinition | ActionDefinition | ConnectorDefinition | DecisionTableDefinition:
    return _definition_adapter.validate_python(value)
