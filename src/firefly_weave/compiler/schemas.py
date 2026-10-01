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

"""Network-free schema preparation and bounded Draft 2020-12 payload evaluation."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal
from typing import Any, cast

import regex
import rfc8785
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import TypeAdapter
from pydantic import ValidationError as ModelValidationError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012

from firefly_weave.compiler.schema_profile import (
    DEFAULT_CONTRACT_LIMITS,
    DIALECT,
    SCHEMA_ARRAYS,
    SCHEMA_MAPS,
    SCHEMA_SINGLE,
    SUPPORTED_FORMATS,
    SUPPORTED_KEYWORDS,
    SchemaLimits,
)
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.schema_export import contract_models, export_schemas
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject, JsonValue

_DEFAULT_LIMITS = SchemaLimits()
_LOCAL_NAME = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_FORMATS = FormatChecker(formats=sorted(SUPPORTED_FORMATS))


class _Failure(Exception):
    def __init__(self, code: str, path: str = "") -> None:
        self.code = code
        self.path = path


def _issue(code: str, path: str = "") -> Diagnostic:
    messages = {
        "SECRET_VALUE": "Classified secret values cannot enter durable orchestration.",
        "INVALID_FORMAT": "Value does not satisfy the declared format.",
        "INVALID_SCHEMA": "Schema is not valid Draft 2020-12.",
        "INVALID_INSTANCE": "Value does not satisfy the declared contract.",
        "UNSUPPORTED_FORMAT": "Format is outside the supported schema profile.",
        "UNSUPPORTED_KEYWORD": "Keyword is outside the supported schema profile.",
        "UNSUPPORTED_DIALECT": "Schema dialect is not supported.",
        "UNSUPPORTED_PATTERN": "Pattern is outside the supported regex subset.",
        "INVALID_REF": "Reference must resolve to a schema in the local bundle.",
        "RECURSIVE_REF": "Recursive user schemas are not supported.",
        "RESOURCE_LIMIT": "Schema or payload validation resource budget exceeded.",
        "DIAGNOSTICS_TRUNCATED": "Additional validation diagnostics were omitted.",
    }
    return Diagnostic(code=f"WV-SCHEMA-{code}", severity="error", stage="schema", message=messages[code], path=path)


def _measure(value: object, *, byte_limit: int, node_limit: int, depth_limit: int) -> int:
    """Count compact UTF-8 JSON before serializing; reject Python cycles and unsafe scalars."""
    nodes = size = 0
    active: set[int] = set()

    def add(amount: int) -> None:
        nonlocal size
        size += amount
        if size > byte_limit:
            raise _Failure("RESOURCE_LIMIT")

    def string(text: str) -> None:
        add(2)
        for offset in range(0, len(text), 4096):
            chunk = text[offset : offset + 4096]
            try:
                encoded_size = len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) - 2
            except UnicodeEncodeError:
                # Preserve the first invalid/over-budget character's diagnostic.
                for char in chunk:
                    code = ord(char)
                    if 0xD800 <= code <= 0xDFFF:
                        raise _Failure("INVALID_INSTANCE") from None
                    add(len(json.dumps(char, ensure_ascii=False).encode("utf-8")) - 2)
            else:
                add(encoded_size)

    def walk(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        container = type(item) in {dict, list}
        if nodes > node_limit or depth + int(container) > depth_limit:
            raise _Failure("RESOURCE_LIMIT")
        if container:
            if id(item) in active:
                raise _Failure("INVALID_INSTANCE")
            active.add(id(item))
            add(2)
            if isinstance(item, dict):
                for index, (key, child) in enumerate(item.items()):
                    if type(key) is not str:
                        raise _Failure("INVALID_INSTANCE")
                    string(key)
                    add(1 + int(index > 0))
                    walk(child, depth + 1)
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    add(int(index > 0))
                    walk(child, depth + 1)
            active.remove(id(item))
        elif type(item) is str:
            string(item)
        elif item is None or type(item) is bool:
            add(len(json.dumps(item)))
        elif (
            type(item) is int
            and -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER
            or type(item) is float
            and math.isfinite(item)
        ):
            add(len(str(item)))
        else:
            raise _Failure("INVALID_INSTANCE")

    walk(value, 0)
    return nodes


def _pattern(
    pattern: str, limits: SchemaLimits, path: str, *, charge: Callable[[int], None] | None = None
) -> regex.Pattern[str]:
    if len(pattern) > limits.max_pattern_length:
        raise _Failure("RESOURCE_LIMIT", path)
    # Escapes are scanned separately so escaped punctuation cannot enable engine extensions.
    index = 0
    in_class = False
    expansion_cost = max(1, len(pattern))
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 1
            if index == len(pattern):
                raise _Failure("INVALID_SCHEMA", path)
            escaped = pattern[index]
            if escaped.isalnum() and escaped not in "dDsSwWbBnrtfv":
                raise _Failure("UNSUPPORTED_PATTERN", path)
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif in_class:
            pass
        elif char == "{":
            quantifier = re.match(r"\{([0-9]+)(?:,([0-9]*))?\}", pattern[index:])
            if quantifier is None:
                raise _Failure("UNSUPPORTED_PATTERN", path)
            if any(int(count) > limits.max_validation_work for count in quantifier.groups() if count):
                raise _Failure("RESOURCE_LIMIT", path)
            expansion_cost *= max(1, *(int(count) for count in quantifier.groups() if count))
            if expansion_cost > limits.max_validation_work:
                raise _Failure("RESOURCE_LIMIT", path)
            index += len(quantifier.group()) - 1
        elif (
            char == "}"
            or pattern.startswith("(*", index)
            or (
                pattern.startswith("(?", index)
                and not pattern.startswith("(?:", index)
                or char == "+"
                and index
                and pattern[index - 1] in "*+?}"
            )
        ):
            raise _Failure("UNSUPPORTED_PATTERN", path)
        index += 1
    if charge is not None:
        charge(expansion_cost)
    try:
        return regex.compile(pattern, regex.ASCII | regex.VERSION0)
    except regex.error:
        raise _Failure("INVALID_SCHEMA", path) from None


def _children(schema: dict[str, Any], path: str) -> Iterator[tuple[object, str]]:
    for keyword, value in schema.items():
        child_path = pointer_child(path, keyword)
        if keyword in SCHEMA_MAPS and isinstance(value, dict):
            for name, child in value.items():
                yield child, pointer_child(child_path, name)
        elif keyword in SCHEMA_ARRAYS and isinstance(value, list):
            for index, child in enumerate(value):
                yield child, pointer_child(child_path, str(index))
        elif keyword in SCHEMA_SINGLE:
            yield value, child_path


def _local_name(name: str) -> bool:
    return _LOCAL_NAME.fullmatch(name) is not None


def _resolve(ref: object, document: str, documents: dict[str, JsonObject]) -> tuple[str, str, object]:
    if not isinstance(ref, str) or ref.count("#") > 1:
        raise _Failure("INVALID_REF")
    name, _, fragment = ref.partition("#")
    if name and not _local_name(name):
        raise _Failure("INVALID_REF")
    target_document = name or document
    if target_document not in documents or (fragment and not fragment.startswith("/")):
        raise _Failure("INVALID_REF")
    target: object = documents[target_document]
    for part in fragment.split("/")[1:] if fragment else []:
        if re.search(r"~(?![01])", part) or "%" in part:
            raise _Failure("INVALID_REF")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and key in target:
            target = target[key]
        elif isinstance(target, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(target):
            target = target[int(key)]
        else:
            raise _Failure("INVALID_REF")
    if type(target) not in {dict, bool}:
        raise _Failure("INVALID_REF")
    return target_document, fragment, target


def _no_remote(uri: str) -> Resource[Any]:
    # referencing's generated attrs constructor is not visible to current mypy.
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


def _ordered(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _ordered(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_ordered(child) for child in value]
    return value


def _prepare(
    schema: JsonObject, bundle: dict[str, JsonObject], limits: SchemaLimits, *, trusted: bool = False
) -> tuple[JsonObject, Registry[Any]]:
    documents = {"": schema, **bundle}
    if "" in bundle or any(not _local_name(name) for name in bundle):
        raise _Failure("INVALID_REF")
    if isinstance(schema.get("$id"), str) and schema["$id"] in bundle:
        raise _Failure("INVALID_REF", "/$id")
    # A single bundle consumes one structural budget, including unreachable resources.
    try:
        _measure(
            documents,
            byte_limit=limits.max_schema_bytes,
            node_limit=limits.max_schema_nodes,
            depth_limit=limits.max_schema_depth,
        )
    except _Failure as failure:
        if failure.code == "INVALID_INSTANCE":
            raise _Failure("INVALID_SCHEMA") from None
        raise
    documents = {name: cast(JsonObject, _ordered(root)) for name, root in sorted(documents.items())}
    nodes: dict[tuple[str, str], dict[str, Any] | bool] = {}
    pattern_work = 0

    def charge_pattern(work: int) -> None:
        nonlocal pattern_work
        pattern_work += work
        if pattern_work > limits.max_validation_work:
            raise _Failure("RESOURCE_LIMIT")

    for name, root in documents.items():
        stack: list[tuple[object, str]] = [(root, "")]
        while stack:
            node, path = stack.pop()
            if type(node) is bool:
                nodes[name, path] = node
                continue
            if not isinstance(node, dict):
                raise _Failure("INVALID_SCHEMA", path)
            nodes[name, path] = node
            for keyword, value in node.items():
                key_path = pointer_child(path, keyword)
                if keyword not in SUPPORTED_KEYWORDS and not (trusted and keyword == "discriminator"):
                    raise _Failure("UNSUPPORTED_KEYWORD", key_path)
                if keyword == "$schema" and value != DIALECT:
                    raise _Failure("UNSUPPORTED_DIALECT", key_path)
                if keyword == "$id" and (
                    path or not isinstance(value, str) or not _local_name(value) or (name and value != name)
                ):
                    raise _Failure("INVALID_REF", key_path)
                if keyword == "format" and (not isinstance(value, str) or value not in SUPPORTED_FORMATS):
                    raise _Failure("UNSUPPORTED_FORMAT", key_path)
                if keyword == "$ref":
                    try:
                        _resolve(value, name, documents)
                    except _Failure:
                        raise _Failure("INVALID_REF", key_path) from None
                if keyword == "x-secret" and type(value) is not bool:
                    raise _Failure("INVALID_SCHEMA", key_path)
                if keyword == "pattern" and isinstance(value, str):
                    _pattern(value, limits, key_path, charge=charge_pattern)
                if keyword == "patternProperties" and isinstance(value, dict):
                    for pattern in value:
                        _pattern(pattern, limits, key_path, charge=charge_pattern)
            stack.extend(reversed(list(_children(node, path))))
        try:
            Draft202012Validator.check_schema(root)
        except SchemaError as error:
            error_path = ""
            for segment in error.absolute_path:
                error_path = pointer_child(error_path, str(segment))
            raise _Failure("INVALID_SCHEMA", error_path) from None
    active: set[tuple[str, str]] = set()
    weights: dict[tuple[str, str], int] = {}
    heights: dict[tuple[str, str], int] = {}

    def expansion(name: str, path: str, node: object, depth: int) -> int:
        identity = name, path
        if identity in active:
            if trusted:
                return 0
            raise _Failure("RECURSIVE_REF", path)
        if depth > limits.max_ref_depth:
            raise _Failure("RESOURCE_LIMIT", path)
        if identity in weights:
            if depth + heights[identity] > limits.max_ref_depth:
                raise _Failure("RESOURCE_LIMIT", path)
            return weights[identity]
        active.add(identity)
        count = 1
        height = 0
        if isinstance(node, dict):
            for child, child_path in _children(node, path):
                count += expansion(name, child_path, child, depth + 1)
                height = max(height, 1 + heights.get((name, child_path), 0))
                if count > limits.max_expansion_nodes:
                    raise _Failure("RESOURCE_LIMIT", path)
            if "$ref" in node:
                try:
                    target_name, target_path, target = _resolve(node["$ref"], name, documents)
                except _Failure:
                    raise _Failure("INVALID_REF", pointer_child(path, "$ref")) from None
                if (target_name, target_path) not in nodes:
                    raise _Failure("INVALID_REF", pointer_child(path, "$ref"))
                count += expansion(target_name, target_path, target, depth + 1)
                height = max(height, 1 + heights.get((target_name, target_path), 0))
        active.remove(identity)
        if count > limits.max_expansion_nodes:
            raise _Failure("RESOURCE_LIMIT", path)
        weights[identity] = count
        heights[identity] = height
        return count

    total = 0
    for name, root in documents.items():
        total += expansion(name, "", root, 0)
        if total > limits.max_expansion_nodes:
            raise _Failure("RESOURCE_LIMIT")
    # Explicit dialect annotations make jsonschema.evolve choose the stock validator.
    # Strip validated annotations only from our owned schema nodes before evaluation.
    for node in nodes.values():
        if isinstance(node, dict):
            node.pop("$schema", None)
    schema = documents[""]
    resources = [
        (name, Resource.from_contents(root, default_specification=DRAFT202012)) for name, root in documents.items()
    ]
    root_id = schema.get("$id")
    if isinstance(root_id, str):
        resources.append((root_id, Resource.from_contents(schema, default_specification=DRAFT202012)))
    registry = Registry(retrieve=_no_remote).with_resources(resources)  # type: ignore[call-arg]
    return schema, registry


class _Budget:
    def __init__(self, limits: SchemaLimits) -> None:
        self.limits = limits
        self.work = 0
        self.regex_seconds = 0.0
        self.patterns: dict[str, regex.Pattern[str]] = {}

    def charge(self, work: int = 1) -> None:
        self.work += work
        if self.work > self.limits.max_validation_work:
            raise _Failure("RESOURCE_LIMIT")

    def search(self, pattern: str, value: str) -> bool:
        self.charge()
        compiled = self.patterns.get(pattern)
        if compiled is None:
            compiled = _pattern(pattern, self.limits, "", charge=self.charge)
            self.patterns[pattern] = compiled
        remaining = self.limits.max_regex_seconds - self.regex_seconds
        if remaining <= 0:
            raise _Failure("RESOURCE_LIMIT")
        start = time.monotonic()
        try:
            return compiled.search(value, timeout=min(self.limits.regex_timeout_seconds, remaining)) is not None
        except TimeoutError:
            raise _Failure("RESOURCE_LIMIT") from None
        finally:
            self.regex_seconds += time.monotonic() - start


def _validator(budget: _Budget, *, strict_integer: bool = False) -> Any:
    def pattern(validator: Any, value: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if isinstance(instance, str) and not budget.search(value, instance):
            yield ValidationError("Pattern constraint failed.")

    def pattern_properties(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if isinstance(instance, dict):
            for pattern, subschema in value.items():
                for key, item in instance.items():
                    if budget.search(pattern, key):
                        yield from validator.descend(item, subschema, path=key, schema_path=pattern)

    def additional(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if isinstance(instance, dict):
            properties = schema.get("properties", {})
            patterns = schema.get("patternProperties", {})
            for key, item in instance.items():
                budget.charge()
                if key in properties or any(budget.search(pattern, key) for pattern in patterns):
                    continue
                if value is False:
                    yield ValidationError("Additional property constraint failed.")
                elif isinstance(value, dict):
                    yield from validator.descend(item, value, path=key)

    def unique(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if value and isinstance(instance, list):
            seen: set[bytes] = set()
            for item in instance:
                budget.charge(_weight(item))
                canonical = rfc8785.dumps(item)
                if canonical in seen:
                    yield ValidationError("Unique item constraint failed.")
                    return
                seen.add(canonical)

    def multiple(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if validator.is_type(instance, "number"):
            numerator, denominator = Decimal(str(instance)).as_integer_ratio()
            divisor_numerator, divisor_denominator = Decimal(str(value)).as_integer_ratio()
            if (numerator * divisor_denominator) % (denominator * divisor_numerator) != 0:
                yield ValidationError("Multiple constraint failed.")

    overrides = {
        "pattern": pattern,
        "patternProperties": pattern_properties,
        "additionalProperties": additional,
        "uniqueItems": unique,
        "multipleOf": multiple,
    }

    def bounded(keyword: str, function: Any) -> Any:
        def validate(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
            work = 1
            if isinstance(value, (dict, list)):
                work += len(value)
            if isinstance(instance, (dict, list)):
                work += len(instance)
            if keyword in {"enum", "const"}:
                work += _weight(instance) * (len(value) if keyword == "enum" else 1) + _weight(value)
            budget.charge(work)
            for error in function(validator, value, instance, schema):
                budget.charge()
                yield error

        return validate

    keywords = {
        key: bounded(key, overrides.get(key, function)) for key, function in Draft202012Validator.VALIDATORS.items()
    }
    checker = Draft202012Validator.TYPE_CHECKER
    if strict_integer:
        checker = checker.redefine("integer", lambda checker, value: type(value) is int)
    extend: Callable[..., Any] = validators.extend
    return extend(Draft202012Validator, validators=keywords, type_checker=checker)


def _weight(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(_weight(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_weight(item) for item in value)
    return 1


def _safe_path(segments: Iterable[object], schema_segments: Iterable[object] = ()) -> str:
    instance_path = list(segments)
    safe = [str(segment) if type(segment) is int else "*" for segment in instance_path]
    schema_path = list(schema_segments)
    cursor = index = 0
    while index < len(schema_path) and cursor < len(instance_path):
        keyword = schema_path[index]
        if keyword == "properties" and index + 1 < len(schema_path):
            name = schema_path[index + 1]
            if type(instance_path[cursor]) is str and instance_path[cursor] == name:
                safe[cursor] = str(name)
            cursor += 1
            index += 2
        elif keyword in {"additionalProperties", "patternProperties", "items", "prefixItems"}:
            cursor += 1
            index += 2 if keyword in {"patternProperties", "prefixItems"} else 1
        elif keyword in {"dependentSchemas", "$defs", "allOf", "anyOf", "oneOf"}:
            index += 2
        else:
            index += 1
    path = ""
    for segment in safe:
        path = pointer_child(path, segment)
    return path


def _collect(errors: Iterable[Diagnostic], limits: SchemaLimits) -> tuple[Diagnostic, ...]:
    issues: list[Diagnostic] = []
    for error in errors:
        if len(issues) == limits.max_diagnostics:
            issues[-1] = _issue("DIAGNOSTICS_TRUNCATED")
            break
        issues.append(error)
    return tuple(
        sorted(issues, key=lambda issue: (issue.code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED", issue.path, issue.code))
    )


def validate_schema(
    schema: JsonObject, bundle: dict[str, JsonObject], *, limits: SchemaLimits = _DEFAULT_LIMITS
) -> tuple[Diagnostic, ...]:
    try:
        from firefly_weave.operations.redaction import Classification

        Classification(schema, bundle, limits).literals()
    except (_Failure, RecursionError) as error:
        return (_issue(error.code, error.path),) if isinstance(error, _Failure) else (_issue("RESOURCE_LIMIT"),)
    return ()


def _payload(
    schema: JsonObject, value: JsonValue, bundle: dict[str, JsonObject], limits: SchemaLimits, *, trusted: bool = False
) -> tuple[Diagnostic, ...]:
    try:
        schema, registry = _prepare(schema, bundle, limits, trusted=trusted)
        _measure(
            value,
            byte_limit=limits.max_payload_bytes,
            node_limit=limits.max_document_nodes,
            depth_limit=limits.max_depth,
        )
        value = _ordered(value)
        validator = _validator(_Budget(limits), strict_integer=trusted)(
            schema, registry=registry, format_checker=_FORMATS
        )

        def errors() -> Iterator[Diagnostic]:
            try:
                for error in validator.iter_errors(value):
                    code = "INVALID_FORMAT" if error.validator == "format" else "INVALID_INSTANCE"
                    yield _issue(code, _safe_path(error.absolute_path, error.absolute_schema_path))
            except _Failure as failure:
                yield _issue(failure.code)

        return _collect(errors(), limits)
    except _Failure as failure:
        return (_issue(failure.code, failure.path),)
    except (RecursionError, ArithmeticError):
        return (_issue("RESOURCE_LIMIT"),)


def validate_payload(
    schema: JsonObject,
    value: JsonValue,
    bundle: dict[str, JsonObject],
    *,
    limits: SchemaLimits = _DEFAULT_LIMITS,
    credential_references: bool = False,
) -> tuple[Diagnostic, ...]:
    if not credential_references:
        from firefly_weave.operations.redaction import classify

        try:
            classify(schema, value, bundle, limits=limits)
        except (_Failure, RecursionError, ArithmeticError) as error:
            return (_issue(error.code if isinstance(error, _Failure) else "RESOURCE_LIMIT"),)
    return _payload(schema, value, bundle, limits)


def validate_contract_payload(
    name: str, value: JsonValue, *, limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS
) -> tuple[Diagnostic, ...]:
    """Validate a built-in generated outer contract, including strict model-only invariants."""
    models = contract_models()
    if name not in models:
        raise ValueError("Unknown built-in contract")
    schema = export_schemas()[name]
    issues = _payload(schema, value, {}, limits, trusted=True)
    if issues:
        return issues
    model = models[name]
    value = _ordered(value)
    try:
        if isinstance(model, TypeAdapter):
            model.validate_python(value)
        else:
            model.model_validate(value)
    except ModelValidationError as error:
        return _collect(
            (_issue("INVALID_INSTANCE", _safe_path(item["loc"])) for item in error.errors(include_input=False)),
            limits,
        )
    except RecursionError:
        return (_issue("RESOURCE_LIMIT"),)
    return ()
