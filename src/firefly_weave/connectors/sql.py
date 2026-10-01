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

"""Bounded SQL-family value/config contract and the initial PostgreSQL grammar.

This is deliberately a small language, not a general SQL sanitizer or portable SQL.
"""

import base64
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonObjectData


class SqlConfig(ContractModel):
    statement: str = Field(min_length=1, max_length=16384)
    parameters: dict[str, JsonObjectData]
    mappings: dict[str, Literal["decimal", "date", "datetime", "time", "base64", "uuid"]] = Field(default_factory=dict)
    max_rows: int = Field(default=100, ge=1, le=10000, alias="maxRows")
    max_bytes: int = Field(default=1048576, ge=32, le=1048576, alias="maxBytes")
    statement_timeout_ms: int = Field(default=10000, ge=1, le=30000, alias="statementTimeoutMs")


@dataclass(frozen=True)
class Statement:
    operation: str
    schema: str
    table: str
    columns: tuple[str, ...]
    parameters: tuple[str, ...]
    sql: str


def _tokens(source: str) -> list[str]:
    if not 0 < len(source) <= 16384:
        raise ValueError("SQL source limit")
    result = []
    index = 0
    while index < len(source):
        char = source[index]
        if char in " \t\r\n":
            index += 1
            continue
        start = index
        if char == ":":
            index += 1
            if index == len(source) or not (source[index].isascii() and source[index].isalpha()):
                raise ValueError("Named value required")
            char = source[index]
        if char.isascii() and (char.isalpha() or char == "_"):
            index += 1
            while index < len(source) and source[index].isascii() and (source[index].isalnum() or source[index] == "_"):
                index += 1
            if index - start > 64:
                raise ValueError("SQL token limit")
        elif char in ",.()=":
            index += 1
        else:
            raise ValueError("Unsupported SQL token")
        result.append(source[start:index])
        if len(result) > 1024:
            raise ValueError("SQL token limit")
    return result


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens, self.index = _tokens(source), 0
        self.parameters: list[str] = []
        self.columns: list[str] = []

    def take(self, keyword: str) -> bool:
        if self.index < len(self.tokens) and self.tokens[self.index].upper() == keyword:
            self.index += 1
            return True
        return False

    def require(self, keyword: str) -> None:
        if not self.take(keyword):
            raise ValueError("Unsupported SQL grammar")

    def identifier(self, *, column: bool = False) -> str:
        if self.index >= len(self.tokens):
            raise ValueError("Identifier required")
        value = self.tokens[self.index]
        if not value or not value[0].isascii() or not (value[0].isalpha() or value[0] == "_"):
            raise ValueError("Identifier required")
        self.index += 1
        value = value.lower()
        if column:
            self.columns.append(value)
        return value

    def names(self) -> list[str]:
        names = [self.identifier(column=True)]
        while self.take(","):
            names.append(self.identifier(column=True))
            if len(names) > 64:
                raise ValueError("SQL column limit")
        if len(set(names)) != len(names):
            raise ValueError("Duplicate columns")
        return names

    def bind(self) -> str:
        if self.index >= len(self.tokens) or not self.tokens[self.index].startswith(":"):
            raise ValueError("Only named values are supported")
        value = self.tokens[self.index][1:]
        self.index += 1
        if value not in self.parameters:
            self.parameters.append(value)
        return value

    def assignments(self, separator: str) -> tuple[tuple[str, str], ...]:
        parts = []
        while True:
            name = self.identifier(column=True)
            self.require("=")
            parts.append((name, self.bind()))
            if len(parts) > 64:
                raise ValueError("SQL predicate limit")
            if not self.take(separator):
                break
        return tuple(parts)


def _quote(value: str) -> str:
    return '"' + value + '"'


@dataclass(frozen=True)
class _Syntax:
    operation: str
    schema: str
    table: str
    projection: tuple[str, ...] = ()
    inserted: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    assignments: tuple[tuple[str, str], ...] = ()
    predicates: tuple[tuple[str, str], ...] = ()
    returning: tuple[str, ...] = ()
    limit: str | None = None

    def emit(self, parameters: tuple[str, ...]) -> str:
        def bind(name: str) -> str:
            return "$" + str(parameters.index(name) + 1)

        def names(values: tuple[str, ...]) -> str:
            return ", ".join(map(_quote, values))

        relation = _quote(self.schema) + "." + _quote(self.table)
        if self.operation == "SELECT":
            sql = "SELECT " + names(self.projection) + " FROM " + relation
        elif self.operation == "INSERT":
            sql = (
                "INSERT INTO "
                + relation
                + " ("
                + names(self.inserted)
                + ") VALUES ("
                + ", ".join(map(bind, self.values))
                + ")"
            )
        elif self.operation == "UPDATE":
            sql = (
                "UPDATE "
                + relation
                + " SET "
                + ", ".join(_quote(name) + " = " + bind(value) for name, value in self.assignments)
            )
        else:
            sql = "DELETE FROM " + relation
        if self.predicates:
            sql += " WHERE " + " AND ".join(_quote(name) + " = " + bind(value) for name, value in self.predicates)
        if self.limit is not None:
            sql += " LIMIT " + bind(self.limit)
        if self.returning:
            sql += " RETURNING " + names(self.returning)
        return sql


def parse_statement(source: str) -> Statement:
    parser = _Parser(source)
    operation = parser.identifier().upper()
    projection: tuple[str, ...] = ()
    inserted: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    assignments: tuple[tuple[str, str], ...] = ()
    predicates: tuple[tuple[str, str], ...] = ()
    returning: tuple[str, ...] = ()
    limit = None
    if operation == "SELECT":
        projection = tuple(parser.names())
        parser.require("FROM")
    elif operation == "INSERT":
        parser.require("INTO")
    elif operation == "DELETE":
        parser.require("FROM")
    elif operation != "UPDATE":
        raise ValueError("Unsupported SQL operation")
    schema = parser.identifier()
    parser.require(".")
    table = parser.identifier()
    if schema.startswith("pg_") or schema in {"information_schema", "weave_connector"}:
        raise ValueError("System schema is unavailable")
    if operation == "INSERT":
        parser.require("(")
        inserted = tuple(parser.names())
        parser.require(")")
        parser.require("VALUES")
        parser.require("(")
        binds = [parser.bind()]
        while parser.take(","):
            binds.append(parser.bind())
        parser.require(")")
        if len(inserted) != len(binds):
            raise ValueError("SQL arity mismatch")
        values = tuple(binds)
    elif operation == "UPDATE":
        parser.require("SET")
        assignments = parser.assignments(",")
    if operation in {"UPDATE", "DELETE"}:
        parser.require("WHERE")
        predicates = parser.assignments("AND")
    elif operation == "SELECT" and parser.take("WHERE"):
        predicates = parser.assignments("AND")
    if operation == "SELECT" and parser.take("LIMIT"):
        limit = parser.bind()
    if operation != "SELECT" and parser.take("RETURNING"):
        returning = tuple(parser.names())
    if parser.index != len(parser.tokens):
        raise ValueError("Unsupported SQL suffix")
    syntax = _Syntax(operation, schema, table, projection, inserted, values, assignments, predicates, returning, limit)
    parameters = tuple(parser.parameters)
    return Statement(operation, schema, table, tuple(parser.columns), parameters, syntax.emit(parameters))


def mapped_value(value: Any, mapping: str | None) -> Any:
    if value is None:
        return None
    mappings: dict[str, tuple[type[Any], Callable[[Any], Any]]] = {
        "decimal": (Decimal, lambda v: str(v) if v.is_finite() else None),
        "date": (date, lambda v: v.isoformat()),
        "datetime": (datetime, lambda v: v.isoformat()),
        "time": (time, lambda v: v.isoformat()),
        "base64": (bytes, lambda v: base64.b64encode(v).decode("ascii")),
        "uuid": (UUID, lambda v: str(v)),
    }
    if mapping is not None:
        expected, convert = mappings[mapping]
        if type(value) is not expected:
            raise ValueError("SQL mapping type mismatch")
        result = convert(value)
        if result is None:
            raise ValueError("Nonfinite SQL value")
        return result
    if type(value) in {str, bool, int} or (type(value) is float and math.isfinite(value)):
        return value
    raise ValueError("Explicit SQL mapping required")


def encode_result(result: Any) -> bytes:
    return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
