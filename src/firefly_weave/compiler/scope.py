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

"""Scope suggestions: which data an expression at a definition path may reference.

The signature and the camelCase result shape are stable so that the Studio host's ``POST /studio/local/scope``
and Studio's offline ``scope.ts`` fallback share one contract. Results come from the compiler's own dominance and
type analysis, including the inferred outputs of ``forEach``, ``callWorkflow`` and ``agent`` steps, which Studio
does not reimplement.
"""

from typing import Final, Literal, cast

from pydantic import Field

from firefly_weave.compiler.analyzer import analyze_scope
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.loops import LoopScope
from firefly_weave.compiler.parser import ParseFailure, parse_source
from firefly_weave.contracts.definitions import (
    ContractModel,
    JsonPointer,
    OmissionOnly,
    ResourceName,
    _is_absent,
    _omit_absent_default,
)
from firefly_weave.contracts.values import JsonObject, JsonObjectData, JsonValue

type ScopeSource = Literal["input", "step", "item", "index"]
type JsonType = Literal["array", "boolean", "integer", "null", "number", "object", "string"]

# Deepest property path expanded below a root, and the most suggestions returned, roots included.
MAX_DEPTH: Final = 4
MAX_ENTRIES: Final = 200
_JSON_TYPES: Final = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
# Studio's value names (studio/src/app/forms/core/type-labels.ts); the shared scope fixtures pin them.
_TYPE_LABELS: Final[dict[str, str]] = {
    "string": "Text",
    "number": "Number",
    "integer": "Whole number",
    "boolean": "Yes/No",
    "object": "Group",
    "array": "List",
    "null": "Empty",
}
_PLURAL_LABELS: Final[dict[str, str]] = {
    "string": "text",
    "number": "numbers",
    "integer": "whole numbers",
    "boolean": "yes/no values",
    "object": "groups",
    "array": "lists",
    "null": "empty values",
}


class ScopeEntry(ContractModel):
    """One suggestion; field names match Studio's ``ScopeEntry`` (``studio/src/app/forms/core/scope.ts``)."""

    ref: JsonPointer
    source: ScopeSource
    # The loop step ID for item and index entries.
    loop: OmissionOnly[ResourceName] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    step_id: OmissionOnly[ResourceName] = Field(
        default=None, alias="stepId", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    step_kind: OmissionOnly[str] = Field(
        default=None, alias="stepKind", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    path: list[str]
    label: str
    breadcrumb: str
    schema_value: JsonObjectData = Field(alias="schema")
    types: list[JsonType]
    type_label: str = Field(alias="typeLabel")
    optional: bool


class ScopeResult(ContractModel):
    found: bool
    evaluated: bool
    truncated: bool
    entries: list[ScopeEntry]


def scope_at(document: JsonObject, path: JsonPointer, *, catalog: CatalogSnapshot | None = None) -> ScopeResult:
    """Suggestions for the expression at ``path`` (an RFC 6901 pointer into ``document``).

    Without a catalog, resources resolve as in offline authoring and their outputs stay unconstrained.
    ``found`` is false when ``path`` is not inside an expression the compiler analyzes, including documents it
    rejects before flow analysis. Entries list the workflow input, the current loop's item and position, enclosing
    loops (inner to outer), then visible step outputs in execution order, each followed by its fields.
    """
    try:
        capture = analyze_scope(parse_source(document, format="object"), path, catalog)
    except ParseFailure:
        capture = None
    if capture is None:
        return ScopeResult(found=False, evaluated=False, truncated=False, entries=[])
    roots: list[JsonObject] = [_root("/input", "input", "Workflow input", "Workflow input", capture.input_schema)]
    for depth, loop in enumerate(reversed(capture.loops)):
        roots.extend(_loop_roots(loop, innermost=depth == 0))
    for identifier, kind, schema in capture.steps:
        step = _root(f"/steps/{_escape(identifier)}/output", "step", identifier, identifier, schema)
        roots.append(step | {"stepId": identifier, "stepKind": kind})
    entries, truncated = _expand(roots)
    return ScopeResult(
        found=True,
        evaluated=capture.evaluated,
        truncated=truncated,
        entries=[ScopeEntry.model_validate(entry) for entry in entries],
    )


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _root(ref: str, source: str, label: str, breadcrumb: str, schema: JsonObject) -> JsonObject:
    return {"ref": ref, "source": source, "path": [], "label": label, "breadcrumb": breadcrumb, "schema": schema}


def _loop_roots(loop: LoopScope, *, innermost: bool) -> list[JsonObject]:
    """The item and position of a loop: ``/item`` and ``/index`` for the innermost, ``/loops/<id>/…`` otherwise."""
    base = "" if innermost else f"/loops/{_escape(loop.id)}"
    prefix = f"Loop {loop.id} › "
    return [
        _root(f"{base}/item", "item", "Current item", prefix + "Current item", loop.item) | {"loop": loop.id},
        _root(f"{base}/index", "index", "Item position (from 0)", prefix + "Item position", loop.index)
        | {"loop": loop.id},
    ]


def _expand(roots: list[JsonObject]) -> tuple[list[JsonObject], bool]:
    """Roots and their fields, breadth first to ``MAX_DEPTH`` and at most ``MAX_ENTRIES``, in display order."""
    accepted: list[tuple[tuple[int, ...], JsonObject, JsonObject]] = []
    for index, root in enumerate(roots[:MAX_ENTRIES]):
        schema = cast(JsonObject, root["schema"])
        accepted.append(((index,), _typed(root, schema), schema))
    truncated = len(roots) > MAX_ENTRIES
    level = list(accepted)
    depth = 1
    while depth <= MAX_DEPTH and level and not truncated:
        following: list[tuple[tuple[int, ...], JsonObject, JsonObject]] = []
        for order, parent, root in level:
            properties = _object_properties(_resolve(cast(JsonObject, parent["schema"]), root), root) or {}
            for position, (key, (schema, required)) in enumerate(properties.items()):
                if len(accepted) >= MAX_ENTRIES:
                    truncated = True
                    break
                schema = _resolve(schema, root)
                title = schema.get("title")
                label = title.strip() if isinstance(title, str) and title.strip() else key
                child = parent | {
                    "ref": f"{parent['ref']}/{_escape(key)}",
                    "path": [*cast(list[str], parent["path"]), key],
                    "label": label,
                    "breadcrumb": f"{parent['breadcrumb']} › {label}",
                    "schema": schema,
                    "optional": parent["optional"] or "null" in cast(list[str], parent["types"]) or not required,
                }
                item = ((*order, position), _typed(child, root), root)
                accepted.append(item)
                following.append(item)
            if truncated:
                break
        level = following
        depth += 1
    accepted.sort(key=lambda item: item[0])
    return [entry for _, entry, _ in accepted], truncated


def _typed(entry: JsonObject, root: JsonObject) -> JsonObject:
    schema = cast(JsonObject, entry["schema"])
    types: list[JsonValue] = [name for name in _schema_types(schema, root) if name in _JSON_TYPES]
    return entry | {"types": types, "typeLabel": _type_label(schema, root), "optional": entry.get("optional", False)}


def _schema_of(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {"not": {}} if value is False else {}


def _resolve(schema: JsonObject, root: JsonObject) -> JsonObject:
    """Follow local ``#/…`` references within ``root``, at most 16 hops."""
    current = schema
    for _ in range(16):
        target = current.get("$ref")
        if not isinstance(target, str) or not target.startswith("#/"):
            return current
        node: JsonValue = root
        for segment in target[2:].split("/"):
            key = segment.replace("~1", "/").replace("~0", "~")
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return current
        current = node
    return current


def _object_properties(
    schema: JsonObject, root: JsonObject, seen: frozenset[int] = frozenset()
) -> dict[str, tuple[JsonObject, bool]] | None:
    """Properties of an object-shaped schema: ``allOf`` merged, local ``$ref`` followed, ``anyOf`` intersected."""
    if id(schema) in seen or len(seen) > 32:
        return None
    seen = seen | {id(schema)}
    resolved = _resolve(schema, root)
    if resolved is not schema:
        return _object_properties(resolved, root, seen)
    alternatives = schema.get("anyOf") if schema.get("anyOf") is not None else schema.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        views = [_object_properties(_schema_of(value), root, seen) for value in alternatives]
        if any(view is None for view in views):
            return None
        first, *rest = cast(list[dict[str, tuple[JsonObject, bool]]], views)
        # A field set on only some paths is rejected by the compiler.
        return {key: value for key, value in first.items() if all(key in view for view in rest)}
    required = set(cast(list[str], schema["required"])) if isinstance(schema.get("required"), list) else set()
    found: dict[str, tuple[JsonObject, bool]] = {}
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, value in properties.items():
            found[key] = (_schema_of(value), key in required)
    parts = schema.get("allOf")
    for part in parts if isinstance(parts, list) else []:
        for key, (value, part_required) in (_object_properties(_schema_of(part), root, seen) or {}).items():
            found[key] = (value, part_required or key in required or (key in found and found[key][1]))
    return {key: (value, value_required or key in required) for key, (value, value_required) in found.items()}


def _json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _schema_types(schema: JsonObject, root: JsonObject) -> list[str]:
    """JSON types a schema admits; empty when unknown."""
    resolved = _resolve(schema, root)
    if "const" in resolved:
        return [_json_type(resolved["const"])]
    if isinstance(resolved.get("enum"), list):
        return _unique([_json_type(value) for value in cast(list[JsonValue], resolved["enum"])])
    kind = resolved.get("type")
    if isinstance(kind, str):
        return [kind]
    if isinstance(kind, list):
        return _unique([str(name) for name in kind])
    alternatives = resolved.get("anyOf") if resolved.get("anyOf") is not None else resolved.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        sets = [_schema_types(_schema_of(value), root) for value in alternatives]
        return [] if any(not item for item in sets) else _unique([name for item in sets for name in item])
    if isinstance(resolved.get("properties"), dict):
        return ["object"]
    if isinstance(resolved.get("items"), dict):
        return ["array"]
    return []


def _type_label(schema: JsonObject, root: JsonObject) -> str:
    """A plain-language description of a schema's type, as Studio's suggestion badges show it."""
    resolved = _resolve(schema, root)
    if "const" in resolved:
        return "Fixed value"
    if isinstance(resolved.get("enum"), list):
        return "Choice"
    types = _schema_types(resolved, root)
    if not types:
        return "Any value"
    if types == ["string"] and resolved.get("format") == "date-time":
        return "Date and time"
    named: list[str] = []
    for name in types:
        if name == "null" and len(types) > 1:
            continue
        if name != "array":
            named.append(_TYPE_LABELS.get(name, name))
            continue
        items = resolved.get("items")
        inner = _schema_types(items, root) if isinstance(items, dict) else []
        named.append(
            f"List of {_PLURAL_LABELS[inner[0]]}" if len(inner) == 1 and inner[0] in _PLURAL_LABELS else "List"
        )
    label = " or ".join(text if position == 0 else text.lower() for position, text in enumerate(named))
    return f"{label} or empty" if "null" in types and len(types) > 1 else label
