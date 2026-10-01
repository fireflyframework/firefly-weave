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

"""Secret admission for authoring, including incomplete drafts; no graph execution."""

from collections.abc import Iterator
from typing import cast

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.compiler.schemas import _Failure
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.redaction import Classification


def expression_shape(expression: JsonObject) -> JsonValue:
    if "literal" in expression:
        return expression["literal"]
    if isinstance(expression.get("object"), dict):
        return {
            k: expression_shape(v) if isinstance(v, dict) else None
            for k, v in cast(JsonObject, expression["object"]).items()
        }
    if isinstance(expression.get("array"), list):
        return [
            expression_shape(v) if isinstance(v, dict) else None for v in cast(list[JsonValue], expression["array"])
        ]
    # Every expression produces a present value; unknown nested structure is checked at runtime.
    return None


def contains_literal(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return "literal" in value or any(contains_literal(v) for v in value.values())
    return isinstance(value, list) and any(contains_literal(v) for v in value)


def admit_binding(schema: JsonObject, expression: JsonObject, bundle: dict[str, JsonObject]) -> None:
    policy = Classification(schema, bundle, SchemaLimits())

    def shapes(value: JsonObject) -> Iterator[JsonValue]:
        policy.budget.charge()
        yield expression_shape(value)
        if "literal" in value:
            return
        if isinstance(value.get("object"), dict):
            fields = cast(JsonObject, value["object"])
            for key, child in fields.items():
                if isinstance(child, dict):
                    for possible in shapes(child):
                        policy.budget.charge(len(fields))
                        yield {**cast(JsonObject, expression_shape(value)), key: possible}
        elif isinstance(value.get("array"), list):
            items = cast(list[JsonValue], value["array"])
            for index, child in enumerate(items):
                if isinstance(child, dict):
                    for possible in shapes(child):
                        policy.budget.charge(len(items))
                        candidate = cast(list[JsonValue], expression_shape(value))
                        candidate[index] = possible
                        yield candidate
        elif isinstance(value.get("op"), dict):
            arguments = cast(JsonObject, value["op"]).get("args")
            if isinstance(arguments, list):
                # Retain visible alternatives/operands without evaluating unknown references.
                for argument in arguments:
                    if isinstance(argument, dict):
                        yield from shapes(argument)
            elif contains_literal(value):
                raise _Failure("INVALID_INSTANCE")
        elif contains_literal(value):
            raise _Failure("INVALID_INSTANCE")

    for possible in shapes(expression):
        policy.check(schema, possible)


def admit_authoring(
    document: JsonObject, catalog: CatalogSnapshot, *, unresolved_literals: bool = True, incomplete: bool = False
) -> None:
    """Unresolved declarations are editable; ambiguous payload-bearing bindings are not."""
    measure_value(document)
    bundle = {name: value.value for name, value in catalog.schemas.items()}

    def schema(value: JsonValue) -> None:
        if not isinstance(value, dict):
            return
        try:
            Classification(value, bundle, SchemaLimits()).literals()
        except _Failure as error:
            # A declaration-only unresolved reference has no stored operand to classify.
            if error.code == "INVALID_REF" and not any_literals(value):
                return
            raise

    def binding(target: JsonValue, expression: JsonValue) -> None:
        if not isinstance(expression, dict) or (incomplete and not contains_literal(expression)):
            return
        if not isinstance(target, dict):
            if unresolved_literals and contains_literal(expression):
                raise _Failure("INVALID_REF")
            return
        try:
            admit_binding(target, expression, bundle)
        except _Failure as error:
            if error.code == "INVALID_REF" and (not unresolved_literals or not contains_literal(expression)):
                return
            raise

    def walk(value: JsonValue) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in {"inputSchema", "outputSchema", "payloadSchema", "authSchema", "configSchema"}:
                    schema(child)
                elif key not in {"literal", "default", "examples", "const", "enum"}:
                    walk(child)
            if value.get("kind") == "action" and "with" in value:
                resource = catalog.resolve("Action", str(value.get("uses", "")))
                spec = cast(JsonObject, resource.definition.value.get("spec", {})) if resource else {}
                binding(spec.get("inputSchema"), value["with"])
                implementation = spec.get("implementation", {})
                secondary = None
                if isinstance(implementation, dict) and implementation.get("kind") == "worker":
                    reference = f"{implementation.get('taskType')}@{implementation.get('taskVersion')}"
                    task = catalog.resolve("TaskCapability", reference)
                    secondary = task.definition.value.get("inputSchema") if task else None
                elif isinstance(implementation, dict) and implementation.get("kind") == "connector":
                    connector = catalog.resolve("Connector", str(implementation.get("uses", "")))
                    if connector:
                        actions = cast(JsonObject, cast(JsonObject, connector.definition.value["spec"])["actions"])
                        descriptor = actions.get(str(implementation.get("action", "")))
                        if isinstance(descriptor, dict):
                            secondary = descriptor.get("inputSchema")
                binding(secondary, value["with"])
            if "output" in value and "outputSchema" in value:
                binding(value["outputSchema"], value["output"])
            if value.get("kind") == "connector" and "config" in value:
                resource = catalog.resolve("Connector", str(value.get("uses", "")))
                spec = cast(JsonObject, resource.definition.value.get("spec", {})) if resource else {}
                actions = cast(JsonObject, spec.get("actions", {}))
                descriptor = actions.get(str(value.get("action", "")), {})
                target = descriptor.get("configSchema", {}) if isinstance(descriptor, dict) and resource else None
                binding(target, {"literal": value["config"]})

    walk(document)


def any_literals(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(k in value for k in ("default", "examples", "const", "enum")) or any(
            any_literals(v) for v in value.values()
        )
    return isinstance(value, list) and any(any_literals(v) for v in value)


def admit_artifact(executable: JsonObject) -> None:
    """Recheck imported schemas and visible bindings without trusting envelope hashes."""
    from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument, ResourceKind

    resources = {}
    for dependency in cast(list[JsonObject], executable["dependencies"]):
        resource = CatalogResource(
            cast(ResourceKind, dependency["kind"]),
            cast(str, dependency["reference"]),
            FrozenDocument.from_value(cast(JsonObject, dependency["document"])),
        )
        resources[(resource.kind, resource.reference)] = resource
    catalog = CatalogSnapshot(resources, {})
    bundle = {name: value.value for name, value in catalog.schemas.items()}
    for value in cast(JsonObject, executable["schemas"]).values():
        Classification(cast(JsonObject, value), bundle, SchemaLimits()).literals()
    for resource in catalog.resources.values():
        if resource.kind == "Schema":
            Classification(resource.definition.value, bundle, SchemaLimits()).literals()
        else:
            admit_authoring(resource.definition.value, catalog)
    if "spec" in executable:
        admit_authoring({"kind": executable["kind"], "spec": executable["spec"]}, catalog)
    graph = executable.get("graph")
    if isinstance(graph, dict):
        schemas = cast(JsonObject, executable["schemas"])
        for node in cast(list[JsonObject], graph["nodes"]):
            if node["kind"] == "action":
                action = next(r for r in resources.values() if r.kind == "Action" and r.digest == node["dependency"])
                admit_authoring({"kind": "action", "uses": action.reference, "with": node["with"]}, catalog)
            elif node["kind"] == "end":
                admit_binding(
                    cast(JsonObject, schemas[cast(str, executable["outputSchema"])]),
                    cast(JsonObject, node["output"]),
                    bundle,
                )
