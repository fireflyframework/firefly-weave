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

"""Shared strict transport parsing; never rewrites request paths or authority."""

import base64
import json
import re
from functools import cache
from types import NoneType, UnionType
from typing import Annotated, Literal, TypeAliasType, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, ValidationError

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError

CURSOR_V2_LIMIT = 2048


def canonical_path(path: str) -> bool:
    return path.startswith("/api/v1/tenants/")


def parse_revision(value: str | None, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if value is None or not re.fullmatch(r'(?:"[1-9][0-9]{0,9}"|[1-9][0-9]{0,9})', value):
        raise ValueError("A positive revision is required")
    return int(value.strip('"'))


def etag(revision: int, path: str) -> str:
    return f'"{revision}"' if canonical_path(path) else str(revision)


def encode_cursor(scope: Scope, collection: str, identifier: UUID | int) -> str:
    raw = json.dumps([1, scope.model_dump(mode="json"), collection, str(identifier)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, scope: Scope, collection: str) -> UUID | None:
    if value is None:
        return None
    try:
        if len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        version, bound, selected, identifier = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if version != 1 or bound != scope.model_dump(mode="json") or selected != collection:
            raise ValueError()
        result = UUID(identifier)
        if encode_cursor(scope, collection, result) != value:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid scope-bound cursor") from None


def page_request(request: object, scope: Scope, collection: str) -> tuple[int, UUID | None]:
    from starlette.requests import Request

    assert isinstance(request, Request)
    raw = request.query_params.get("cursor")
    cursor = decode_cursor(raw, scope, collection) if canonical_path(request.url.path) else UUID(raw) if raw else None
    return int(request.query_params.get("limit", "50")), cursor


def page_response(result: dict[str, object], scope: Scope, collection: str, path: str) -> dict[str, object]:
    if canonical_path(path) and result["next_cursor"] is not None:
        return {**result, "next_cursor": encode_cursor(scope, collection, UUID(str(result["next_cursor"])))}
    return result


def decode_sequence_cursor(value: str | None, scope: Scope, collection: str) -> int:
    if value is None:
        return 0
    try:
        if len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        version, bound, selected, identifier = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if version != 1 or bound != scope.model_dump(mode="json") or selected != collection:
            raise ValueError()
        if not isinstance(identifier, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", identifier):
            raise ValueError()
        result = int(identifier)
        if result > 9223372036854775807 or encode_cursor(scope, collection, result) != value:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid scope-bound sequence cursor") from None


def encode_cursor_v2(scope: Scope, collection: str, sort_value: JsonValue, identifier: str) -> str:
    """Time-ordered cursor: base64url of [2, scope, collection, sort value, identifier]."""
    raw = json.dumps([2, scope.model_dump(mode="json"), collection, sort_value, identifier], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor_v2(value: str | None, scope: Scope, collection: str) -> tuple[JsonValue, str] | None:
    """The collection carries the filter hash and order, so a changed filter rejects old cursors."""
    if value is None:
        return None
    try:
        if len(value) > CURSOR_V2_LIMIT or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        version, bound, selected, sort_value, identifier = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if version != 2 or bound != scope.model_dump(mode="json") or selected != collection:
            raise ValueError()
        if not isinstance(identifier, str) or encode_cursor_v2(scope, collection, sort_value, identifier) != value:
            raise ValueError()
        return sort_value, identifier
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid scope-bound cursor") from None


def _query_kind(annotation: object) -> Literal["array", "boolean", "integer", "string"]:
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        elif get_origin(annotation) in (Union, UnionType):
            choices = [item for item in get_args(annotation) if item is not NoneType]
            if len(choices) != 1:
                return "string"
            annotation = choices[0]
        else:
            break
    if annotation is list or get_origin(annotation) is list:
        return "array"
    if annotation is bool:
        return "boolean"
    return "integer" if annotation is int else "string"


@cache
def _query_kinds(model: type[BaseModel]) -> dict[str, Literal["array", "boolean", "integer", "string"]]:
    return {name: _query_kind(field.annotation) for name, field in model.model_fields.items()}


def parse_query[M: BaseModel](params: object, model: type[M]) -> M:
    """Strict query decoding: known names only, one value per scalar, JSON-typed values.

    Filter combinations rejected by the model answer 422 WV-FILTER; other invalid values WV-VALIDATION.
    """
    from starlette.datastructures import QueryParams

    assert isinstance(params, QueryParams)
    kinds = _query_kinds(model)
    values: dict[str, object] = {}
    for name in dict.fromkeys(key for key, _ in params.multi_items()):
        if name not in kinds:
            raise ValueError("Unknown query parameter")
        raw = params.getlist(name)
        if kinds[name] == "array":
            values[name] = raw
            continue
        if len(raw) != 1:
            raise ValueError("Repeated query parameter")
        if kinds[name] == "boolean":
            if raw[0] not in {"true", "false"}:
                raise ValueError("A boolean query parameter must be true or false")
            values[name] = raw[0] == "true"
        elif kinds[name] == "integer":
            if not re.fullmatch(r"-?[0-9]{1,9}", raw[0]):
                raise ValueError("An integer query parameter is required")
            values[name] = int(raw[0])
        else:
            values[name] = raw[0]
    try:
        return model.model_validate_json(json.dumps(values))
    except ValidationError as error:
        if any(item["type"] == "weave_filter" for item in error.errors()):
            raise CatalogError(422, "WV-FILTER", "Invalid filter combination or time range") from None
        raise
