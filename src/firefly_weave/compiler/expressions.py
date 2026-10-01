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

"""JSON-only expression interpretation, shared pointers, and structural budgets."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import cast

from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject, JsonValue


class ExpressionFailure(ValueError):
    """Safe, stable expression error located at an expression-document pointer."""

    def __init__(self, code: str, path: str = "") -> None:
        self.code = f"WV-EXPR-{code}"
        self.path = path
        super().__init__(self.code)


@dataclass
class _ValueWork:
    remaining: int

    def charge(self, path: str) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ExpressionFailure("RESOURCE_LIMIT", path)


class _Missing:
    pass


_MISSING = _Missing()
DEFAULT_LIMITS = Limits()
_COMPARISONS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_OPERATORS = _COMPARISONS | {"and", "or", "not", "exists", "coalesce"}


def pointer_segments(pointer: str, *, path: str = "") -> tuple[str, ...]:
    """Decode RFC 6901 once; both runtime lookup and schema lookup use this."""
    if type(pointer) is not str or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", pointer) is None:
        raise ExpressionFailure("INVALID", path)
    return tuple(segment.replace("~1", "/").replace("~0", "~") for segment in pointer.split("/")[1:])


def array_index(segment: str, length: int | None = None) -> int | None:
    """Array pointers accept ASCII canonical nonnegative indexes only."""
    if re.fullmatch(r"0|[1-9][0-9]*", segment) is None:
        return None
    # Huge indexes cannot address any bounded JSON array; avoid int's digit ceiling.
    if len(segment) > 15:
        return None
    index = int(segment)
    return index if length is None or index < length else None


def _resolve(context: JsonValue, segments: tuple[str, ...]) -> JsonValue | _Missing:
    value = context
    for segment in segments:
        if type(value) is dict:
            if segment not in value:
                return _MISSING
            value = value[segment]
        elif type(value) is list:
            index = array_index(segment, len(value))
            if index is None:
                return _MISSING
            value = value[index]
        else:
            return _MISSING
    return value


def _string_size(value: str, limits: Limits, path: str) -> int:
    if len(value) > limits.max_payload_bytes:
        raise ExpressionFailure("RESOURCE_LIMIT", path)
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except UnicodeEncodeError:
        raise ExpressionFailure("INVALID_JSON", path) from None
    if size > limits.max_payload_bytes:
        raise ExpressionFailure("RESOURCE_LIMIT", path)
    return size


def measure_value(
    value: object,
    *,
    limits: Limits = DEFAULT_LIMITS,
    path: str = "",
    context: bool = False,
    work: _ValueWork | None = None,
) -> int:
    """Bound exact compact JSON bytes before copying, with cycle/domain checks.

    Contexts may hold many historical payloads. Their aggregate node/depth budget
    applies, but bytes are bounded per scalar and on every selected/output value.
    """
    size = 0
    nodes = 0
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if work is not None:
            work.charge(path)
        if nodes > limits.max_document_nodes:
            raise ExpressionFailure("RESOURCE_LIMIT", path)
        kind = type(item)
        if kind is dict or kind is list:
            if id(item) in active:
                raise ExpressionFailure("INVALID_JSON", path)
            if depth + 1 > limits.max_depth:
                raise ExpressionFailure("RESOURCE_LIMIT", path)
            active.add(id(item))
            stack.append((item, depth, True))
            container = cast(dict[object, object] | list[object], item)
            size += 2 + max(0, len(container) - 1)
            if len(container) > limits.max_document_nodes - nodes:
                raise ExpressionFailure("RESOURCE_LIMIT", path)
            if kind is dict:
                mapping = cast(dict[object, object], item)
                for key, child in mapping.items():
                    if type(key) is not str:
                        raise ExpressionFailure("INVALID_JSON", path)
                    size += _string_size(key, limits, path) + 1
                    stack.append((child, depth + 1, False))
            else:
                stack.extend((child, depth + 1, False) for child in cast(list[object], item))
        elif kind is str:
            size += _string_size(cast(str, item), limits, path)
        elif item is None:
            size += 4
        elif kind is bool:
            size += 4 if item else 5
        elif kind is int:
            if not -MAX_SAFE_INTEGER <= cast(int, item) <= MAX_SAFE_INTEGER:
                raise ExpressionFailure("INVALID_JSON", path)
            size += len(str(item))
        elif kind is float:
            if not math.isfinite(cast(float, item)):
                raise ExpressionFailure("INVALID_JSON", path)
            size += len(str(item))
        else:
            raise ExpressionFailure("INVALID_JSON", path)
        if not context and size > limits.max_payload_bytes:
            raise ExpressionFailure("RESOURCE_LIMIT", path)
    return size


def _children(expression: object, path: str, limits: Limits, work: _ValueWork) -> list[tuple[JsonObject, str]]:
    if type(expression) is not dict or len(cast(dict[object, object], expression)) != 1:
        raise ExpressionFailure("INVALID", path)
    tag, value = next(iter(cast(dict[object, object], expression).items()))
    if type(tag) is not str:
        raise ExpressionFailure("INVALID", path)
    tag_path = pointer_child(path, tag)
    if tag == "literal":
        measure_value(value, limits=limits, path=tag_path, work=work)
        return []
    if tag == "ref":
        if type(value) is not str:
            raise ExpressionFailure("INVALID", tag_path)
        _string_size(value, limits, tag_path)
        pointer_segments(value, path=tag_path)
        return []
    if tag == "object" and type(value) is dict:
        if len(value) > limits.max_expression_nodes:
            raise ExpressionFailure("RESOURCE_LIMIT", tag_path)
        children = []
        for key, child in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ExpressionFailure("INVALID", tag_path)
            _string_size(key, limits, tag_path)
            children.append((cast(JsonObject, child), pointer_child(tag_path, key)))
        return children
    if tag == "array" and type(value) is list:
        if len(value) > limits.max_expression_nodes:
            raise ExpressionFailure("RESOURCE_LIMIT", tag_path)
        return [(cast(JsonObject, child), pointer_child(tag_path, str(i))) for i, child in enumerate(value)]
    if tag != "op" or type(value) is not dict:
        raise ExpressionFailure("INVALID", tag_path)
    operation = cast(dict[str, object], value)
    if any(type(key) is not str for key in operation) or operation.keys() != {"name", "args"}:
        raise ExpressionFailure("INVALID", tag_path)
    name, args = operation["name"], operation["args"]
    if type(name) is not str or name not in _OPERATORS or type(args) is not list:
        raise ExpressionFailure("INVALID", tag_path)
    operands = cast(list[JsonObject], args)
    if len(operands) > limits.max_expression_nodes:
        raise ExpressionFailure("RESOURCE_LIMIT", tag_path)
    count = len(operands)
    if (
        (name in _COMPARISONS and count != 2)
        or (name in {"not", "exists"} and count != 1)
        or (name in {"and", "or", "coalesce"} and count < 1)
        or (
            name == "exists"
            and (
                type(operands[0]) is not dict
                or any(type(key) is not str for key in operands[0])
                or operands[0].keys() != {"ref"}
            )
        )
    ):
        raise ExpressionFailure("ARITY", tag_path)
    return [(child, pointer_child(f"{tag_path}/args", str(i))) for i, child in enumerate(operands)]


def count_expression_nodes(
    expression: JsonObject, *, limits: Limits = DEFAULT_LIMITS, initial_count: int = 0, path: str = ""
) -> int:
    """Return cumulative count; A5 threads it across all workflow expression roots.

    Literal data is never interpreted as syntax. All operands, including lazy
    ones, are structurally validated and charged before runtime evaluation.
    """
    if type(initial_count) is not int or initial_count < 0:
        raise ValueError("initial_count must be a nonnegative integer")
    count = initial_count
    value_work = _ValueWork(limits.max_document_nodes)
    stack = [(expression, path, 1)]
    while stack:
        node, node_path, depth = stack.pop()
        count += 1
        if count > limits.max_expression_nodes or depth > limits.max_depth:
            raise ExpressionFailure("RESOURCE_LIMIT", node_path)
        children = _children(node, node_path, limits, value_work)
        if len(children) > limits.max_expression_nodes - count:
            raise ExpressionFailure("RESOURCE_LIMIT", node_path)
        stack.extend((child, child_path, depth + 1) for child, child_path in reversed(children))
    return count


def _copy(value: JsonValue) -> JsonValue:
    if type(value) is dict:
        return {key: _copy(child) for key, child in value.items()}
    if type(value) is list:
        return [_copy(child) for child in value]
    return value


def json_equal(left: JsonValue, right: JsonValue) -> bool:
    """Structural equality over already validated, bounded JSON values."""
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if type(left) is dict and type(right) is dict:
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    if type(left) is list and type(right) is list:
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


@dataclass
class _Evaluator:
    context: JsonObject
    limits: Limits
    operations: int = 0
    value_work: _ValueWork = field(init=False)

    def __post_init__(self) -> None:
        self.value_work = _ValueWork(self.limits.max_document_nodes)

    def run(
        self, expression: JsonObject, path: str = "", *, allow_missing: bool = False, presence_only: bool = False
    ) -> JsonValue | _Missing:
        self.operations += 1
        if self.operations > self.limits.max_expression_nodes:
            raise ExpressionFailure("RESOURCE_LIMIT", path)
        tag, body = next(iter(expression.items()))
        result: JsonValue | _Missing
        if tag == "literal":
            result = body
        elif tag == "ref":
            result = _resolve(self.context, pointer_segments(cast(str, body)))
            if result is _MISSING:
                if allow_missing:
                    return result
                raise ExpressionFailure("MISSING", path)
            if presence_only:
                return result
        elif tag == "object":
            result = {}
            size = 2
            for key, child in cast(dict[str, JsonObject], body).items():
                value = cast(JsonValue, self.run(child, pointer_child(f"{path}/object", key)))
                size += (
                    _string_size(key, self.limits, path)
                    + 1
                    + measure_value(value, limits=self.limits, path=path, work=self.value_work)
                )
                size += int(bool(result))
                if size > self.limits.max_payload_bytes:
                    raise ExpressionFailure("RESOURCE_LIMIT", path)
                result[key] = value
        elif tag == "array":
            result = []
            size = 2
            for i, child in enumerate(cast(list[JsonObject], body)):
                value = cast(JsonValue, self.run(child, f"{path}/array/{i}"))
                size += measure_value(value, limits=self.limits, path=path, work=self.value_work) + int(i > 0)
                if size > self.limits.max_payload_bytes:
                    raise ExpressionFailure("RESOURCE_LIMIT", path)
                result.append(value)
        else:
            operation = cast(JsonObject, body)
            args = cast(list[JsonObject], operation["args"])
            name = cast(str, operation["name"])
            args_path = f"{path}/op/args"
            if name == "exists":
                result = self.run(args[0], f"{args_path}/0", allow_missing=True, presence_only=True) is not _MISSING
            elif name == "coalesce":
                result = None
                for i, arg in enumerate(args):
                    candidate = self.run(arg, f"{args_path}/{i}", allow_missing=True)
                    if candidate is not _MISSING and candidate is not None:
                        result = candidate
                        break
            elif name in {"and", "or"}:
                result = name == "and"
                for i, arg in enumerate(args):
                    boolean = self.run(arg, f"{args_path}/{i}")
                    if type(boolean) is not bool:
                        raise ExpressionFailure("TYPE", f"{args_path}/{i}")
                    result = boolean
                    if result == (name == "or"):
                        break
            elif name == "not":
                boolean = self.run(args[0], f"{args_path}/0")
                if type(boolean) is not bool:
                    raise ExpressionFailure("TYPE", f"{args_path}/0")
                result = not boolean
            else:
                left = cast(JsonValue, self.run(args[0], f"{args_path}/0"))
                right = cast(JsonValue, self.run(args[1], f"{args_path}/1"))
                if name in {"eq", "ne"}:
                    equal = json_equal(left, right)
                    result = equal if name == "eq" else not equal
                else:
                    if not (
                        (type(left) in {int, float} and type(right) in {int, float})
                        or (type(left) is str and type(right) is str)
                    ):
                        raise ExpressionFailure("TYPE", path)
                    a = cast(int | float | str, left)
                    b = cast(int | float | str, right)
                    if name == "lt":
                        result = a < b  # type: ignore[operator]
                    elif name == "lte":
                        result = a <= b  # type: ignore[operator]
                    elif name == "gt":
                        result = a > b  # type: ignore[operator]
                    else:
                        result = a >= b  # type: ignore[operator]
        measure_value(result, limits=self.limits, path=path, work=self.value_work)
        return cast(JsonValue, result)


def evaluate(expression: JsonObject, context: JsonObject, *, limits: Limits = DEFAULT_LIMITS) -> JsonValue:
    count_expression_nodes(expression, limits=limits)
    if type(context) is not dict:
        raise ExpressionFailure("INVALID_JSON")
    measure_value(context, limits=limits, context=True)
    result = cast(JsonValue, _Evaluator(context, limits).run(expression))
    return _copy(result)
