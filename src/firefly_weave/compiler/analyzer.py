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

"""Pure catalog resolution and conservative structured control/data-flow analysis."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from firefly_weave.compiler.action_config import (
    ACTION_CONFIG_CODES,
    ActionConfigCheck,
    ActionConfigIssue,
    ActionConfigValidator,
)
from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot, FrozenDocument, ResourceKind
from firefly_weave.compiler.decision_tables import DecisionFailure, expression_roots, validate_decision_expressions
from firefly_weave.compiler.expression_types import InferredType, infer_expression
from firefly_weave.compiler.expressions import (
    COLLECTION_COMPARISONS,
    ExpressionFailure,
    count_expression_nodes,
    measure_value,
    pointer_segments,
)
from firefly_weave.compiler.llm import llm_action_valid, llm_input, llm_output_schema
from firefly_weave.compiler.parser import ParsedSource
from firefly_weave.compiler.schema_profile import DEFAULT_CONTRACT_LIMITS, SchemaLimits
from firefly_weave.compiler.schemas import validate_contract_payload, validate_payload, validate_schema
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.compiler.typecheck import TypeCheckLimit, check_compatibility, schema_types
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.diagnostics import Diagnostic, RelatedLocation, SourceRange
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonValue

_DEFAULT_LIMITS = Limits()
_DEFAULT_SCHEMA_LIMITS = SchemaLimits()
_POINTER = re.compile(r"^/spec(?:/(?:[^~/]|~[01])*)*$")
_MESSAGES = {
    "UNKNOWN_COMPATIBILITY": "Schema containment is unproved; runtime validation is required.",
    "REFERENCE_PRESENCE": "Reference presence is unproved; runtime validation is required.",
    "UNAVAILABLE_REFERENCE": "Referenced step does not dominate this expression in its lexical scope.",
    "CATALOG_PENDING": "This reference is checked against the project catalog when the definition is compiled.",
}


def _message(code: str) -> str:
    return _MESSAGES.get(code, "Definition violates the " + code.lower().replace("_", " ") + " contract.")


def reference_available(step_id: str, completed: frozenset[str]) -> bool:
    return step_id in completed


def severity_for_unknown(strict: bool) -> Literal["error", "warning"]:
    return "error" if strict else "warning"


@dataclass(frozen=True)
class RuntimeGuard:
    path: str
    purpose: str
    schema: FrozenDocument


@dataclass(frozen=True)
class AnalyzedBranch:
    name: str
    path: str
    steps: tuple[AnalyzedStep, ...]
    output: FrozenDocument
    output_schema: FrozenDocument
    completes: bool


@dataclass(frozen=True)
class AnalyzedStep:
    id: str
    kind: str
    path: str
    scope: tuple[str, ...]
    definition: FrozenDocument
    output_schema: FrozenDocument
    branches: tuple[AnalyzedBranch, ...] = ()
    completes: bool = True
    reachable: bool = True


@dataclass(frozen=True)
class AnalysisResult:
    kind: Literal["Workflow", "Action", "Connector", "DecisionTable"] | None
    definition: FrozenDocument | None
    resolved_resources: tuple[CatalogResource, ...]
    typed_graph: tuple[AnalyzedStep, ...]
    runtime_guards: tuple[RuntimeGuard, ...]
    source_hash: str
    _diagnostics: tuple[FrozenDocument, ...] = field(repr=False)
    error_count: int = 0
    omitted_count: int = 0
    schema_truncated: bool = False

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        # Diagnostic.related and suggestedEdit are shallow model fields; copy at the boundary.
        return tuple(Diagnostic.model_validate(item.value) for item in self._diagnostics)

    @property
    def ok(self) -> bool:
        return self.definition is not None and self.error_count == 0

    @property
    def truncated(self) -> bool:
        return self.omitted_count > 0 or self.schema_truncated


def _object(properties: dict[str, JsonValue]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _children(expression: JsonObject, path: str) -> list[tuple[JsonObject, str]]:
    if "object" in expression:
        return [
            (cast(JsonObject, child), pointer_child(path + "/object", key))
            for key, child in cast(JsonObject, expression["object"]).items()
        ]
    if "array" in expression:
        return [
            (cast(JsonObject, child), f"{path}/array/{i}")
            for i, child in enumerate(cast(list[JsonValue], expression["array"]))
        ]
    if "op" in expression:
        return [
            (cast(JsonObject, child), f"{path}/op/args/{i}")
            for i, child in enumerate(cast(list[JsonValue], cast(JsonObject, expression["op"])["args"]))
        ]
    return []


class _Analyzer:
    def __init__(
        self,
        source: ParsedSource,
        catalog: CatalogSnapshot,
        strict: bool,
        limits: Limits,
        schema_limits: SchemaLimits,
        contract_limits: SchemaLimits,
        concurrency: int,
        action_validators: Mapping[str, ActionConfigValidator] | None = None,
        *,
        catalog_absent: bool = False,
    ) -> None:
        self.source, self.catalog, self.strict = source, catalog, strict
        self.action_validators = dict(action_validators or {})
        # Offline authoring: every catalog reference is pending rather than unknown.
        self.catalog_absent = catalog_absent
        self.pending: list[Diagnostic] = []
        # Steps whose output is unconstrained only because the catalog is absent.
        self.pending_outputs: set[str] = set()
        self.limits, self.schema_limits, self.contract_limits = limits, schema_limits, contract_limits
        self.concurrency = concurrency
        self.issues: list[Diagnostic] = []
        self.error_count = self.omitted_count = 0
        self.schema_truncated = False
        self.guards: list[RuntimeGuard] = []
        self.resolved: dict[tuple[ResourceKind, str], CatalogResource] = {}
        self.checked: dict[tuple[ResourceKind, str], bool] = {}
        self.bundle = {key: item.value for key, item in catalog.schemas.items()}
        self.ids: dict[str, str] = {}
        self.signals: dict[str, str] = {}
        self.expression_count = self.step_count = 0
        self.input_schema: JsonObject = {}
        self.connections: JsonObject = {}
        self.llm_profiles: JsonObject = {}
        self.kind: Literal["Workflow", "Action", "Connector", "DecisionTable"] | None = None
        self.definition: FrozenDocument | None = None

    def location(self, path: str) -> SourceRange | None:
        while path not in self.source.locations and path:
            path = path.rsplit("/", 1)[0]
        return self.source.locations.get(path)

    @staticmethod
    def order(issue: Diagnostic) -> tuple[int, int, str, str]:
        source = issue.source
        return (source.line if source else 0, source.column if source else 0, issue.code, issue.path)

    def add(self, issue: Diagnostic, prefix: str = "") -> None:
        path = prefix + issue.path
        issue = issue.model_copy(update={"path": path, "source": self.location(path)})
        self.error_count += int(issue.severity == "error")
        self.schema_truncated |= issue.code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED"
        self.issues.append(issue)
        self.issues.sort(key=self.order)
        if len(self.issues) > self.limits.max_diagnostics:
            self.issues.pop()
            self.omitted_count += 1

    def issue(
        self,
        code: str,
        path: str,
        *,
        unknown: bool = False,
        stage: Literal["resolution", "semantic"] = "semantic",
        related: str | None = None,
    ) -> None:
        self.add(
            Diagnostic(
                code="WV-COMP-" + code,
                severity=severity_for_unknown(self.strict) if unknown else "error",
                stage=stage,
                path=path,
                message=_message(code),
                related=[] if related is None else [RelatedLocation(path=related, source=self.location(related))],
            )
        )

    def freeze_schema(self, schema: JsonObject) -> FrozenDocument:
        measure_value(
            schema,
            limits=Limits(
                max_payload_bytes=self.schema_limits.max_schema_bytes,
                max_document_nodes=self.schema_limits.max_schema_nodes,
                max_depth=self.schema_limits.max_schema_depth,
            ),
        )
        return FrozenDocument.from_value(schema)

    def guard(self, path: str, purpose: str, schema: JsonObject) -> None:
        self.guards.append(RuntimeGuard(path, purpose, self.freeze_schema(schema)))

    def schema(self, schema: JsonObject, path: str) -> bool:
        issues = validate_schema(schema, self.bundle, limits=self.schema_limits)
        for issue in issues:
            self.add(issue, path)
        return not issues

    def compatibility(self, source: JsonObject, target: JsonObject, path: str, *, quiet: bool = False) -> None:
        try:
            result = check_compatibility(source, target, self.bundle, limits=self.schema_limits)
        except TypeCheckLimit:
            self.issue("RESOURCE_LIMIT", path)
            return
        if result == "incompatible":
            self.issue("TYPE_MISMATCH", path)
        elif result == "unknown":
            if not quiet:
                self.issue("UNKNOWN_COMPATIBILITY", path, unknown=True)
            self.guard(path, "compatibility", target)

    def reads_pending(self, expression: JsonObject) -> bool:
        """Whether a value reads an output that only the absent catalog leaves unconstrained."""
        if not self.pending_outputs:
            return False
        stack = [expression]
        while stack:
            node = stack.pop()
            if "ref" in node:
                segments = pointer_segments(cast(str, node["ref"]))
                if not segments or (
                    segments[0] == "steps" and (len(segments) == 1 or segments[1] in self.pending_outputs)
                ):
                    return True
            stack.extend(child for child, _ in _children(node, ""))
        return False

    def resolve(self, kind: ResourceKind, reference: str, path: str) -> CatalogResource | None:
        resource = self.catalog.resolve(kind, reference)
        if resource is None and self.catalog_absent:
            # Without a catalog nothing can be resolved, so nothing is unknown yet; analysis continues
            # with an unconstrained output and complete compilation reports the real resolution.
            self.pending.append(
                Diagnostic(
                    code="WV-COMP-CATALOG_PENDING",
                    severity="info",
                    stage="resolution",
                    path=path,
                    message=_message("CATALOG_PENDING"),
                    source=self.location(path),
                )
            )
            return None
        if resource is None:
            code = {
                "Action": "UNKNOWN_ACTION",
                "Connector": "UNKNOWN_CONNECTOR",
                "TaskCapability": "UNKNOWN_TASK",
                "Adapter": "UNKNOWN_ADAPTER",
            }.get(kind, "UNKNOWN_RESOURCE")
            self.issue(code, path, stage="resolution")
            return None
        key = kind, reference
        self.resolved[key] = resource
        if key in self.checked:
            return resource if self.checked[key] else None
        self.checked[key] = False
        before = self.error_count
        value = resource.definition.value
        if kind in {"Action", "Connector", "DecisionTable"}:
            self.manifest(value, path, dependency=True)
        elif kind == "TaskCapability":
            self.schema(cast(JsonObject, value["inputSchema"]), path)
            self.schema(cast(JsonObject, value["outputSchema"]), path)
        self.checked[key] = before == self.error_count
        return resource if self.checked[key] else None

    def manifest(self, value: JsonObject, path: str, *, dependency: bool = False, partial: bool = False) -> None:
        spec = cast(JsonObject, value["spec"])
        base = path if dependency else path + "/spec"
        kind = value["kind"]
        schema_names = ("configSchema", "authSchema") if kind == "Connector" else ("inputSchema", "outputSchema")
        valid = True
        for name in schema_names:
            valid = self.schema(cast(JsonObject, spec[name]), base + "/" + name) and valid
        if kind == "DecisionTable":
            previous_input = self.input_schema
            self.input_schema = cast(JsonObject, spec["inputSchema"])
            try:
                validate_decision_expressions(spec, limits=self.limits)
                output_schema = cast(JsonObject, spec["outputSchema"])
                if spec["hitPolicy"] == "collect" and validate_payload(
                    output_schema, [], self.bundle, limits=self.schema_limits
                ):
                    self.issue("DECISION_EMPTY_OUTPUT", base + "/outputSchema")
                row_schema = (
                    cast(JsonObject, output_schema["items"]) if spec["hitPolicy"] == "collect" else output_schema
                )
                for expression, location in expression_roots(spec):
                    location = base + location.removeprefix("/spec")
                    self.count(expression, location)
                    target: JsonObject = {"type": "boolean"} if location.endswith("/when") else row_schema
                    self.expression(expression, location, {}, target)
            except (DecisionFailure, ExpressionFailure) as failure:
                self.add(
                    Diagnostic(
                        code=failure.code,
                        severity="error",
                        stage="semantic",
                        path=base + failure.path.removeprefix("/spec"),
                        message="Decision rule violates its expression contract.",
                    )
                )
            finally:
                self.input_schema = previous_input
            return
        if kind == "Connector":
            if not partial:
                self.resolve("Adapter", cast(str, spec["adapter"]), base + "/adapter")
            limits = cast(JsonObject, spec["limits"])
            for name, descriptor in cast(JsonObject, spec["actions"]).items():
                descriptor = cast(JsonObject, descriptor)
                descriptor_path = pointer_child(base + "/actions", name)
                for key in ("inputSchema", "outputSchema", *(["configSchema"] if "configSchema" in descriptor else [])):
                    self.schema(cast(JsonObject, descriptor[key]), descriptor_path + "/" + key)
                if cast(int, descriptor["timeoutSeconds"]) > cast(int, limits["maxTimeoutSeconds"]):
                    self.issue("TIMEOUT_CONTRACT", descriptor_path + "/timeoutSeconds")
            return
        if partial:
            return
        if "connection" in spec:
            requirement = cast(JsonObject, spec["connection"])
            self.resolve("Connector", cast(str, requirement["connector"]), base + "/connection/connector")
        implementation = cast(JsonObject, spec["implementation"])
        descriptor = None
        validator: ActionConfigValidator | None = None
        if implementation["kind"] == "worker":
            reference = f"{implementation['taskType']}@{implementation['taskVersion']}"
            task = self.resolve("TaskCapability", reference, base + "/implementation")
            if task is not None:
                descriptor = task.definition.value
        else:
            reference = cast(str, implementation["uses"])
            connector = self.resolve("Connector", reference, base + "/implementation/uses")
            if "routing" in spec:
                self.issue("ROUTING_CONTRACT", base + "/routing")
            if "connection" in spec and cast(JsonObject, spec["connection"])["connector"] != reference:
                self.issue("CONNECTION", base + "/connection")
            if connector is not None:
                connector_spec = cast(JsonObject, connector.definition.value["spec"])
                descriptor = cast(JsonObject, connector_spec["actions"]).get(cast(str, implementation["action"]))
                if descriptor is None:
                    self.issue("UNKNOWN_CONNECTOR_ACTION", base + "/implementation/action", stage="resolution")
                elif isinstance(descriptor, dict):
                    config_schema = cast(
                        JsonObject, descriptor.get("configSchema", {"type": "object", "maxProperties": 0})
                    )
                    if validate_payload(config_schema, implementation.get("config", {}), self.bundle):
                        self.issue("CONFIG_CONTRACT", base + "/implementation/config")
                    elif not dependency:
                        # Only the exact installed manifest selects trusted checks; published
                        # dependencies were checked when they were published.
                        validator = self.action_validators.get(connector.digest)
        if descriptor is None or not valid:
            return
        descriptor = cast(JsonObject, descriptor)
        self.compatibility(
            cast(JsonObject, spec["inputSchema"]), cast(JsonObject, descriptor["inputSchema"]), base + "/inputSchema"
        )
        self.compatibility(
            cast(JsonObject, descriptor["outputSchema"]), cast(JsonObject, spec["outputSchema"]), base + "/outputSchema"
        )
        if spec["sideEffect"] != descriptor["sideEffect"]:
            self.issue("SIDE_EFFECT_CONTRACT", base + "/sideEffect")
        if cast(int, spec["timeoutSeconds"]) > cast(int, descriptor["timeoutSeconds"]):
            self.issue("TIMEOUT_CONTRACT", base + "/timeoutSeconds")
        if validator is not None:
            self.action_config(validator, implementation, spec, base)

    def action_config(
        self, validator: ActionConfigValidator, implementation: JsonObject, spec: JsonObject, base: str
    ) -> None:
        config_path = base + "/implementation/config"
        check = ActionConfigCheck(
            action=cast(str, implementation["action"]),
            config=copy.deepcopy(implementation.get("config", {})),
            spec=copy.deepcopy(spec),
            schemas=copy.deepcopy(self.bundle),
        )
        try:
            issues = list(validator(check))
        except Exception:
            # Trusted descriptor code still fails closed with a stable, non-revealing diagnostic.
            self.issue("CONFIG_CONTRACT", config_path)
            return
        for item in issues[: self.limits.max_diagnostics]:
            if not isinstance(item, ActionConfigIssue):
                self.issue("CONFIG_CONTRACT", config_path)
                continue
            valid_path = isinstance(item.path, str) and _POINTER.fullmatch(item.path) is not None
            message = item.message if isinstance(item.message, str) and item.message.strip() else ""
            self.add(
                Diagnostic(
                    code="WV-COMP-" + (item.code if item.code in ACTION_CONFIG_CODES else "CONFIG_CONTRACT"),
                    severity="warning" if item.severity == "warning" else "error",
                    stage="semantic",
                    path=base + item.path.removeprefix("/spec") if valid_path else config_path,
                    message=message[:1000] or "Definition violates the connector configuration contract.",
                )
            )

    def preflight(self, steps: list[JsonObject], path: str, depth: int = 1) -> bool:
        for i, step in enumerate(steps):
            step_path = f"{path}/{i}"
            self.step_count += 1
            if self.step_count > self.limits.max_steps or depth > self.limits.max_depth:
                self.issue("STEP_LIMIT" if self.step_count > self.limits.max_steps else "DEPTH_LIMIT", step_path)
                return False
            identifier = cast(str, step["id"])
            if identifier in self.ids:
                self.issue("DUPLICATE_ID", step_path + "/id", related=self.ids[identifier])
            else:
                self.ids[identifier] = step_path + "/id"
            kind = step["kind"]
            if kind == "signal":
                name = cast(str, step["name"])
                if name in self.signals:
                    self.issue("DUPLICATE_SIGNAL", step_path + "/name", related=self.signals[name])
                else:
                    self.signals[name] = step_path + "/name"
                self.schema(cast(JsonObject, step["payloadSchema"]), step_path + "/payloadSchema")
            if kind == "humanTask":
                self.schema(cast(JsonObject, step["formSchema"]), step_path + "/formSchema")
                self.count(cast(JsonObject, step["title"]), step_path + "/title")
                self.count(cast(JsonObject, step["context"]), step_path + "/context")
            if kind == "llm":
                self.count(cast(JsonObject, step["prompt"]), step_path + "/prompt")
                self.count(cast(JsonObject, step["context"]), step_path + "/context")
            if kind in {"action", "decisionTable", "transform"}:
                key = "value" if kind == "transform" else "with"
                self.count(cast(JsonObject, step[key]), step_path + "/" + key)
            for _, child, child_path in self.branch_values(step, step_path):
                if "when" in child:
                    self.count(cast(JsonObject, child["when"]), child_path + "/when")
                if not self.preflight(cast(list[JsonObject], child["steps"]), child_path + "/steps", depth + 1):
                    return False
                self.count(cast(JsonObject, child["output"]), child_path + "/output")
        return True

    def count(self, expression: JsonObject, path: str) -> None:
        self.expression_count = count_expression_nodes(
            expression, limits=self.limits, initial_count=self.expression_count, path=path
        )

    @staticmethod
    def branch_values(step: JsonObject, path: str) -> list[tuple[str, JsonObject, str]]:
        if step["kind"] == "switch":
            return [
                (f"case:{i}", case, f"{path}/cases/{i}") for i, case in enumerate(cast(list[JsonObject], step["cases"]))
            ] + [("default", cast(JsonObject, step["default"]), path + "/default")]
        if step["kind"] == "parallel":
            return [
                (name, cast(JsonObject, branch), pointer_child(path + "/branches", name))
                for name, branch in sorted(cast(JsonObject, step["branches"]).items())
            ]
        return []

    def infer(self, expression: JsonObject, visible: dict[str, JsonObject]) -> InferredType:
        requested: set[str] = set()
        include_input = include_all = include_steps = False
        stack = [expression]
        while stack:
            node = stack.pop()
            if "ref" in node:
                segments = pointer_segments(cast(str, node["ref"]))
                if not segments:
                    include_input = include_all = include_steps = True
                elif segments[0] == "input":
                    include_input = True
                elif segments[0] == "steps":
                    include_steps = True
                    if len(segments) == 1:
                        include_all = True
                    else:
                        requested.add(segments[1])
            stack.extend(child for child, _ in _children(node, ""))
        context: dict[str, JsonObject] = {}
        if include_input:
            context["input"] = self.input_schema
        if include_steps:
            context["steps"] = _object(
                {key: _object({"output": schema}) for key, schema in visible.items() if include_all or key in requested}
            )
        return infer_expression(expression, context, limits=self.limits, schema_limits=self.schema_limits)

    def expression(
        self, expression: JsonObject, path: str, visible: dict[str, JsonObject], target: JsonObject | None = None
    ) -> JsonObject:
        try:
            # Dominance is a syntactic requirement, including lazy operands.
            stack = [(expression, path)]
            while stack:
                node, location = stack.pop()
                if "ref" in node:
                    segments = pointer_segments(cast(str, node["ref"]))
                    if (
                        len(segments) >= 2
                        and segments[0] == "steps"
                        and not reference_available(segments[1], frozenset(visible))
                    ):
                        self.issue("UNAVAILABLE_REFERENCE", location + "/ref")
                stack.extend(_children(node, location))
            self.operands(expression, path, visible)
            result = self.infer(expression, visible)
            if target is not None:
                self.compatibility(result.schema, target, path, quiet=self.reads_pending(expression))
            return result.schema
        except ExpressionFailure as failure:
            self.add(
                Diagnostic(
                    code=failure.code,
                    severity="error",
                    stage="semantic",
                    path=path + failure.path,
                    message="Expression violates its static or resource contract.",
                )
            )
            return {}

    def operands(
        self, expression: JsonObject, path: str, visible: dict[str, JsonObject], *, handled: bool = False
    ) -> None:
        if "ref" in expression:
            result = self.infer(expression, visible)
            segments = pointer_segments(cast(str, expression["ref"]))
            unavailable = len(segments) >= 2 and segments[0] == "steps" and segments[1] not in visible
            if unavailable:
                return
            if not handled and len(segments) > 3 and segments[0] == "steps" and segments[2] == "output":
                variants = [visible[segments[1]]]
                while variants:
                    variant = variants.pop()
                    if set(variant) == {"anyOf"}:
                        variants.extend(cast(list[JsonObject], variant["anyOf"]))
                    elif self.infer(expression, {segments[1]: variant}).classification == "missing":
                        self.issue("INCOMPLETE_BRANCH_OUTPUT", path)
                        return
            if result.classification == "missing" and not handled:
                self.issue("MISSING_REFERENCE", path)
            elif result.may_be_missing and not handled:
                if not self.reads_pending(expression):
                    self.issue("REFERENCE_PRESENCE", path, unknown=True)
                self.guard(path, "reference_presence", result.schema)
            return
        children = _children(expression, path)
        if "op" not in expression:
            for child, child_path in children:
                self.operands(child, child_path, visible)
            return
        name = cast(JsonObject, expression["op"])["name"]
        inferred = []
        for child, child_path in children:
            self.operands(child, child_path, visible, handled=name in {"exists", "coalesce"} and "ref" in child)
            result = self.infer(child, visible)
            inferred.append(result)
            if name in {"and", "or", "not"}:
                self.compatibility(result.schema, {"type": "boolean"}, child_path, quiet=self.reads_pending(child))
            if name == "coalesce" and result.classification == "known" and not result.may_be_missing:
                types = schema_types(result.schema)
                if ("const" in result.schema and result.schema["const"] is not None) or (types and "null" not in types):
                    break
            if name == "coalesce" and self.reads_pending(child):
                # The catalog may prove this operand present and non-null, leaving the remaining
                # fallbacks unreachable; complete compilation checks them against the real output.
                break
            if name in {"and", "or"} and "literal" in child and child["literal"] is (name == "or"):
                break
        if name in {"lt", "lte", "gt", "gte"}:
            kinds = [schema_types(item.schema) for item in inferred]
            numeric = {"number", "integer"}
            valid = all(k and k <= numeric for k in kinds) or all(k == {"string"} for k in kinds)
            if not valid:
                definite = all(k for k in kinds) and (
                    any(k.isdisjoint(numeric | {"string"}) for k in kinds)
                    or (kinds[0] <= numeric and kinds[1] == {"string"})
                    or (kinds[1] <= numeric and kinds[0] == {"string"})
                )
                if definite or not any(self.reads_pending(child) for child, _ in children):
                    self.issue("TYPE_MISMATCH" if definite else "UNKNOWN_COMPATIBILITY", path, unknown=not definite)
                if not definite:
                    self.guard(path, "operator_operands", {})
        elif name in COLLECTION_COMPARISONS:
            # Membership accepts every JSON needle; only the container's type constrains it.
            domain = frozenset({"string", "array", "object", "number", "integer", "boolean", "null"})
            kinds = [schema_types(item.schema) or domain for item in inferred]
            allowed = [
                right == "array"
                if name in {"in", "notIn"}
                else left == "array" or left == right == "string"
                if name in {"contains", "notContains"}
                else left == right == "string"
                for left in kinds[0]
                for right in kinds[1]
            ]
            if not all(allowed):
                definite = not any(allowed)
                if definite or not any(self.reads_pending(child) for child, _ in children):
                    self.issue("TYPE_MISMATCH" if definite else "UNKNOWN_COMPATIBILITY", path, unknown=not definite)
                if not definite:
                    self.guard(path, "operator_operands", {})

    def connection(self, step: JsonObject, action: JsonObject, path: str) -> None:
        requirement = cast(JsonObject | None, action.get("connection"))
        name = step.get("connection")
        if name is None:
            if requirement is not None and requirement.get("required", True):
                self.issue("CONNECTION", path)
            return
        slot = cast(JsonObject | None, self.connections.get(cast(str, name)))
        if (
            slot is None
            or requirement is None
            or slot["connector"] != requirement["connector"]
            or (requirement.get("required", True) and not slot.get("required", True))
        ):
            self.issue("CONNECTION", path)

    def block(
        self, steps: list[JsonObject], path: str, incoming: dict[str, JsonObject], scope: tuple[str, ...]
    ) -> tuple[tuple[AnalyzedStep, ...], dict[str, JsonObject], bool]:
        visible = dict(incoming)
        nodes: list[AnalyzedStep] = []
        reachable = True
        for i, step in enumerate(steps):
            location = f"{path}/{i}"
            kind, identifier = cast(str, step["kind"]), cast(str, step["id"])
            output: JsonObject = {"type": "null"}
            branches: list[AnalyzedBranch] = []
            completes = kind != "fail"
            if kind == "transform":
                output = self.expression(cast(JsonObject, step["value"]), location + "/value", visible)
                self.guard(location + "/value", "transform_output", output)
                if self.reads_pending(cast(JsonObject, step["value"])):
                    self.pending_outputs.add(identifier)
            elif kind == "llm":
                profile = cast(JsonObject | None, self.llm_profiles.get(cast(str, step["profile"])))
                if profile is None:
                    self.issue("LLM_PROFILE", location + "/profile")
                self.expression(
                    cast(JsonObject, step["prompt"]), location + "/prompt", visible, {"type": "string", "minLength": 1}
                )
                self.expression(cast(JsonObject, step["context"]), location + "/context", visible)
                resource = self.resolve("Action", cast(str, step["uses"]), location + "/uses")
                action_spec = cast(JsonObject, resource.definition.value["spec"]) if resource else None
                if profile is not None:
                    output = llm_output_schema(profile)
                    self.guard(location, "action_output", output)
                    if action_spec is not None:
                        if not llm_action_valid(action_spec, profile):
                            self.issue("LLM_ACTION", location + "/uses")
                        self.connection(step, action_spec, location + "/connection")
                        self.expression(
                            llm_input(step, profile),
                            location + "/with",
                            visible,
                            cast(JsonObject, action_spec["inputSchema"]),
                        )
                if action_spec is None and step["connection"] not in self.connections:
                    self.issue("CONNECTION", location + "/connection")
            elif kind == "action":
                resource = self.resolve("Action", cast(str, step["uses"]), location + "/uses")
                action_spec = cast(JsonObject, resource.definition.value["spec"]) if resource else None
                target = cast(JsonObject, action_spec["inputSchema"]) if action_spec else None
                self.expression(cast(JsonObject, step["with"]), location + "/with", visible, target)
                if action_spec is not None:
                    self.connection(step, action_spec, location + "/connection")
                    output = cast(JsonObject, action_spec["outputSchema"])
                    self.guard(location + "/with", "action_input", cast(JsonObject, target))
                    self.guard(location, "action_output", output)
                else:
                    output = {}
                    if self.catalog_absent:
                        self.pending_outputs.add(identifier)
                        slot = step.get("connection")
                        if slot is not None and slot not in self.connections:
                            # An undeclared slot fails complete compilation whatever the Action requires.
                            self.issue("CONNECTION", location + "/connection")
            elif kind == "decisionTable":
                resource = self.resolve("DecisionTable", cast(str, step["uses"]), location + "/uses")
                table_spec = cast(JsonObject, resource.definition.value["spec"]) if resource else None
                target = cast(JsonObject, table_spec["inputSchema"]) if table_spec else None
                self.expression(cast(JsonObject, step["with"]), location + "/with", visible, target)
                output = cast(JsonObject, table_spec["outputSchema"]) if table_spec else {}
                if table_spec is not None:
                    self.guard(location + "/with", "decision_input", cast(JsonObject, target))
                    self.guard(location, "decision_output", output)
                elif self.catalog_absent:
                    self.pending_outputs.add(identifier)
            elif kind == "humanTask":
                self.expression(
                    cast(JsonObject, step["title"]),
                    location + "/title",
                    visible,
                    {"type": "string", "minLength": 1, "maxLength": 512},
                )
                self.expression(cast(JsonObject, step["context"]), location + "/context", visible, {"type": "object"})
                output = {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": step["decisions"]},
                        "data": step["formSchema"],
                    },
                    "required": ["decision", "data"],
                    "additionalProperties": False,
                }
                self.guard(location, "human_output", output)
            elif kind == "signal":
                output = cast(JsonObject, step["payloadSchema"])
                self.guard(location, "signal_payload", output)
            elif kind in {"switch", "parallel"}:
                if kind == "parallel" and cast(int, step["concurrency"]) > self.concurrency:
                    self.issue("CONCURRENCY_LIMIT", location + "/concurrency")
                for name, branch, branch_path in self.branch_values(step, location):
                    if "when" in branch:
                        self.expression(
                            cast(JsonObject, branch["when"]), branch_path + "/when", visible, {"type": "boolean"}
                        )
                    children, branch_visible, branch_completes = self.block(
                        cast(list[JsonObject], branch["steps"]),
                        branch_path + "/steps",
                        visible,
                        scope + (identifier, name),
                    )
                    expression = cast(JsonObject, branch["output"])
                    branch_schema = (
                        self.expression(expression, branch_path + "/output", branch_visible) if branch_completes else {}
                    )
                    self.guard(branch_path + "/output", "branch_output", branch_schema)
                    if branch_completes and self.reads_pending(expression):
                        self.pending_outputs.add(identifier)
                    branches.append(
                        AnalyzedBranch(
                            name,
                            branch_path,
                            children,
                            FrozenDocument.from_value(expression),
                            self.freeze_schema(branch_schema),
                            branch_completes,
                        )
                    )
                if kind == "parallel":
                    output = _object({b.name: b.output_schema.value for b in branches})
                    completes = all(b.completes for b in branches)
                else:
                    alternatives = [b.output_schema.value for b in branches if b.completes]
                    output = (
                        alternatives[0]
                        if len(alternatives) == 1
                        else {"anyOf": cast(list[JsonValue], alternatives)}
                        if alternatives
                        else {}
                    )
                    completes = bool(alternatives)
            nodes.append(
                AnalyzedStep(
                    identifier,
                    kind,
                    location,
                    scope,
                    FrozenDocument.from_value(step),
                    self.freeze_schema(output),
                    tuple(branches),
                    completes,
                    reachable,
                )
            )
            if reachable and completes:
                visible[identifier] = output
            reachable = reachable and completes
        return tuple(nodes), visible, reachable

    def settle_pending(self) -> None:
        """Catalog-pending notes are the lowest priority: they only fill capacity that findings left.

        Overflowing notes are counted as omitted. A slot is reserved for the truncation marker so
        it never replaces a finding, unless findings alone already fill the whole budget.
        """
        pending, self.pending = self.pending, []
        room = self.limits.max_diagnostics - len(self.issues)
        if len(pending) > room:
            kept = max(room - 1, 0)
            self.omitted_count += len(pending) - kept
            pending = pending[:kept]
        self.issues.extend(pending)
        self.issues.sort(key=self.order)

    def truncation_marker(self) -> Diagnostic:
        return Diagnostic(
            code="WV-COMP-DIAGNOSTICS_TRUNCATED",
            severity="error" if self.error_count else "warning",
            stage="semantic",
            path="",
            message=f"{self.omitted_count} diagnostics omitted.",
        )

    def finish(
        self,
        kind: Literal["Workflow", "Action", "Connector", "DecisionTable"] | None,
        definition: FrozenDocument | None,
        graph: tuple[AnalyzedStep, ...] = (),
    ) -> AnalysisResult:
        self.settle_pending()
        issues = list(self.issues)
        if self.omitted_count:
            if len(issues) < self.limits.max_diagnostics:
                # Only catalog-pending overflow leaves room: the marker takes the reserved slot.
                issues.append(self.truncation_marker())
            else:
                self.omitted_count += 1
                issues[-1] = self.truncation_marker()
        return AnalysisResult(
            kind,
            definition,
            tuple(self.resolved[key] for key in sorted(self.resolved)),
            graph,
            tuple(self.guards),
            self.source.source_hash,
            tuple(FrozenDocument.from_value(d.model_dump(by_alias=True)) for d in issues),
            self.error_count,
            self.omitted_count,
            self.schema_truncated,
        )

    def run(self, *, partial: bool = False) -> AnalysisResult:
        kind = self.source.value.get("kind")
        contract = (
            kind.lower()
            if isinstance(kind, str) and kind in {"Workflow", "Action", "Connector", "DecisionTable"}
            else "definition"
        )
        issues = validate_contract_payload(contract, self.source.value, limits=self.contract_limits)
        for issue in issues:
            self.add(issue)
        if issues:
            return self.finish(None, None)
        model = load_definition(self.source.value)
        value = model.model_dump(by_alias=True)
        definition = FrozenDocument.from_value(value)
        self.kind, self.definition = model.kind, definition
        existing = self.catalog.resolve(model.kind, f"{model.metadata.name}@{model.metadata.version}")
        if existing is not None and existing.digest != definition.digest:
            self.issue("IMMUTABLE_VERSION", "/metadata/version", stage="resolution")
        # A3 preparation validates the entire supplied local bundle; lock every resource it consumes.
        for name in self.catalog.schemas:
            resource = self.catalog.resolve("Schema", name)
            if resource is not None:
                self.resolved[("Schema", name)] = resource
        if model.kind != "Workflow":
            self.manifest(value, "", partial=partial)
            return self.finish(model.kind, definition)
        spec = cast(JsonObject, value["spec"])
        self.input_schema = cast(JsonObject, spec["inputSchema"])
        self.connections = cast(JsonObject, spec["connections"])
        self.llm_profiles = cast(JsonObject, spec.get("llmProfiles", {}))
        for name, profile in self.llm_profiles.items():
            self.schema(
                cast(JsonObject, cast(JsonObject, profile)["outputSchema"]),
                pointer_child("/spec/llmProfiles", name) + "/outputSchema",
            )
        self.schema(self.input_schema, "/spec/inputSchema")
        self.schema(cast(JsonObject, spec["outputSchema"]), "/spec/outputSchema")
        try:
            valid = self.preflight(cast(list[JsonObject], spec["steps"]), "/spec/steps")
            self.count(cast(JsonObject, spec["output"]), "/spec/output")
        except ExpressionFailure as failure:
            self.add(
                Diagnostic(
                    code=failure.code,
                    severity="error",
                    stage="semantic",
                    path=failure.path,
                    message="Aggregate expression or literal budget exceeded.",
                )
            )
            return self.finish(model.kind, definition)
        if not valid or self.error_count:
            return self.finish(model.kind, definition)
        if partial:
            return self.finish(model.kind, definition)
        self.guard("/spec/inputSchema", "workflow_input", self.input_schema)
        for name, slot in sorted(self.connections.items()):
            self.resolve(
                "Connector", cast(str, cast(JsonObject, slot)["connector"]), pointer_child("/spec/connections", name)
            )
        graph, visible, completes = self.block(cast(list[JsonObject], spec["steps"]), "/spec/steps", {}, ())
        if completes:
            self.expression(
                cast(JsonObject, spec["output"]), "/spec/output", visible, cast(JsonObject, spec["outputSchema"])
            )
        self.guard("/spec/output", "workflow_output", cast(JsonObject, spec["outputSchema"]))
        return self.finish(model.kind, definition, graph)


def analyze(
    source: ParsedSource,
    catalog: CatalogSnapshot,
    *,
    strict: bool = False,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
    max_parallel_concurrency: int = 1000,
    action_validators: Mapping[str, ActionConfigValidator] | None = None,
) -> AnalysisResult:
    """Full definition analysis. A successful result is not an activation readiness claim."""
    if type(max_parallel_concurrency) is not int or max_parallel_concurrency < 1:
        raise ValueError("max_parallel_concurrency must be a positive integer")
    analyzer = _Analyzer(
        source,
        catalog,
        strict,
        limits,
        schema_limits,
        contract_limits,
        max_parallel_concurrency,
        action_validators,
    )
    try:
        return analyzer.run()
    except (ExpressionFailure, RecursionError) as failure:
        analyzer.issue("RESOURCE_LIMIT", failure.path if isinstance(failure, ExpressionFailure) else "")
        return analyzer.finish(analyzer.kind, analyzer.definition)


def analyze_authoring(
    source: ParsedSource,
    *,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
) -> AnalysisResult:
    """Offline authoring analysis: complete flow, dominance and type checks without a catalog.

    Every Action, Connector, task capability and adapter reference reports
    ``WV-COMP-CATALOG_PENDING`` at info severity instead of an unknown-resource error, and its
    output stays unconstrained, so references into it never produce type errors. Callers must
    not lower the result: it is an authoring aid, not a compilation.
    """
    analyzer = _Analyzer(
        source, CatalogSnapshot.empty(), False, limits, schema_limits, contract_limits, 1000, catalog_absent=True
    )
    try:
        return analyzer.run()
    except (ExpressionFailure, RecursionError) as failure:
        analyzer.issue("RESOURCE_LIMIT", failure.path if isinstance(failure, ExpressionFailure) else "")
        return analyzer.finish(analyzer.kind, analyzer.definition)


def analyze_partial(
    source: ParsedSource,
    *,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
) -> AnalysisResult:
    """Authoring-only outer/embedded schema checks; no resolution or flow analysis."""
    analyzer = _Analyzer(source, CatalogSnapshot.empty(), False, limits, schema_limits, contract_limits, 1000)
    try:
        return analyzer.run(partial=True)
    except (ExpressionFailure, RecursionError) as failure:
        analyzer.issue("RESOURCE_LIMIT", failure.path if isinstance(failure, ExpressionFailure) else "")
        return analyzer.finish(analyzer.kind, analyzer.definition)
