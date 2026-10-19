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

"""The fixture catalog J0 publishes: the acme-local connection, the five acme-* actions and risk-tier.

Actions are authored offline on the built-in weave-http@2.0.0 connector from the Acme test
service's own OpenAPI schemas, so the catalog and the service cannot drift. The connection
names the per-run API key by its secret handle only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/acceptance/fixtures"
CONNECTION = "acme-local"
ORIGIN = "http://acme.acceptance.test:8080"
API_KEY_HANDLE = "acme-api-key"
AUTH = {"kind": "api-key", "header": "X-Api-Key"}
HTTP_CONNECTOR = ("weave-http", "2.0.0")
# name, method, path, request body schema, response schema, success status
_ACTIONS = (
    ("acme-get-customer", "GET", "/customers/{id}", None, "Customer", 200),
    ("acme-list-orders", "GET", "/customers/{id}/orders", None, "Orders", 200),
    ("acme-order-status", "GET", "/orders/{id}/status", None, "OrderStatus", 200),
    ("acme-post-approval", "POST", "/approvals", "Approval", "Receipt", 201),
    ("acme-post-notification", "POST", "/notifications", "Notification", "Receipt", 201),
)
ACTION_NAMES = tuple(name for name, *_ in _ACTIONS)


def acme() -> ModuleType:
    """The Acme test service module (its OpenAPI document and data)."""
    if "acme_api_fixture" in sys.modules:
        return sys.modules["acme_api_fixture"]
    spec = importlib.util.spec_from_file_location("acme_api_fixture", FIXTURES / "acme_api/server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inline(value: Any, schemas: dict[str, Any]) -> Any:
    """The schema with every #/components/schemas reference replaced by its definition."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            return _inline(schemas[reference.rsplit("/", 1)[1]], schemas)
        return {key: _inline(item, schemas) for key, item in value.items()}
    if isinstance(value, list):
        return [_inline(item, schemas) for item in value]
    return value


def action_requests() -> list[dict[str, Any]]:
    schemas = acme().openapi()["components"]["schemas"]
    requests = []
    for name, method, path, body, response, status in _ACTIONS:
        request: dict[str, Any] = {
            "name": name,
            "version": "1.0.0",
            "method": method,
            "pathTemplate": path,
            "parameters": (
                [{"name": "id", "location": "path", "type": "string", "array": False, "required": True}]
                if "{id}" in path
                else []
            ),
            "statuses": [status],
            "emptyStatuses": [],
            "timeoutSeconds": 10,
            "stringMaxLength": 256,
            "bodyRequired": body is not None,
            "auth": dict(AUTH),
            "responseSchema": _inline(schemas[response], schemas),
        }
        if body is not None:
            request["bodySchema"] = _inline(schemas[body], schemas)
        requests.append(request)
    return requests


def actions() -> dict[str, dict[str, Any]]:
    from firefly_weave.sdk.http_actions import author_http_action

    documents: dict[str, dict[str, Any]] = {}
    for request in action_requests():
        result = author_http_action(request)
        if not result.ok or result.action is None:
            codes = ", ".join(sorted({d.code for d in result.diagnostics if d.severity == "error"}))
            raise ValueError(f"{request['name']} was refused: {codes}")
        documents[request["name"]] = dict(result.action)
    return documents


def decision_table() -> str:
    return (FIXTURES / "workflows/risk-tier.yaml").read_text(encoding="utf-8")


CHILD_WORKFLOW = "notify-customer"


def child_workflow() -> str:
    """The callable child workflow J0 publishes (J0.8/child-workflow) and activates (J0.9)."""
    return (FIXTURES / f"workflows/{CHILD_WORKFLOW}.yaml").read_text(encoding="utf-8")


async def connector_version(client: Any) -> UUID:
    """The published weave-http@2.0.0 connector version in the demo project."""
    cursor: str | None = None
    for _ in range(20):
        query: dict[str, str | int] = {"limit": 100}
        if cursor:
            query["cursor"] = cursor
        page = await client.invoke("definitions.list", collection="connectors", query=query)
        for item in page.items:
            if (getattr(item, "name", None), getattr(item, "version", None)) == HTTP_CONNECTOR:
                return UUID(str(item.id))
        cursor = page.next_cursor
        if not cursor:
            break
    raise AssertionError("weave-http@2.0.0 is not published in the demo project; run J0.6 first")


def connection_request(version: UUID) -> Any:
    return _request(CONNECTION, ORIGIN, dict(AUTH), {"api_key": API_KEY_HANDLE}, version)


def open_connection_request(name: str, base_url: str, version: UUID) -> Any:
    return _request(name, base_url, {"kind": "none"}, {}, version)


def _request(name: str, base_url: str, auth: dict[str, Any], handles: dict[str, str], version: UUID) -> Any:
    from firefly_weave.compiler.canonical import canonical_bytes
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.sdk.http_actions import build_connection_request

    body = build_connection_request(name, base_url, auth, handles, version)
    return ConnectionRequest.model_validate_json(canonical_bytes(body))
