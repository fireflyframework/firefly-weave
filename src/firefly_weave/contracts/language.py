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

"""The language manifest: the single source of step kinds, operators, workflow fields, features and limits.

Served by ``language.read`` and the Studio host's ``GET /studio/contracts/language`` and exported as
``language-manifest`` (language spec 14.1, contract C5). ``studio`` is ``ready`` once Studio ships a
descriptor for the entry and ``pending`` until then; Studio's schema coverage test compares only ready entries.
Every change that adds a step kind, operator or workflow field to the definition models adds its entry here.
"""

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import Field, model_validator

from firefly_weave.compiler.ir import COMPARISON_IR_VERSION, HUMAN_IR_VERSION, IR_VERSION
from firefly_weave.contracts.definitions import (
    ContractModel,
    OmissionOnly,
    PositiveInt,
    _is_absent,
    _omit_absent_default,
)
from firefly_weave.contracts.language_features import (
    ADVERTISED_FEATURES,
    DEFAULT_LOOP_MAX_ITEMS,
    KIND_FEATURES,
    MAX_CALL_DEPTH,
    MAX_LOOP_DEPTH,
    MAX_LOOP_ITEMS,
    MAX_RUN_ITERATIONS,
    OPERATOR_FEATURES,
    WORKFLOW_FIELD_FEATURES,
    LanguageFeature,
)
from firefly_weave.contracts.values import JsonObject, JsonObjectData

type JsonType = Literal["array", "boolean", "integer", "null", "number", "object", "string"]
type StudioMark = Literal["ready", "pending"]
type KindGroup = Literal["actions", "ai", "data", "flow", "wait", "human"]

MANIFEST_VERSION: Final = "weave/language-manifest-v1"
IR_VERSIONS: Final[tuple[str, ...]] = (IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION)
# The compiler's default parallel concurrency ceiling (compile_source's max_parallel_concurrency).
MAX_PARALLEL_CONCURRENCY: Final = 1000


class Arity(ContractModel):
    min: PositiveInt
    max: OmissionOnly[PositiveInt] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)


class ManifestOperator(ContractModel):
    name: str
    feature: OmissionOnly[LanguageFeature] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    arity: Arity
    # One list of accepted JSON types per operand position; with repeat_last the last list repeats.
    operand_types: list[list[JsonType]] = Field(min_length=1)
    repeat_last: bool = Field(default=False, exclude_if=lambda value: not value)
    # Accepted item types for operators that take a list.
    item_types: OmissionOnly[list[JsonType]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    result: JsonObjectData
    label: str = Field(min_length=1)
    studio: StudioMark


class ManifestWorkflowField(ContractModel):
    name: str
    feature: OmissionOnly[LanguageFeature] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    studio: StudioMark


class ManifestBlock(ContractModel):
    """A nested Branch at a fixed path: its steps are at ``path + "/steps"`` and its output at ``path + "/output"``."""

    name: str
    path: str = Field(pattern=r"^(?:/[A-Za-z]+)+$")
    label: str = Field(min_length=1)


class ManifestStepKind(ContractModel):
    kind: str
    feature: OmissionOnly[LanguageFeature] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    schema_ref: str = Field(pattern=r"^workflow\.schema\.json#/\$defs/[A-Za-z]+$")
    group: KindGroup
    label: str = Field(min_length=1)
    blocks: list[ManifestBlock]
    scope_roots: list[str]
    studio: StudioMark


class ManifestLimits(ContractModel):
    max_loop_items: PositiveInt
    default_loop_max_items: PositiveInt
    max_loop_depth: PositiveInt
    max_concurrency: PositiveInt
    max_run_iterations: PositiveInt
    max_call_depth: PositiveInt


class LanguageManifest(ContractModel):
    version: Literal["weave/language-manifest-v1"]
    language_version: Literal["weave/v1alpha1"]
    ir_versions: list[str] = Field(min_length=1)
    # The features the serving target runs (Capabilities.language_features), sorted and unique.
    features: list[LanguageFeature]
    limits: ManifestLimits
    operators: list[ManifestOperator]
    workflow_fields: list[ManifestWorkflowField]
    step_kinds: list[ManifestStepKind]

    @model_validator(mode="after")
    def unique_entries(self) -> "LanguageManifest":
        for names in (
            [entry.name for entry in self.operators],
            [entry.name for entry in self.workflow_fields],
            [entry.kind for entry in self.step_kinds],
        ):
            if len(set(names)) != len(names):
                raise ValueError("Manifest entries must be unique")
        if self.features != sorted(set(self.features)):
            raise ValueError("Manifest features must be sorted and unique")
        return self


_ANY: Final[list[JsonType]] = ["array", "boolean", "integer", "null", "number", "object", "string"]
_ORDERED: Final[list[JsonType]] = ["integer", "number", "string"]
_TEXT_PARTS: Final[list[JsonType]] = ["string", "integer", "number", "boolean"]
_BOOLEAN: Final[JsonObject] = {"type": "boolean"}
_STRING: Final[JsonObject] = {"type": "string"}


def _operator(
    name: str,
    arity: tuple[int, int | None],
    operand_types: list[list[JsonType]],
    result: JsonObject,
    label: str,
    *,
    repeat_last: bool = False,
    item_types: list[JsonType] | None = None,
    studio: StudioMark = "ready",
) -> ManifestOperator:
    value: JsonObject = {
        "name": name,
        "arity": {"min": arity[0]} if arity[1] is None else {"min": arity[0], "max": arity[1]},
        "operand_types": [list(types) for types in operand_types],
        "repeat_last": repeat_last,
        "result": result,
        "label": label,
        "studio": studio,
    }
    if name in OPERATOR_FEATURES:
        value["feature"] = OPERATOR_FEATURES[name]
    if item_types is not None:
        value["item_types"] = list(item_types)
    return ManifestOperator.model_validate(value)


OPERATORS: Final[tuple[ManifestOperator, ...]] = (
    _operator("eq", (2, 2), [_ANY, _ANY], _BOOLEAN, "Is"),
    _operator("ne", (2, 2), [_ANY, _ANY], _BOOLEAN, "Is not"),
    _operator("lt", (2, 2), [_ORDERED, _ORDERED], _BOOLEAN, "Is less than"),
    _operator("lte", (2, 2), [_ORDERED, _ORDERED], _BOOLEAN, "Is at most"),
    _operator("gt", (2, 2), [_ORDERED, _ORDERED], _BOOLEAN, "Is greater than"),
    _operator("gte", (2, 2), [_ORDERED, _ORDERED], _BOOLEAN, "Is at least"),
    _operator("and", (1, None), [["boolean"]], _BOOLEAN, "All of", repeat_last=True),
    _operator("or", (1, None), [["boolean"]], _BOOLEAN, "Any of", repeat_last=True),
    _operator("not", (1, 1), [["boolean"]], _BOOLEAN, "Not"),
    _operator("exists", (1, 1), [_ANY], _BOOLEAN, "Is present"),
    _operator("coalesce", (1, None), [_ANY], {}, "First available of", repeat_last=True),
    _operator("contains", (2, 2), [["array", "string"], _ANY], _BOOLEAN, "Contains"),
    _operator("notContains", (2, 2), [["array", "string"], _ANY], _BOOLEAN, "Does not contain"),
    _operator("in", (2, 2), [_ANY, ["array"]], _BOOLEAN, "Is in list"),
    _operator("notIn", (2, 2), [_ANY, ["array"]], _BOOLEAN, "Is not in list"),
    _operator("startsWith", (2, 2), [["string"], ["string"]], _BOOLEAN, "Starts with"),
    _operator("endsWith", (2, 2), [["string"], ["string"]], _BOOLEAN, "Ends with"),
    _operator("concat", (1, None), [_TEXT_PARTS], _STRING, "Combine text", repeat_last=True, studio="pending"),
    _operator("join", (2, 2), [["array"], ["string"]], _STRING, "Join list", item_types=_TEXT_PARTS, studio="pending"),
)

_WORKFLOW_FIELD_MARKS: Final[tuple[tuple[str, StudioMark], ...]] = (
    ("inputSchema", "ready"),
    ("outputSchema", "ready"),
    ("connections", "ready"),
    ("timeoutSeconds", "ready"),
    ("llmProfiles", "ready"),
    ("steps", "ready"),
    ("output", "ready"),
    ("callable", "pending"),
)
WORKFLOW_FIELDS: Final[tuple[ManifestWorkflowField, ...]] = tuple(
    ManifestWorkflowField.model_validate(
        {"name": name, "studio": studio}
        | ({"feature": WORKFLOW_FIELD_FEATURES[name]} if name in WORKFLOW_FIELD_FEATURES else {})
    )
    for name, studio in _WORKFLOW_FIELD_MARKS
)


def _kind(
    kind: str,
    model: str,
    group: KindGroup,
    label: str,
    *,
    blocks: Sequence[tuple[str, str, str]] = (),
    scope_roots: Sequence[str] = (),
    studio: StudioMark = "ready",
) -> ManifestStepKind:
    value: JsonObject = {
        "kind": kind,
        "schema_ref": f"workflow.schema.json#/$defs/{model}",
        "group": group,
        "label": label,
        "blocks": [{"name": name, "path": path, "label": text} for name, path, text in blocks],
        "scope_roots": list(scope_roots),
        "studio": studio,
    }
    if kind in KIND_FEATURES:
        value["feature"] = KIND_FEATURES[kind]
    return ManifestStepKind.model_validate(value)


# Lane A adds the agent entry with AgentStep: kind "agent", group "ai", label "AI agent", studio "pending".
STEP_KINDS: Final[tuple[ManifestStepKind, ...]] = (
    _kind("action", "ActionStep", "actions", "Action"),
    _kind("llm", "LLMStep", "ai", "AI task"),
    _kind("transform", "TransformStep", "data", "Transform"),
    _kind("decisionTable", "DecisionTableStep", "data", "Decision table"),
    _kind("switch", "SwitchStep", "flow", "Decision", blocks=[("default", "/default", "Otherwise")]),
    _kind("parallel", "ParallelStep", "flow", "Parallel"),
    _kind(
        "forEach",
        "ForEachStep",
        "flow",
        "Loop over items",
        blocks=[("body", "/body", "For each item")],
        scope_roots=["/item", "/index", "/loops"],
        studio="pending",
    ),
    _kind("callWorkflow", "CallWorkflowStep", "flow", "Call a workflow", studio="pending"),
    _kind("wait", "WaitStep", "wait", "Wait for time"),
    _kind("signal", "SignalStep", "wait", "Wait for signal"),
    _kind("humanTask", "HumanTaskStep", "human", "Human task"),
    _kind("fail", "FailStep", "flow", "Stop with error"),
)


def language_manifest(features: Sequence[LanguageFeature] = ADVERTISED_FEATURES) -> LanguageManifest:
    """The manifest of a target that runs ``features``; limits are this platform's language ceilings."""
    return LanguageManifest(
        version=MANIFEST_VERSION,
        language_version="weave/v1alpha1",
        ir_versions=list(IR_VERSIONS),
        features=sorted(set(features)),
        limits=ManifestLimits(
            max_loop_items=MAX_LOOP_ITEMS,
            default_loop_max_items=DEFAULT_LOOP_MAX_ITEMS,
            max_loop_depth=MAX_LOOP_DEPTH,
            max_concurrency=MAX_PARALLEL_CONCURRENCY,
            max_run_iterations=MAX_RUN_ITERATIONS,
            max_call_depth=MAX_CALL_DEPTH,
        ),
        operators=list(OPERATORS),
        workflow_fields=list(WORKFLOW_FIELDS),
        step_kinds=list(STEP_KINDS),
    )


def supported_step_kinds(features: Sequence[LanguageFeature] = ADVERTISED_FEATURES) -> list[str]:
    """The kinds a target running ``features`` executes: every kind without a feature, plus the listed ones."""
    return [entry.kind for entry in STEP_KINDS if entry.feature is None or entry.feature in features]
