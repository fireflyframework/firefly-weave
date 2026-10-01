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
from uuid import UUID

from firefly_weave.contracts.access import Scope


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
