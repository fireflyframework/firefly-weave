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

"""Pure bounded classification. Omitted evidence is never an executable value."""

from typing import Literal, cast

from pydantic import Field

from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.compiler.schemas import _Budget, _Failure, _measure, _prepare, _resolve
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonData, JsonObject, JsonValue

SECRET_VALUE = "WV-SCHEMA-SECRET_VALUE"
_DEFAULT_LIMITS = SchemaLimits()


class Omission(ContractModel):
    path: str
    reason: Literal[
        "classified_secret", "classification_unavailable", "uncertain_derived", "confidential_text", "resource_limit"
    ]


class SafeProjection(ContractModel):
    available: bool
    value: JsonData = None
    omissions: list[Omission] = Field(default_factory=list)


class Classification:
    """Union of all possible schema branches; no applicability guesses or remote refs."""

    def __init__(self, schema: JsonObject, bundle: dict[str, JsonObject], limits: SchemaLimits) -> None:
        _prepare(schema, bundle, limits)
        self.documents = {"": schema, **bundle}
        self.limits = limits
        self.budget = _Budget(limits)

    def check(self, schema: JsonValue, value: JsonValue, document: str = "", depth: int = 0) -> None:
        self.budget.charge()
        if depth > self.limits.max_ref_depth:
            raise _Failure("RESOURCE_LIMIT")
        if not isinstance(schema, dict):
            return
        if schema.get("x-secret") is True or schema.get("writeOnly") is True:
            raise _Failure("SECRET_VALUE")
        if "$ref" in schema:
            name, _, target = _resolve(schema["$ref"], document, self.documents)
            self.check(cast(JsonValue, target), value, name, depth + 1)
        for keyword in ("allOf", "anyOf", "oneOf"):
            for child in cast(list[JsonValue], schema.get(keyword, [])):
                self.check(child, value, document, depth + 1)
        for keyword in ("if", "then", "else", "not"):
            if keyword in schema:
                self.check(schema[keyword], value, document, depth + 1)
        if isinstance(value, dict):
            for child in cast(JsonObject, schema.get("dependentSchemas", {})).values():
                self.check(child, value, document, depth + 1)
            props = cast(JsonObject, schema.get("properties", {}))
            patterns = cast(JsonObject, schema.get("patternProperties", {}))
            for key, item in value.items():
                self.budget.charge()
                matched = key in props
                if matched:
                    self.check(props[key], item, document, depth + 1)
                for pattern, child in patterns.items():
                    if self.budget.search(pattern, key):
                        matched = True
                        self.check(child, item, document, depth + 1)
                if not matched:
                    self.check(schema.get("additionalProperties", True), item, document, depth + 1)
                self.check(schema.get("propertyNames", True), key, document, depth + 1)
        if isinstance(value, list):
            prefix = cast(list[JsonValue], schema.get("prefixItems", []))
            for index, item in enumerate(value):
                self.check(
                    prefix[index] if index < len(prefix) else schema.get("items", True), item, document, depth + 1
                )
                self.check(schema.get("contains", True), item, document, depth + 1)

    def literals(self) -> None:
        type Location = tuple[JsonObject, str]

        def mapping(node: JsonObject, key: str) -> JsonObject:
            return cast(JsonObject, node.get(key, {}))

        def sequence(node: JsonObject, key: str) -> list[JsonValue]:
            return cast(list[JsonValue], node.get(key, []))

        def expanded(nodes: list[Location], depth: int = 0) -> list[Location]:
            if not nodes:
                return []
            if depth > self.limits.max_ref_depth:
                raise _Failure("RESOURCE_LIMIT")
            result: list[Location] = []
            for node, document in nodes:
                self.budget.charge()
                result.append((node, document))
                related: list[Location] = []
                if "$ref" in node:
                    name, _, target = _resolve(node["$ref"], document, self.documents)
                    if isinstance(target, dict):
                        related.append((target, name))
                for keyword in ("allOf", "anyOf", "oneOf"):
                    related.extend((child, document) for child in sequence(node, keyword) if isinstance(child, dict))
                related.extend(
                    (cast(JsonObject, node[key]), document)
                    for key in ("if", "then", "else", "not")
                    if isinstance(node.get(key), dict)
                )
                related.extend(
                    (child, document) for child in mapping(node, "dependentSchemas").values() if isinstance(child, dict)
                )
                result.extend(expanded(related, depth + 1))
            return result

        def walk(nodes: list[Location], inherited: bool = False, depth: int = 0) -> None:
            if not nodes:
                return
            if depth > self.limits.max_schema_depth:
                raise _Failure("RESOURCE_LIMIT")
            effective = expanded(nodes)
            marked = inherited or any(n.get("x-secret") is True or n.get("writeOnly") is True for n, _ in effective)
            for node, _ in effective:
                for keyword in ("default", "const", "examples", "enum"):
                    if keyword not in node:
                        continue
                    values = sequence(node, keyword) if keyword in {"examples", "enum"} else [node[keyword]]
                    for value in values:
                        self.budget.charge()
                        if marked:
                            raise _Failure("SECRET_VALUE")
                        for governing, document in effective:
                            self.check(governing, value, document)

            def add(target: list[Location], child: JsonValue, document: str) -> None:
                self.budget.charge()
                if isinstance(child, dict):
                    target.append((child, document))

            names = {key for node, _ in effective for key in mapping(node, "properties")}
            for key in sorted(names):
                children: list[Location] = []
                for node, document in effective:
                    props, patterns = mapping(node, "properties"), mapping(node, "patternProperties")
                    matched = key in props
                    if matched:
                        add(children, props[key], document)
                    for pattern, child in patterns.items():
                        if self.budget.search(pattern, key):
                            matched = True
                            add(children, child, document)
                    if not matched:
                        add(children, node.get("additionalProperties", True), document)
                walk(children, marked, depth + 1)

            # Unknown property names conservatively union pattern overlap; concrete names above
            # retain their exact property/pattern/additional governing schemas.
            anonymous: list[Location] = []
            property_names: list[Location] = []
            for node, document in effective:
                for child in mapping(node, "patternProperties").values():
                    add(anonymous, child, document)
                add(anonymous, node.get("additionalProperties", True), document)
                add(property_names, node.get("propertyNames", True), document)
                for child in mapping(node, "$defs").values():
                    if isinstance(child, dict):
                        walk([(child, document)], False, depth + 1)
            walk(anonymous, marked, depth + 1)
            walk(property_names, marked, depth + 1)
            maximum = max((len(sequence(node, "prefixItems")) for node, _ in effective), default=0)
            for index in range(maximum + 1):
                children = []
                for node, document in effective:
                    prefix = sequence(node, "prefixItems")
                    add(children, prefix[index] if index < len(prefix) else node.get("items", True), document)
                    add(children, node.get("contains", True), document)
                walk(children, marked, depth + 1)

        for name, root in self.documents.items():
            walk([(root, name)])


def classify(
    schema: JsonObject, value: JsonValue, bundle: dict[str, JsonObject], *, limits: SchemaLimits = _DEFAULT_LIMITS
) -> None:
    policy = Classification(schema, bundle, limits)
    _measure(
        value, byte_limit=limits.max_payload_bytes, node_limit=limits.max_document_nodes, depth_limit=limits.max_depth
    )
    policy.check(schema, value)


def project(
    value: JsonValue, schema: JsonObject, bundle: dict[str, JsonObject], *, limits: SchemaLimits = _DEFAULT_LIMITS
) -> SafeProjection:
    """Coarse root omission avoids disclosing payload-owned keys or array shape."""
    try:
        classify(schema, value, bundle, limits=limits)
    except (_Failure, RecursionError, ArithmeticError) as error:
        reason: Literal["classified_secret", "classification_unavailable"] = (
            "classified_secret"
            if isinstance(error, _Failure) and error.code == "SECRET_VALUE"
            else "classification_unavailable"
        )
        return SafeProjection(available=False, omissions=[Omission(path="", reason=reason)])
    return SafeProjection(available=True, value=value)


def has_markers(value: JsonValue) -> bool:
    """For already bounded artifacts only; conservative legacy derivation test."""
    if isinstance(value, dict):
        return (
            value.get("x-secret") is True
            or value.get("writeOnly") is True
            or any(has_markers(v) for v in value.values())
        )
    return isinstance(value, list) and any(has_markers(v) for v in value)


def project_operational_record(record: JsonObject) -> SafeProjection:
    """Default evidence projection for validated operational metadata, never payloads."""
    _measure(
        record,
        byte_limit=_DEFAULT_LIMITS.max_payload_bytes,
        node_limit=_DEFAULT_LIMITS.max_document_nodes,
        depth_limit=_DEFAULT_LIMITS.max_depth,
    )
    permitted = {
        "id",
        "run_id",
        "task_id",
        "completion_id",
        "receipt_id",
        "actor_id",
        "generation",
        "status",
        "code",
        "reason_code",
        "sequence",
        "accepted_at",
        "resolved_at",
        "kind",
    }
    value: JsonObject = {
        key: item for key, item in record.items() if key in permitted and not isinstance(item, (dict, list))
    }
    omitted = len(value) != len(record)
    return SafeProjection(
        available=not omitted,
        value=value,
        omissions=[Omission(path="/*", reason="confidential_text")] if omitted else [],
    )
