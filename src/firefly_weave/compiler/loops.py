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

"""Typing rules of ``forEach``: the schemas of ``/item``, ``/index`` and the collected output.

A loop body reads the current element as ``/item`` and its zero-based position as ``/index``; any enclosing loop is
``/loops/<loop id>/item`` and ``/loops/<loop id>/index``. These roots exist only inside loop bodies.
"""

from dataclasses import dataclass
from typing import Final, cast

from firefly_weave.compiler.expression_types import nonnull_schema
from firefly_weave.contracts.values import JsonObject, JsonValue

LOOP_ROOTS: Final = frozenset({"item", "index", "loops"})
# Tasks one turn of a run can issue; nested loop concurrency above it is spread over several turns.
MAX_CONCURRENT_TASKS: Final = 1000
_COMPOSITION: Final = ("$ref", "allOf", "anyOf", "oneOf", "not", "if", "dependentSchemas", "const", "enum")


@dataclass(frozen=True)
class LoopScope:
    """One enclosing loop as an expression inside its body sees it."""

    id: str
    item: JsonObject
    index: JsonObject
    concurrency: int
    max_items: int
    # References to the item are not checked: the items are already wrong, or unknown until the catalog is present.
    quiet: bool = False


def item_schema(items: JsonObject) -> JsonObject:
    """The schema of one element of ``items``: ``items``, ``anyOf`` the tuple positions, or ``{}`` when unknown."""
    if items.get("type") != "array" or any(keyword in items for keyword in _COMPOSITION):
        return {}
    rest = items.get("items", True)
    prefix = items.get("prefixItems", [])
    if not isinstance(prefix, list) or not all(isinstance(position, dict) for position in prefix):
        return {}
    if not prefix:
        return rest if isinstance(rest, dict) else {}
    if isinstance(rest, dict):
        alternatives = [*prefix, rest]
    elif rest is False:
        alternatives = list(prefix)
    else:
        return {}
    return cast(JsonObject, alternatives[0]) if len(alternatives) == 1 else {"anyOf": cast(JsonValue, alternatives)}


def index_schema(max_items: int) -> JsonObject:
    """``/index``: a position from 0 to ``max_items - 1``."""
    return {"type": "integer", "minimum": 0, "maximum": max_items - 1}


def output_schema(body: JsonObject, max_items: int, collect: str) -> JsonObject:
    """The list a loop returns: one body result per element, in element order, without nulls for ``nonNull``."""
    result = body
    if collect == "nonNull":
        narrowed = nonnull_schema(body)
        if narrowed is None:
            return {"type": "array", "maxItems": 0}
        result = narrowed
    return {"type": "array", "items": result, "maxItems": max_items}


def loops_schema(loops: tuple[LoopScope, ...]) -> JsonObject:
    """The ``/loops`` root: each enclosing loop by its step ID, with its item and index."""

    def closed(properties: dict[str, JsonValue]) -> JsonObject:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    return closed({loop.id: closed({"item": loop.item, "index": loop.index}) for loop in loops})
