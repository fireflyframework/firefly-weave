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


"""Schemas the platform provides to every workflow, which the compiler supplies when a definition names them.

``weave.platform-event-v1`` is the authoring view of the published ``platform-event-v1`` contract: its references
inlined, its identifiers dropped, and the instance-key pattern, which is too costly for the compiler's schema
profile, replaced by a length bound. The platform builds and validates the real event with its own model, so the view
only has to accept every event the model accepts. Its bytes are frozen: artifacts pin it by digest, so a change to the
event is a new view under a new name, never an edit.

A definition carries a platform schema only when it references one, so every other artifact keeps its bytes.
"""

import json
from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Final, cast

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.schemas import contract_schema
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA
from firefly_weave.contracts.values import JsonObject, JsonValue

_INSTANCE_KEY: Final = rf"^(?:{INSTANCE_KEY_PATTERN})$"


def _inline(schema: JsonObject) -> JsonObject:
    """The schema with every local ``#/$defs/...`` reference replaced by its definition; the contract has no cycles."""
    definitions = cast(JsonObject, schema.get("$defs", {}))

    def walk(node: JsonValue, depth: int) -> JsonValue:
        if depth > 64:
            raise RuntimeError("The platform event contract nests deeper than its view allows")
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        rest = {key: walk(value, depth + 1) for key, value in node.items() if key not in {"$ref", "$defs"}}
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = walk(definitions[reference.removeprefix("#/$defs/")], depth + 1)
            return {**cast(JsonObject, target), **rest}
        return rest

    return cast(JsonObject, walk(schema, 0))


def _authoring(node: JsonValue) -> tuple[JsonValue, int]:
    """The node without root identifiers, and with each instance-key pattern replaced by a nonempty bound."""
    if isinstance(node, list):
        items = [_authoring(item) for item in node]
        return [item for item, _ in items], sum(count for _, count in items)
    if not isinstance(node, dict):
        return node, 0
    if node.get("pattern") == _INSTANCE_KEY:
        return {key: value for key, value in node.items() if key != "pattern"} | {"minLength": 1}, 1
    fields = {key: _authoring(value) for key, value in node.items() if key not in {"$id", "$schema"}}
    return {key: value for key, (value, _) in fields.items()}, sum(count for _, count in fields.values())


@cache
def _event_view_text() -> str:
    view, replaced = _authoring(_inline(contract_schema("platform-event-v1")))
    text = json.dumps(view, sort_keys=True)
    if not replaced or "$ref" in text:
        raise RuntimeError("The platform event contract changed; publish a new view instead of editing this one")
    return text


def platform_event_view() -> JsonObject:
    """The platform event as workflows read it, as an object no other caller shares."""
    return cast(JsonObject, json.loads(_event_view_text()))


_SCHEMAS: Final = MappingProxyType({PLATFORM_EVENT_SCHEMA: platform_event_view})


def platform_schema(name: str) -> JsonObject | None:
    """The platform's schema called ``name``, or None when the platform provides none by that name."""
    view = _SCHEMAS.get(name)
    return None if view is None else view()


@cache
def platform_schema_digests() -> Mapping[str, str]:
    return MappingProxyType({name: FrozenDocument.from_value(view()).digest for name, view in _SCHEMAS.items()})


def platform_references(document: JsonValue) -> dict[str, str]:
    """The platform schemas a definition references, each with the path of its first reference in sorted order."""
    found: dict[str, str] = {}
    stack: list[tuple[JsonValue, str]] = [(document, "")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str):
                name = reference.partition("#")[0]
                if name in _SCHEMAS:
                    candidate = pointer_child(path, "$ref")
                    found[name] = min(found.get(name, candidate), candidate)
            stack.extend((value, pointer_child(path, key)) for key, value in node.items())
        elif isinstance(node, list):
            stack.extend((value, pointer_child(path, str(index))) for index, value in enumerate(node))
    return found
