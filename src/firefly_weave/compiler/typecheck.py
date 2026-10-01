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

"""Bounded, deliberately incomplete schema containment for static data flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from firefly_weave.compiler.expressions import json_equal
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE, SchemaLimits
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.values import JsonObject, JsonValue

_DEFAULT_LIMITS = SchemaLimits()

Compatibility = Literal["compatible", "incompatible", "unknown"]
_ANNOTATIONS = {
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "x-secret",
    "$comment",
    "$schema",
    "$defs",
}


class TypeCheckLimit(ValueError):
    pass


def schema_types(schema: JsonObject) -> frozenset[str]:
    kind = schema.get("type")
    if type(kind) is str:
        return frozenset({kind})
    if type(kind) is list:
        return frozenset(cast(list[str], kind))
    return frozenset()


def _all(results: list[Compatibility]) -> Compatibility:
    return "incompatible" if "incompatible" in results else "unknown" if "unknown" in results else "compatible"


@dataclass
class _Containment:
    bundle: dict[str, JsonObject]
    limits: SchemaLimits
    work: int = 0

    def check(self, source: JsonValue, target: JsonValue, depth: int = 0) -> Compatibility:
        self.work += 1
        if self.work > self.limits.max_validation_work or depth > self.limits.max_ref_depth:
            raise TypeCheckLimit("Schema containment budget exceeded")
        if target is True or target == {} or source is False:
            return "compatible"
        if target is False:
            return "incompatible"
        if source is True:
            return "unknown"
        if type(source) is not dict or type(target) is not dict:
            return "unknown"
        if json_equal(source, target):
            return "compatible"
        # References are scoped to their complete schema roots. Never strip or rebase them.
        if any(k in source or k in target for k in ("$ref", "$id")):
            return "unknown"
        for keyword in ("const", "enum"):
            if keyword in source:
                candidates = [source["const"]] if keyword == "const" else cast(list[JsonValue], source["enum"])
                outcomes: list[Compatibility] = []
                for value in candidates:
                    self.work += 1
                    if self.work > self.limits.max_validation_work:
                        raise TypeCheckLimit("Schema containment budget exceeded")
                    if validate_payload(source, value, self.bundle, limits=self.limits):
                        return "unknown"
                    issues = validate_payload(target, value, self.bundle, limits=self.limits)
                    if any(d.code == "WV-SCHEMA-RESOURCE_LIMIT" for d in issues):
                        raise TypeCheckLimit("Schema validation budget exceeded")
                    outcomes.append("incompatible" if issues else "compatible")
                return _all(outcomes)
        if "anyOf" in source and set(source) - _ANNOTATIONS == {"anyOf"}:
            return _all([self.check(child, target, depth + 1) for child in cast(list[JsonValue], source["anyOf"])])
        if "anyOf" in target and set(target) - _ANNOTATIONS == {"anyOf"}:
            results = [self.check(source, child, depth + 1) for child in cast(list[JsonValue], target["anyOf"])]
            return (
                "compatible"
                if "compatible" in results
                else "incompatible"
                if all(r == "incompatible" for r in results)
                else "unknown"
            )
        if any(k in source or k in target for k in ("allOf", "anyOf", "oneOf", "not", "if", "dependentSchemas")):
            return "unknown"
        left, right = schema_types(source), schema_types(target)
        expanded_left = left | ({"integer"} if "number" in left else set())
        expanded_right = right | ({"integer"} if "number" in right else set())
        if left and right and expanded_left.isdisjoint(expanded_right):
            return "incompatible"
        outcomes = []
        if right and (not left or not expanded_left <= expanded_right):
            outcomes.append("unknown")
        handled = {"type"} | _ANNOTATIONS
        if left == {"object"} and right <= {"object"}:
            handled |= {"properties", "required", "additionalProperties"}
            # Unmodeled source constraints can forbid a property's presence, so its
            # disjoint subschema alone is not an incompatibility witness.
            if set(source) - handled:
                return "unknown"
            source_props = cast(dict[str, JsonValue], source.get("properties", {}))
            target_props = cast(dict[str, JsonValue], target.get("properties", {}))
            required = cast(list[str], source.get("required", []))
            for name in cast(list[str], target.get("required", [])):
                if name not in required:
                    if name not in source_props and source.get("additionalProperties") is False:
                        return "incompatible"
                    outcomes.append("unknown")
            for name, schema in source_props.items():
                outcomes.append(
                    self.check(schema, target_props.get(name, target.get("additionalProperties", True)), depth + 1)
                )
            for name, schema in target_props.items():
                if name not in source_props and source.get("additionalProperties", True) is not False:
                    outcomes.append(self.check(source.get("additionalProperties", True), schema, depth + 1))
            outcomes.append(
                self.check(
                    source.get("additionalProperties", True), target.get("additionalProperties", True), depth + 1
                )
            )
        elif left == {"array"} and right <= {"array"}:
            handled |= {"items", "prefixItems", "minItems", "maxItems"}
            # Profile-validated Draft cardinalities may be integral JSON floats.
            smin = cast(int | float, source.get("minItems", 0))
            tmin = cast(int | float, target.get("minItems", 0))
            smax = cast(int | float | None, source.get("maxItems"))
            tmax = cast(int | float | None, target.get("maxItems"))
            if smax is not None and smax < tmin:
                return "incompatible"
            if tmax is not None and smin > tmax:
                return "incompatible"
            if smin < tmin:
                outcomes.append("unknown")
            if tmax is not None and (smax is None or smax > tmax):
                outcomes.append("unknown")
            source_prefix = cast(list[JsonValue], source.get("prefixItems", []))
            target_prefix = cast(list[JsonValue], target.get("prefixItems", []))
            for i in range(max(len(source_prefix), len(target_prefix))):
                if smax is not None and i >= smax:
                    break
                a = source_prefix[i] if i < len(source_prefix) else source.get("items", True)
                b = target_prefix[i] if i < len(target_prefix) else target.get("items", True)
                outcomes.append(self.check(a, b, depth + 1))
            if smax is None or smax > max(len(source_prefix), len(target_prefix)):
                outcomes.append(self.check(source.get("items", True), target.get("items", True), depth + 1))
        for keyword in set(target) - handled:
            if keyword not in source or not json_equal(source[keyword], target[keyword]):
                outcomes.append("unknown")
        return _all(outcomes)


def check_compatibility(
    source: JsonObject,
    target: JsonObject,
    bundle: dict[str, JsonObject],
    *,
    limits: SchemaLimits = _DEFAULT_LIMITS,
) -> Compatibility:
    """Inputs must have passed validate_schema; unknown always requires a guard."""
    checker = _Containment(bundle, limits)
    if not json_equal(source, target):
        stack: list[JsonObject] = [source, target]
        while stack:
            schema = stack.pop()
            checker.work += 1
            if checker.work > limits.max_validation_work:
                raise TypeCheckLimit("Schema containment budget exceeded")
            if "$ref" in schema or "$id" in schema:
                return "unknown"
            for key in SCHEMA_MAPS:
                if key in schema:
                    stack.extend(child for child in cast(JsonObject, schema[key]).values() if type(child) is dict)
            for key in SCHEMA_ARRAYS:
                if key in schema:
                    stack.extend(child for child in cast(list[JsonValue], schema[key]) if type(child) is dict)
            for key in SCHEMA_SINGLE:
                if type(schema.get(key)) is dict:
                    stack.append(cast(JsonObject, schema[key]))
    return checker.check(source, target)
