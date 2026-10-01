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

"""Bounded JSON/YAML parsing with value spans and typed diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Literal, NoReturn, cast

import rfc8785
import yaml
from yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from firefly_weave.compiler.source_map import SourceMap, pointer_child
from firefly_weave.contracts.diagnostics import Diagnostic, RelatedLocation, SourceRange
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject, JsonValue

_DEFAULT_LIMITS = Limits()
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_TAG_PREFIX = "tag:yaml.org,2002:"
_ALLOWED_TAGS = {_TAG_PREFIX + name for name in ("str", "null", "bool", "int", "float", "map", "seq")}


@dataclass(frozen=True)
class ParsedSource:
    value: JsonObject
    locations: dict[str, SourceRange]
    source_hash: str
    pointers: frozenset[str]


class ParseFailure(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...], *, omitted_count: int = 0) -> None:
        self.diagnostics = diagnostics
        self.omitted_count = omitted_count
        super().__init__("; ".join(issue.message for issue in diagnostics))

    @property
    def truncated(self) -> bool:
        return self.omitted_count > 0


class _Context:
    def __init__(self, limits: Limits, source_map: SourceMap | None = None) -> None:
        self.limits = limits
        self.source_map = source_map
        self.locations: dict[str, SourceRange] = {}
        self.pointers: set[str] = set()
        self.diagnostics: list[Diagnostic] = []
        self.nodes = 0
        self.omitted_count = 0

    def issue(
        self,
        code: str,
        message: str,
        path: str = "",
        source: SourceRange | None = None,
        related: list[RelatedLocation] | None = None,
    ) -> None:
        if len(self.diagnostics) >= self.limits.max_diagnostics:
            self.omitted_count += 1
            return
        self.diagnostics.append(
            Diagnostic(
                code=f"WV-PARSE-{code}",
                severity="error",
                stage="parse",
                message=message,
                path=path,
                source=source,
                related=related or [],
            )
        )

    def fail(self, code: str, message: str, path: str = "", source: SourceRange | None = None) -> NoReturn:
        self.issue(code, message, path, source)
        raise ParseFailure(tuple(self.diagnostics), omitted_count=self.omitted_count)

    def visit(self, path: str, depth: int, source: SourceRange | None = None) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_document_nodes:
            self.fail("NODE_LIMIT", "Source node budget exceeded.", path, source)
        if depth > self.limits.max_depth:
            self.fail("DEPTH_LIMIT", "Source nesting budget exceeded.", path, source)
        self.pointers.add(path)
        if source is not None:
            self.locations[path] = source

    def scalar(self, value: object, path: str, source: SourceRange | None = None) -> JsonValue:
        valid = value is None or type(value) is bool
        if type(value) is str:
            valid = not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        elif type(value) is int:
            valid = -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
        elif type(value) is float:
            valid = math.isfinite(value)
        if not valid:
            self.fail("VALUE", "Value is outside the interoperable JSON domain.", path, source)
        return cast(JsonValue, value)

    def duplicate(self, path: str, source: SourceRange, first: SourceRange) -> None:
        self.issue(
            "DUPLICATE_KEY",
            "Duplicate object key.",
            path,
            source,
            [RelatedLocation(path=path, source=first, message="First key occurrence.")],
        )


class _JsonParser:
    def __init__(self, text: str, context: _Context, source_map: SourceMap) -> None:
        self.text = text
        self.context = context
        self.source_map = source_map
        self.index = 0

    def whitespace(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n":
            self.index += 1

    def syntax(self, path: str) -> NoReturn:
        self.context.fail("SYNTAX", "Invalid JSON syntax.", path, self.source_map.span(self.index, self.index))

    def string(self, path: str) -> str:
        start = self.index
        self.index += 1
        while self.index < len(self.text):
            char = self.text[self.index]
            self.index += 1
            if char == '"':
                try:
                    value = json.loads(self.text[start : self.index])
                except ValueError:
                    self.syntax(path)
                return cast(str, self.context.scalar(value, path, self.source_map.span(start, self.index)))
            if char == "\\":
                self.index += 1
            elif ord(char) < 32:
                self.syntax(path)
        self.syntax(path)

    def value(self, path: str, depth: int) -> JsonValue:
        self.whitespace()
        start = self.index
        if start >= len(self.text):
            self.syntax(path)
        char = self.text[start]
        container = char in "{["
        self.context.visit(path, depth + int(container), self.source_map.span(start, start))
        result: JsonValue
        if char == "{":
            result = {}
            keys: dict[str, SourceRange] = {}
            self.index += 1
            self.whitespace()
            if self.index < len(self.text) and self.text[self.index] == "}":
                self.index += 1
            else:
                while True:
                    self.whitespace()
                    key_start = self.index
                    if self.index >= len(self.text) or self.text[self.index] != '"':
                        self.syntax(path)
                    key = self.string(path)
                    child = pointer_child(path, key)
                    key_span = self.source_map.span(key_start, self.index)
                    if key in keys:
                        self.context.duplicate(child, key_span, keys[key])
                    else:
                        keys[key] = key_span
                    self.whitespace()
                    if self.index >= len(self.text) or self.text[self.index] != ":":
                        self.syntax(child)
                    self.index += 1
                    result[key] = self.value(child, depth + 1)
                    self.whitespace()
                    if self.index >= len(self.text):
                        self.syntax(path)
                    delimiter = self.text[self.index]
                    self.index += 1
                    if delimiter == "}":
                        break
                    if delimiter != ",":
                        self.syntax(path)
        elif char == "[":
            result = []
            self.index += 1
            self.whitespace()
            if self.index < len(self.text) and self.text[self.index] == "]":
                self.index += 1
            else:
                while True:
                    result.append(self.value(pointer_child(path, str(len(result))), depth + 1))
                    self.whitespace()
                    if self.index >= len(self.text):
                        self.syntax(path)
                    delimiter = self.text[self.index]
                    self.index += 1
                    if delimiter == "]":
                        break
                    if delimiter != ",":
                        self.syntax(path)
        elif char == '"':
            result = self.string(path)
        else:
            token = next((token for token in ("null", "true", "false") if self.text.startswith(token, start)), None)
            if token is not None:
                self.index += len(token)
                result = {"null": None, "true": True, "false": False}[token]
            else:
                match = _NUMBER.match(self.text, start)
                if match is None:
                    self.syntax(path)
                self.index = match.end()
                number = match.group()
                try:
                    result = float(number) if any(char in number for char in ".eE") else int(number)
                except ValueError:
                    self.context.fail("VALUE", "Invalid JSON number.", path, self.source_map.span(start, self.index))
                result = self.context.scalar(result, path, self.source_map.span(start, self.index))
        self.context.locations[path] = self.source_map.span(start, self.index)
        return result

    def parse(self) -> JsonValue:
        value = self.value("", 0)
        self.whitespace()
        if self.index != len(self.text):
            self.syntax("")
        return value


@dataclass
class _YamlFrame:
    value: dict[str, JsonValue] | list[JsonValue]
    path: str
    start: int
    key: str | None = None
    keys: dict[str, SourceRange] = field(default_factory=dict)


def _yaml_scalar(event: ScalarEvent, context: _Context, path: str, span: SourceRange) -> JsonValue:
    value = event.value
    tag = event.tag
    if tag in {_TAG_PREFIX + "map", _TAG_PREFIX + "seq"}:
        context.fail("TAG", "Invalid scalar tag.", path, span)
    if tag == _TAG_PREFIX + "str" or (tag is None and event.style is not None):
        return context.scalar(value, path, span)
    if value.lower() in {".nan", ".inf", "+.inf", "-.inf"}:
        context.fail("VALUE", "Nonfinite numbers are not permitted.", path, span)
    result: JsonValue = value
    if value == "null" or (value == "" and tag in {None, _TAG_PREFIX + "null"}):
        result = None
    elif value in {"true", "false"}:
        result = value == "true"
    elif _NUMBER.fullmatch(value):
        try:
            result = float(value) if any(char in value for char in ".eE") else int(value)
        except ValueError:
            context.fail("VALUE", "Invalid JSON number.", path, span)
    expected = {
        _TAG_PREFIX + "null": type(None),
        _TAG_PREFIX + "bool": bool,
        _TAG_PREFIX + "int": int,
        _TAG_PREFIX + "float": float,
    }
    if tag in expected and type(result) is not expected[tag]:
        context.fail("VALUE", "Explicit scalar tag is incompatible with its JSON value.", path, span)
    return context.scalar(result, path, span)


def _parse_yaml(text: str, context: _Context, source_map: SourceMap) -> JsonValue:
    stack: list[_YamlFrame] = []
    root: JsonValue = None
    documents = 0
    try:
        for event in yaml.parse(text):
            assert event.start_mark is not None and event.end_mark is not None
            span = source_map.span(event.start_mark.index, event.end_mark.index)
            parent = stack[-1] if stack else None
            path = ""
            if parent is not None:
                path = pointer_child(
                    parent.path, str(len(parent.value)) if isinstance(parent.value, list) else parent.key or ""
                )
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    context.fail("EXTRA_DOCUMENT", "Only one YAML document is allowed.", "", span)
                continue
            if isinstance(event, AliasEvent):
                context.fail("ALIAS", "YAML aliases are not allowed.", path, span)
            if isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                frame = stack.pop()
                context.locations[frame.path] = source_map.span(frame.start, event.end_mark.index)
                continue
            if not isinstance(event, (ScalarEvent, MappingStartEvent, SequenceStartEvent)):
                continue
            if event.tag is not None and event.tag not in _ALLOWED_TAGS:
                context.fail("TAG", "Custom YAML tags are not allowed.", path, span)
            is_key = parent is not None and isinstance(parent.value, dict) and parent.key is None
            if is_key:
                assert parent is not None
                if not isinstance(event, ScalarEvent):
                    context.fail("NON_STRING_KEY", "Object keys must be strings.", parent.path, span)
                key = _yaml_scalar(event, context, parent.path, span)
                if type(key) is not str:
                    context.fail("NON_STRING_KEY", "Object keys must be strings.", parent.path, span)
                path = pointer_child(parent.path, key)
                if key == "<<":
                    context.fail("MERGE_KEY", "YAML merge keys are not allowed.", path, span)
                if key in parent.keys:
                    context.duplicate(path, span, parent.keys[key])
                else:
                    parent.keys[key] = span
                parent.key = key
                continue
            container = isinstance(event, (MappingStartEvent, SequenceStartEvent))
            context.visit(path, len(stack) + int(container), span)
            value: JsonValue
            if isinstance(event, MappingStartEvent):
                if event.tag not in {None, _TAG_PREFIX + "map"}:
                    context.fail("TAG", "Invalid mapping tag.", path, span)
                value = {}
            elif isinstance(event, SequenceStartEvent):
                if event.tag not in {None, _TAG_PREFIX + "seq"}:
                    context.fail("TAG", "Invalid sequence tag.", path, span)
                value = []
            else:
                if event.tag in {_TAG_PREFIX + "map", _TAG_PREFIX + "seq"}:
                    context.fail("TAG", "Invalid scalar tag.", path, span)
                value = _yaml_scalar(event, context, path, span)
            if parent is None:
                root = value
            elif isinstance(parent.value, list):
                parent.value.append(value)
            else:
                assert parent.key is not None
                parent.value[parent.key] = value
                parent.key = None
            if isinstance(value, (dict, list)):
                stack.append(_YamlFrame(value, path, event.start_mark.index))
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        location = source_map.span(mark.index, mark.index) if mark is not None else None
        context.fail("SYNTAX", "Invalid YAML syntax.", "", location)
    except yaml.YAMLError:
        context.fail("SYNTAX", "Invalid YAML syntax.")
    return root


def _parse_object(source: object, context: _Context) -> JsonObject:
    if type(source) is not dict:
        context.fail("ROOT", "Source root must be an object.")
    active: set[int] = set()
    size = 0

    def add_size(amount: int) -> None:
        nonlocal size
        size += amount
        if size > context.limits.max_source_bytes:
            context.fail("SOURCE_LIMIT", "Source byte budget exceeded.")

    def string_size(value: str, path: str) -> None:
        add_size(2)
        for char in value:
            code = ord(char)
            if 0xD800 <= code <= 0xDFFF:
                context.fail("VALUE", "Value is outside the interoperable JSON domain.", path)
            if char in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
                add_size(2)
            elif code < 32:
                add_size(6)
            else:
                add_size(1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4)

    def walk(value: object, path: str, depth: int) -> None:
        container = type(value) in {dict, list}
        context.visit(path, depth + int(container))
        if not container:
            if type(value) is str:
                string_size(value, path)
            else:
                context.scalar(value, path)
                add_size(len(json.dumps(value)))
            return
        if id(value) in active:
            context.fail("CYCLE", "Object source contains a cycle.", path)
        active.add(id(value))
        add_size(2)
        if isinstance(value, dict):
            for index, (key, item) in enumerate(value.items()):
                if type(key) is not str:
                    context.fail("NON_STRING_KEY", "Object keys must be strings.", path)
                add_size(1 + int(index > 0))
                string_size(key, path)
                walk(item, pointer_child(path, key), depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                add_size(int(index > 0))
                walk(item, pointer_child(path, str(index)), depth + 1)
        active.remove(id(value))

    walk(source, "", 0)
    return cast(JsonObject, source)


def parse_source(
    source: str | bytes | JsonObject,
    *,
    format: Literal["yaml", "json", "object"],
    filename: str | None = None,
    limits: Limits = _DEFAULT_LIMITS,
) -> ParsedSource:
    context = _Context(limits)
    if format == "object":
        try:
            value = _parse_object(source, context)
            digest = hashlib.sha256(rfc8785.dumps(value)).hexdigest()
        except RecursionError:
            context.fail("DEPTH_LIMIT", "Source nesting exceeds parser capacity.")
    else:
        if format not in {"yaml", "json"} or not isinstance(source, (str, bytes)):
            context.fail("INPUT", "Source and format are incompatible.")
        if isinstance(source, bytes):
            if len(source) > limits.max_source_bytes:
                context.fail("SOURCE_LIMIT", "Source byte budget exceeded.")
            try:
                text = source.decode("utf-8")
            except UnicodeDecodeError:
                context.fail("ENCODING", "Source must be UTF-8.")
            raw = source
        else:
            # Count before allocating the encoded byte buffer.
            size = 0
            for char in source:
                code = ord(char)
                if 0xD800 <= code <= 0xDFFF:
                    context.fail("VALUE", "Source contains a lone Unicode surrogate.")
                size += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
                if size > limits.max_source_bytes:
                    context.fail("SOURCE_LIMIT", "Source byte budget exceeded.")
            text = source
            raw = source.encode("utf-8")
        source_map = SourceMap(text, filename, yaml_line_breaks=format == "yaml")
        context.source_map = source_map
        if text.startswith("\ufeff"):
            context.fail("BOM", "UTF-8 BOM is not allowed.", "", source_map.span(0, 1))
        try:
            parsed = (
                _parse_yaml(text, context, source_map)
                if format == "yaml"
                else _JsonParser(text, context, source_map).parse()
            )
        except RecursionError:
            context.fail("DEPTH_LIMIT", "Source nesting exceeds parser capacity.")
        if not isinstance(parsed, dict):
            context.fail("ROOT", "Source root must be an object.", "", context.locations.get(""))
        value = parsed
        digest = hashlib.sha256(raw).hexdigest()
    if context.diagnostics:
        raise ParseFailure(tuple(context.diagnostics), omitted_count=context.omitted_count)
    return ParsedSource(value, context.locations, digest, frozenset(context.pointers))
