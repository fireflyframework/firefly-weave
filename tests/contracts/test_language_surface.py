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

"""One language manifest across the platform operation, schema, OpenAPI, SDK, CLI and the Studio host."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from starlette.requests import Request
from starlette.testclient import TestClient

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.language import language_manifest

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def controller_fixture():
    from pyfly.container import Container

    from firefly_weave.api.compiler import CompilerController
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.settings import Settings

    settings = Settings(database_url="postgresql+asyncpg://test:never-expose-this@localhost/test")
    service = SimpleNamespace(catalog=AsyncMock(), capabilities=SimpleNamespace(resources={}))
    graph = Container()
    graph.register_instance(Settings, settings)
    graph.register_instance(DefinitionService, service)
    graph.register(CompilerController)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    request = Request(
        {
            "type": "http",
            "path_params": {"tenant": str(scope.tenant_id), "project": str(scope.project_id)},
            "state": {"principal": object(), "audit_context": object()},
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )
    return graph.resolve(CompilerController), request, service, scope


async def test_language_read_is_authorized_like_capabilities_and_matches_every_declaration(monkeypatch):
    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.schema_export import export_schemas
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.sdk import client

    controller, request, service, scope = controller_fixture()
    payload = await controller.language(request)
    service.catalog.assert_awaited_once_with(request.state.principal, scope, context=request.state.audit_context)
    assert payload == language_manifest().model_dump(mode="json")
    operation = OPERATIONS["language.read"]
    assert (operation.method, operation.canonical_path, operation.capability) == (
        "GET",
        "/api/v1/tenants/{tenant}/projects/{project}/language",
        "catalog.read",
    )
    Draft202012Validator(export_schemas()["language-manifest"]).validate(payload)
    spec = export_openapi()
    schema = spec["paths"][operation.canonical_path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": spec["components"]}).validate(payload)

    original_client = client.WeaveClient
    calls = []

    def receive(http_request):
        calls.append(http_request)
        assert http_request.url.path.endswith("/language")
        return httpx.Response(200, json=payload)

    def connected(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(receive))

    async with connected("https://api.invalid", lambda: "test-token", scope) as sdk:
        assert (await sdk.language()) == language_manifest()
    monkeypatch.setattr(client, "WeaveClient", connected)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "test-token")
    arguments = ["remote", "language", "--base-url", "https://api.invalid"]
    arguments += ["--tenant", str(scope.tenant_id), "--project", str(scope.project_id)]
    result = await asyncio.to_thread(CliRunner().invoke, cli, arguments)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload
    assert len(calls) == 2


def test_studio_host_serves_the_manifest_only_to_a_paired_session(tmp_path: Path):
    from firefly_weave.studio.host import make_studio_app
    from firefly_weave.studio.service import StudioOptions

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    app = make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE))
    with TestClient(app, base_url=ORIGIN) as browser:
        assert browser.get("/studio/contracts/language").status_code == 401
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 200
        response = browser.get("/studio/contracts/language")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == language_manifest().model_dump(mode="json")
