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

"""Conservative output schemas and reference presence for semantic compilation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, cast

from firefly_weave.compiler.expressions import (
    DEFAULT_LIMITS,
    TEXT_OPERATORS,
    ExpressionFailure,
    _copy,
    _string_size,
    _ValueWork,
    array_index,
    count_expression_nodes,
    measure_value,
    pointer_segments,
)
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE, SchemaLimits
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonValue

DEFAULT_INFERENCE_LIMITS = SchemaLimits()


@dataclass(frozen=True)
class InferredType:
    """Schema guaranteed on success, with separate reference-presence evidence.

    Unknown means schema containment cannot be proved and needs runtime guards.
    Missing is reserved for provably absent references, never JSON null.
    """

    schema: JsonObject
    classification: Literal["known", "unknown", "missing"] = "known"
    may_be_missing: bool = False


@dataclass
class _InferenceBudget:
    limits: Limits
    max_work: int
    work: _ValueWork = field(init=False)

    def __post_init__(self) -> None:
        self.work = _ValueWork(self.max_work)

    def check(self, value: JsonValue, *, child: bool = False) -> int:
        measured_limits = self.limits.model_copy(update={"max_depth": self.limits.max_depth - int(child)})
        return measure_value(value, limits=measured_limits, work=self.work)

    def object(self, items: Iterable[tuple[str, JsonValue]]) -> JsonObject:
        self.work.charge("")
        size = 2
        nodes = 1
        if size > self.limits.max_payload_bytes:
            raise ExpressionFailure("RESOURCE_LIMIT")
        result: JsonObject = {}
        for key, value in items:
            before = self.work.remaining
            size += _string_size(key, self.limits, "") + 1 + self.check(value, child=True) + int(bool(result))
            nodes += before - self.work.remaining
            if size > self.limits.max_payload_bytes or nodes > self.limits.max_document_nodes:
                raise ExpressionFailure("RESOURCE_LIMIT")
            result[key] = value
        return result

    def array(self, items: Iterable[JsonValue]) -> list[JsonValue]:
        self.work.charge("")
        size = 2
        nodes = 1
        if size > self.limits.max_payload_bytes:
            raise ExpressionFailure("RESOURCE_LIMIT")
        result: list[JsonValue] = []
        for value in items:
            before = self.work.remaining
            size += self.check(value, child=True) + int(bool(result))
            nodes += before - self.work.remaining
            if size > self.limits.max_payload_bytes or nodes > self.limits.max_document_nodes:
                raise ExpressionFailure("RESOURCE_LIMIT")
            result.append(value)
        return result


def _unknown() -> InferredType:
    return InferredType({}, "unknown", True)


def _missing() -> InferredType:
    return InferredType({}, "missing", True)


def _literal(value: JsonValue, budget: _InferenceBudget) -> InferredType:
    kind = type(value)
    name = {
        type(None): "null",
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }[kind]
    return InferredType(budget.object((("type", name), ("const", value))))


def _has_relocated_ref(schema: JsonObject, budget: _InferenceBudget) -> bool:
    """Inspect schema locations only; const/enum/default contents are JSON data."""
    stack: list[JsonObject] = [schema]
    while stack:
        node = stack.pop()
        budget.work.charge("")
        if "$ref" in node or "$id" in node:
            return True
        children: list[Iterable[JsonValue]] = []
        for keyword in SCHEMA_MAPS:
            if keyword in node:
                children.append(cast(dict[str, JsonValue], node[keyword]).values())
        for keyword in SCHEMA_ARRAYS:
            if keyword in node:
                children.append(cast(list[JsonValue], node[keyword]))
        for keyword in SCHEMA_SINGLE:
            if type(node.get(keyword)) is dict:
                children.append((node[keyword],))
        for group in children:
            for child in group:
                if type(child) is dict:
                    if len(stack) >= budget.work.remaining:
                        raise ExpressionFailure("RESOURCE_LIMIT")
                    stack.append(child)
    return False


def _reference(pointer: str, context_schemas: dict[str, JsonObject], budget: _InferenceBudget) -> InferredType:
    segments = pointer_segments(pointer)
    if segments:
        if segments[0] not in context_schemas:
            return _missing()
        schema = context_schemas[segments[0]]
    else:
        schema = budget.object(
            (
                ("type", "object"),
                ("properties", cast(JsonValue, context_schemas)),
                ("required", budget.array(context_schemas)),
                ("additionalProperties", False),
            )
        )
    optional = False
    for segment in segments[1:]:
        if any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "not", "if", "dependentSchemas")):
            return _unknown()
        kind = schema.get("type")
        if kind == "object":
            if "patternProperties" in schema:
                return _unknown()
            properties = cast(dict[str, JsonValue], schema.get("properties", {}))
            if segment in properties:
                child = properties[segment]
                optional = optional or segment not in cast(list[str], schema.get("required", []))
            else:
                child = schema.get("additionalProperties", True)
                optional = True
        elif kind == "array":
            index = array_index(segment)
            if index is None:
                return _missing()
            maximum = schema.get("maxItems")
            if type(maximum) is int and index >= maximum:
                return _missing()
            prefix = cast(list[JsonValue], schema.get("prefixItems", []))
            child = prefix[index] if index < len(prefix) else schema.get("items", True)
            minimum = schema.get("minItems", 0)
            optional = optional or type(minimum) is not int or index >= minimum
        elif type(kind) is str and kind in {"string", "integer", "number", "boolean", "null"}:
            return _missing()
        else:
            return _unknown()
        if child is False:
            return _missing()
        if type(child) is not dict:
            return _unknown()
        schema = child
    unsupported = any(key in schema for key in ("$ref", "allOf", "anyOf", "oneOf", "not", "if", "dependentSchemas"))
    if _has_relocated_ref(schema, budget):
        return InferredType({}, "unknown", optional)
    budget.check(schema)
    return InferredType(schema, "known" if schema and not unsupported else "unknown", optional)


def _nonnull(inferred: InferredType, budget: _InferenceBudget) -> InferredType | None:
    if inferred.classification == "missing":
        return None
    schema = budget.object(inferred.schema.items())
    if "const" in schema:
        return None if schema["const"] is None else InferredType(schema, inferred.classification)
    if "enum" in schema:
        values = cast(list[JsonValue], schema["enum"])
        values = budget.array(value for value in values if value is not None)
        if not values:
            return None
        schema["enum"] = values
        return InferredType(schema, inferred.classification)
    kind = schema.get("type")
    if kind == "null":
        return None
    if type(kind) is list:
        kinds = budget.array(name for name in kind if name != "null")
        if not kinds:
            return None
        schema["type"] = kinds[0] if len(kinds) == 1 else kinds
    elif type(kind) is not str:
        return InferredType({}, "unknown")
    return InferredType(schema, inferred.classification)


def _infer(expression: JsonObject, context_schemas: dict[str, JsonObject], budget: _InferenceBudget) -> InferredType:
    budget.work.charge("")
    tag, body = next(iter(expression.items()))
    if tag == "literal":
        return _literal(body, budget)
    if tag == "ref":
        return _reference(cast(str, body), context_schemas, budget)
    if tag in {"object", "array"}:
        if tag == "object":
            fields = {
                key: _infer(child, context_schemas, budget) for key, child in cast(dict[str, JsonObject], body).items()
            }
            children = list(fields.values())
            properties = budget.object((key, value.schema) for key, value in fields.items())
            required = budget.array(key for key in fields)
            schema = budget.object(
                (
                    ("type", "object"),
                    ("properties", properties),
                    ("required", required),
                    ("additionalProperties", False),
                )
            )
        else:
            children = [_infer(child, context_schemas, budget) for child in cast(list[JsonObject], body)]
            items: list[tuple[str, JsonValue]] = [
                ("type", "array"),
                ("items", False),
                ("minItems", len(children)),
                ("maxItems", len(children)),
            ]
            if children:
                items.append(("prefixItems", budget.array(child.schema for child in children)))
            schema = budget.object(items)
        classification: Literal["known", "unknown", "missing"] = (
            "missing"
            if any(child.classification == "missing" for child in children)
            else "unknown"
            if any(child.classification == "unknown" for child in children)
            else "known"
        )
        return InferredType(schema, classification, any(child.may_be_missing for child in children))
    operation = cast(JsonObject, body)
    if operation["name"] in TEXT_OPERATORS:
        return InferredType(budget.object((("type", "string"),)))
    if operation["name"] != "coalesce":
        return InferredType(budget.object((("type", "boolean"),)))
    alternatives = []
    exhausted = True
    for arg in cast(list[JsonObject], operation["args"]):
        inferred = _infer(arg, context_schemas, budget)
        nonnull = _nonnull(inferred, budget)
        if nonnull is not None:
            alternatives.append(nonnull)
            kind = inferred.schema.get("type")
            excludes_null = (
                ("const" in inferred.schema and inferred.schema["const"] is not None)
                or ("enum" in inferred.schema and None not in cast(list[JsonValue], inferred.schema["enum"]))
                or (type(kind) is str and kind != "null")
                or (type(kind) is list and "null" not in kind)
            )
            if not inferred.may_be_missing and inferred.classification == "known" and excludes_null:
                exhausted = False
                break
    if exhausted:
        alternatives.append(InferredType({"type": "null"}))
    if any(item.classification == "unknown" for item in alternatives):
        return InferredType({}, "unknown")
    if len(alternatives) == 1:
        return alternatives[0]
    return InferredType(budget.object((("anyOf", budget.array(item.schema for item in alternatives)),)))


def infer_expression(
    expression: JsonObject,
    context_schemas: dict[str, JsonObject],
    *,
    limits: Limits = DEFAULT_LIMITS,
    schema_limits: SchemaLimits = DEFAULT_INFERENCE_LIMITS,
) -> InferredType:
    """Infer from already profile-validated schemas keyed by root context names.

    Local/bundled refs and composition remain unknown here; callers must retain runtime
    guards whenever stronger schema containment or operand typing is unproved.
    schema_limits independently bounds generated schema bytes/nodes/depth and
    cumulative expansion/copy work. Runtime limits still govern expressions/data.
    """
    count_expression_nodes(expression, limits=limits)
    schema_domain_limits = limits.model_copy(
        update={
            "max_depth": schema_limits.max_schema_depth,
            "max_payload_bytes": schema_limits.max_schema_bytes,
            "max_document_nodes": schema_limits.max_document_nodes,
        }
    )
    measure_value(cast(JsonObject, context_schemas), limits=schema_domain_limits, context=True)
    # Own the schema/literal data in the result, without executing arbitrary copies.
    generated_limits = schema_domain_limits.model_copy(update={"max_document_nodes": schema_limits.max_schema_nodes})
    budget = _InferenceBudget(generated_limits, schema_limits.max_expansion_nodes)
    result = _infer(expression, context_schemas, budget)
    # Charge the expanded tree before allocating its independently owned copy.
    budget.check(result.schema)
    return InferredType(cast(JsonObject, _copy(result.schema)), result.classification, result.may_be_missing)
