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


"""Pure decision evaluation with typed policies and one budget across all rows."""

from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from firefly_weave.compiler.expressions import (
    DEFAULT_LIMITS,
    TEXT_OPERATORS,
    ExpressionSession,
    count_expression_nodes,
    measure_value,
    pointer_segments,
)
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.definitions import DecisionTableSpec
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonValue

# Plain-language messages for decision codes whose generic compiler message would mislead.
DECISION_MESSAGES = {"WV-DECISION-OPERATOR": "Decision rules cannot use text operators yet."}


class DecisionFailure(ValueError):
    """Value-free failure with a stable pointer into the decision definition."""

    def __init__(self, code: str, path: str = "/spec") -> None:
        self.code = "WV-DECISION-" + code
        self.path = path
        super().__init__(self.code)


@dataclass(frozen=True)
class DecisionResult:
    output: JsonValue
    matched_rule_ids: tuple[str, ...]
    used_default: bool = False


def expression_roots(spec: JsonObject) -> list[tuple[JsonObject, str]]:
    roots = [
        (cast(JsonObject, rule[key]), f"/spec/rules/{index}/{key}")
        for index, rule in enumerate(cast(list[JsonObject], spec["rules"]))
        for key in ("when", "output")
    ]
    if "defaultOutput" in spec:
        roots.append((cast(JsonObject, spec["defaultOutput"]), "/spec/defaultOutput"))
    return roots


def validate_decision_expressions(spec: JsonObject, *, limits: Limits = DEFAULT_LIMITS) -> None:
    """Check all syntax, including unselected outputs, before evaluating any row."""
    count = 0
    for expression, path in expression_roots(spec):
        count = count_expression_nodes(expression, limits=limits, initial_count=count, path=path)
        pending = [(expression, path)]
        while pending:
            current, location = pending.pop()
            if "ref" in current:
                segments = pointer_segments(cast(str, current["ref"]))
                if not segments or segments[0] != "input":
                    raise DecisionFailure("REFERENCE", location + "/ref")
            elif "object" in current:
                from firefly_weave.compiler.source_map import pointer_child

                pending.extend(
                    (cast(JsonObject, value), pointer_child(location + "/object", key))
                    for key, value in cast(JsonObject, current["object"]).items()
                )
            elif "array" in current:
                pending.extend(
                    (cast(JsonObject, value), f"{location}/array/{index}")
                    for index, value in enumerate(cast(list[JsonValue], current["array"]))
                )
            elif "op" in current:
                operation = cast(JsonObject, current["op"])
                # Text operators stay out of decision rules, so decision table executables keep weave/ir-v1alpha3.
                if operation["name"] in TEXT_OPERATORS:
                    raise DecisionFailure("OPERATOR", location + "/op/name")
                pending.extend(
                    (cast(JsonObject, value), f"{location}/op/args/{index}")
                    for index, value in enumerate(cast(list[JsonValue], operation["args"]))
                )


def evaluate_decision_table(
    specification: JsonObject,
    value: JsonValue,
    *,
    bundle: dict[str, JsonObject] | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> DecisionResult:
    measure_value(specification, limits=limits.model_copy(update={"max_payload_bytes": limits.max_source_bytes}))
    try:
        contract = DecisionTableSpec.model_validate(specification)
    except ValidationError:
        raise DecisionFailure("CONTRACT") from None
    spec = cast(JsonObject, contract.model_dump(by_alias=True))
    if len(contract.rules) > limits.max_steps:
        raise DecisionFailure("RESOURCE_LIMIT", "/spec/rules")
    validate_decision_expressions(spec, limits=limits)
    schemas = bundle or {}
    schema_limits = SchemaLimits(
        max_payload_bytes=limits.max_payload_bytes,
        max_document_nodes=limits.max_document_nodes,
        max_depth=limits.max_depth,
        max_validation_work=limits.max_document_nodes,
    )
    if validate_payload(contract.input_schema, value, schemas, limits=schema_limits):
        raise DecisionFailure("INPUT", "/spec/inputSchema")
    session = ExpressionSession({"input": value}, limits=limits)
    matched: list[str] = []
    outputs: list[JsonValue] = []
    collected_bytes = 2
    selected: tuple[JsonObject, str] | None = None
    for index, rule in enumerate(cast(list[JsonObject], spec["rules"])):
        path = f"/spec/rules/{index}"
        predicate = session.evaluate(cast(JsonObject, rule["when"]), path=path + "/when")
        if type(predicate) is not bool:
            raise DecisionFailure("PREDICATE", path + "/when")
        if not predicate:
            continue
        matched.append(cast(str, rule["id"]))
        if contract.hit_policy == "unique" and len(matched) > 1:
            raise DecisionFailure("MULTIPLE_MATCHES", path + "/when")
        selected = cast(JsonObject, rule["output"]), path + "/output"
        if contract.hit_policy == "first":
            break
        if contract.hit_policy == "collect":
            output = session.evaluate(selected[0], path=selected[1])
            collected_bytes += session.measure(output, path=selected[1]) + int(bool(outputs))
            if collected_bytes > limits.max_payload_bytes:
                raise DecisionFailure("RESOURCE_LIMIT", selected[1])
            outputs.append(output)
    used_default = False
    if contract.hit_policy == "collect":
        result: JsonValue = outputs
    else:
        if selected is None:
            if "defaultOutput" not in spec:
                raise DecisionFailure("NO_MATCH")
            selected = cast(JsonObject, spec["defaultOutput"]), "/spec/defaultOutput"
            used_default = True
        result = session.evaluate(selected[0], path=selected[1])
    session.measure(result, path="/spec/outputSchema")
    if validate_payload(contract.output_schema, result, schemas, limits=schema_limits):
        raise DecisionFailure("OUTPUT", "/spec/outputSchema")
    return DecisionResult(result, tuple(matched), used_default)
