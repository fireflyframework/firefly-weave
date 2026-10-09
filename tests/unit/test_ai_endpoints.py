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

"""AI metadata routes expose strict queries, safe metadata and cancellation-owned refreshes."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.surface import OPERATIONS


def controller():
    module = importlib.import_module("firefly_weave.api.ai")
    assert hasattr(module.AIController, "models"), "Model metadata route is unavailable"
    return module.AIController


def test_model_query_is_required_and_defaults_remain_optional():
    assert "ai_models.list" in OPERATIONS
    native = OPERATIONS["ai_models.list"].native()
    params = {item.name: item for item in native.parameters}
    assert params["connection"].required is True
    assert params["refresh"].required is False and params["refresh"].default is False
    bindable = {item.name: item for item in OPERATIONS["bindable_connections.list"].native().parameters}
    assert bindable["limit"].required is False and bindable["limit"].default == 50


@pytest.mark.parametrize(
    "query",
    [
        "",
        "connection=wrong",
        "connection=" + str(uuid4()) + "&refresh=wrong",
        "connection=" + str(uuid4()) + "&refresh=true&refresh=false",
        "connection=" + str(uuid4()) + "&unknown=1",
    ],
)
async def test_model_queries_are_strict_before_service_access(query):
    service = SimpleNamespace(models=AsyncMock())
    request = Request({"type": "http", "query_string": query.encode(), "headers": []})
    with pytest.raises(ValueError):
        await controller()(SimpleNamespace(), service).models(request)
    service.models.assert_not_awaited()


@pytest.mark.parametrize("method", ["endpoints", "models"])
async def test_metadata_routes_return_no_store(method):
    contracts = importlib.import_module("firefly_weave.contracts.ai")
    handler = controller()
    response = (
        contracts.AIEndpointsResult(policy="absent")
        if method == "endpoints"
        else contracts.AIModelsResult(approval="unknown", discovery="unknown")
    )
    service = SimpleNamespace(**{method: AsyncMock(return_value=response)})
    request = Request(
        {
            "type": "http",
            "query_string": ("connection=" + str(uuid4())).encode() if method == "models" else b"",
            "headers": [],
            "path_params": {key: str(uuid4()) for key in ("tenant", "project", "environment")},
        }
    )
    request.state.principal = SimpleNamespace()
    request.state.audit_context = AuditContext()
    answer = await getattr(handler(SimpleNamespace(), service), method)(request)
    assert answer.headers["Cache-Control"] == "no-store"
    assert json.loads(answer.body) == response.model_dump(mode="json")


async def test_native_application_resolves_one_shared_model_service_and_both_routes():
    from starlette.routing import Match

    from firefly_weave import private_origins
    from firefly_weave.api.ai import AIController
    from firefly_weave.api.connections import ConnectionController
    from firefly_weave.app import make_app
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.operations.ai_connections import AIConnectionService
    from firefly_weave.operations.ai_models import AIModelService
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.settings import Settings

    with private_origins.installed(private_origins.PrivateOrigins.empty()):
        app = make_app(Settings(database_url="postgresql+asyncpg://unused@127.0.0.1:1/unused"))
        try:
            context = app.state.pyfly.context
            context.container.register_instance(UnitOfWork, UnitOfWork(app.state.resources.sessions))
            model_service = context.get_bean(AIModelService)
            assert model_service.connections is context.get_bean(ConnectionService)
            assert model_service.tests is context.get_bean(AIConnectionService)
            assert context.get_bean(AIController).model_service is model_service
            assert context.get_bean(ConnectionController).ai_models is model_service
            for suffix, identifier in [("endpoints", "ai_endpoints.list"), ("models", "ai_models.list")]:
                scope = {
                    "type": "http",
                    "method": "GET",
                    "root_path": "",
                    "path": f"/api/v1/tenants/{uuid4()}/projects/{uuid4()}/environments/{uuid4()}/ai/{suffix}",
                }
                route = next(r for r in app.router.routes if r.matches(scope)[0] == Match.FULL)
                assert route.name == identifier
        finally:
            await app.state.resources.close()
            for owner in app.state.telemetry:
                owner.close()


@pytest.mark.parametrize("root_path", ["", "/weave", "/weave/nested"])
@pytest.mark.parametrize("telemetry", [False, True])
@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize(
    "outcome,status",
    [
        ("ok", 200),
        ("auth", 403),
        ("validation", 422),
        ("policy", 503),
        ("body", 413),
        ("head", 200),
    ],
)
async def test_metadata_http_replies_always_disable_storage(root_path, telemetry, canonical, outcome, status):
    from contextlib import nullcontext

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.api.ai import AIController
    from firefly_weave.api.errors import ErrorAdvice
    from firefly_weave.contracts.ai import AIEndpointsResult
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations.transport import BodyBoundary

    async def endpoints(*args, **kwargs):
        if outcome == "auth":
            raise AccessDenied()
        if outcome == "policy":
            raise CatalogError(503, "WV-AI-POLICY", "The AI policy is unavailable")
        return AIEndpointsResult(policy="absent")

    controller = AIController(SimpleNamespace(), SimpleNamespace(endpoints=endpoints))
    advice = ErrorAdvice()

    async def denied(request, error):
        return await advice.access_denied(error)

    async def unavailable(request, error):
        return await advice.catalog_error(error)

    async def invalid(request, error):
        return await advice.invalid_value(error)

    path = OPERATIONS["ai_models.list" if outcome == "validation" else "ai_endpoints.list"]
    template = path.canonical_path if canonical else path.path
    handler = controller.models if outcome == "validation" else controller.endpoints
    app = Starlette(
        routes=[Route(template, handler, methods=["GET"])],
        exception_handlers={
            AccessDenied: denied,
            CatalogError: unavailable,
            ValueError: invalid,
        },
    )
    if telemetry:
        app.state.telemetry_service = SimpleNamespace(span=lambda *a, **kw: nullcontext(), record=lambda *a, **kw: None)
    records = []
    boundary = BodyBoundary(app)

    async def seeded(scope, receive, send):
        scope["app"] = app
        scope["state"] = {"principal": SimpleNamespace(), "audit_context": AuditContext()}

        async def capture(message):
            records.append(message)
            await send(message)

        await boundary(scope, receive, capture)

    url = root_path + template.format(tenant=uuid4(), project=uuid4(), environment=uuid4())
    headers = {"content-length": "6356999"} if outcome == "body" else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=seeded, root_path=root_path), base_url="http://testserver"
    ) as client:
        response = await client.request("HEAD" if outcome == "head" else "GET", url, headers=headers)
    assert response.status_code == status
    assert response.headers.get_list("Cache-Control") == ["no-store"]
    if outcome not in {"ok", "head"}:
        expected = {
            "auth": "WV-FORBIDDEN",
            "validation": "WV-VALIDATION",
            "policy": "WV-AI-POLICY",
            "body": "WV-REQUEST-LIMIT",
        }[outcome]
        assert response.json()["code"] == expected
    else:
        assert response.content == (b"" if outcome == "head" else b'{"policy":"absent","endpoints":[]}')


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/tenants/t/projects/p/environments/e/ai/models", "no-store"),
        ("/tenants/t/projects/p/environments/e/ai/endpoints", "no-store"),
        ("/api/v1/tenants/t/projects/p/environments/e/ai/models/extra", "public"),
        ("/api/v1/tenants/t/projects/p/environments/e/connections", "public"),
    ],
)
async def test_metadata_header_boundary_replaces_case_insensitive_headers_on_exact_paths(path, expected):
    from firefly_weave.operations.transport import BodyBoundary

    output = []

    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"Cache-Control", b"public"),
                    (b"Retry-After", b"17"),
                    (b"ETag", b'"2"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"safe refusal"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        output.append(message)

    await BodyBoundary(inner)({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send)
    assert output[0]["status"] == 401 and output[1]["body"] == b"safe refusal"
    assert [(k.lower(), v) for k, v in output[0]["headers"] if k.lower() == b"cache-control"] == [
        (b"cache-control", expected.encode())
    ]
    assert (b"Retry-After", b"17") in output[0]["headers"] and (b"ETag", b'"2"') in output[0]["headers"]


async def test_metadata_header_boundary_leaves_non_http_protocols_unchanged():
    from firefly_weave.operations.transport import BodyBoundary

    output = []
    scope = {"type": "lifespan"}

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        output.append(message)

    async def inner(given, read, write):
        assert given is scope and read is receive and write is send
        await write({"type": "lifespan.startup.complete"})

    await BodyBoundary(inner)(scope, receive, send)
    assert output == [{"type": "lifespan.startup.complete"}]


@pytest.mark.parametrize("operation", ["ai_models.list", "ai_endpoints.list"])
@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("telemetry", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [200, 503])
async def test_mounted_metadata_replies_preserve_response_and_telemetry(
    operation, canonical, telemetry, method, status
):
    from contextlib import nullcontext

    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from firefly_weave.operations.transport import BodyBoundary

    row = OPERATIONS[operation]
    template = row.canonical_path if canonical else row.path
    payload = {"code": "ok" if status == 200 else "WV-AI-POLICY"}

    async def respond(request):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "public", "X-Control": "preserve"})

    app = Starlette(routes=[Route(template, respond, methods=["GET"])])
    records = []
    if telemetry:
        app.state.telemetry_service = SimpleNamespace(
            span=lambda *a, **kw: nullcontext(), record=lambda *a, **kw: records.append((a, kw))
        )
    app.add_middleware(BodyBoundary)
    path = "/weave" + template.format(tenant=uuid4(), project=uuid4(), environment=uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, root_path="/weave"), base_url="https://testserver"
    ) as client:
        response = await client.request(method, path)
    assert response.status_code == status
    assert response.headers["x-control"] == "preserve"
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert response.content == (b"" if method == "HEAD" else json.dumps(payload, separators=(",", ":")).encode())
    if telemetry:
        assert records and records[0][1]["operation"] == "other"


@pytest.mark.parametrize("operation", ["ai_models.list", "ai_endpoints.list"])
@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_native_mounted_authentication_errors_disable_metadata_storage(monkeypatch, operation, canonical, method):
    import httpx

    from firefly_weave import private_origins
    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.app import make_app
    from firefly_weave.operations.compatibility import CompatibilityService
    from firefly_weave.persistence.resources import DatabaseResources
    from firefly_weave.settings import Settings

    monkeypatch.setattr(DatabaseResources, "check_startup", AsyncMock())
    for name in ("open", "scan", "close"):
        monkeypatch.setattr(CompatibilityService, name, AsyncMock())
    row = OPERATIONS[operation]
    template = row.canonical_path if canonical else row.path
    path = template.format(tenant=uuid4(), project=uuid4(), environment=uuid4())
    with private_origins.installed(private_origins.PrivateOrigins.empty()):
        app = make_app(
            Settings(scheduler_enabled=False, database_url="postgresql+asyncpg://unused@127.0.0.1:1/unused"),
            verifiers=VerifierSet(()),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app, root_path="/weave"), base_url="https://testserver"
            ) as client:
                unrelated = await client.request(method, "/weave" + path + "/extra")
                prefixed = await client.request(method, "/weave" + path)
            assert unrelated.status_code == 401 and "cache-control" not in unrelated.headers
            assert prefixed.status_code == 401
            assert prefixed.headers.get_list("cache-control") == ["no-store"]
            if method == "GET":
                assert prefixed.json()["code"] == "WV-UNAUTHENTICATED"
            else:
                assert prefixed.content == b""


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("operation", ["ai_models.list", "ai_endpoints.list"])
@pytest.mark.parametrize(
    "root_path,prefix,suffix,expected",
    [
        ("/weave", "/weave", "", True),
        ("/weave/nested", "/weave/nested", "", True),
        ("/weave", "", "", True),
        ("/weave", "/weave/weave", "", False),
        ("/weave", "/weaver", "", False),
        ("/weave", "/other/weave", "", False),
        ("/weave", "/weave", "/extra", False),
        ("/weave/", "/weave", "", False),
    ],
)
async def test_metadata_matching_uses_one_exact_router_mount(root_path, prefix, suffix, expected, canonical, operation):
    from firefly_weave.operations.transport import BodyBoundary

    row = OPERATIONS[operation]
    template = row.canonical_path if canonical else row.path
    path = prefix + template.format(tenant="t", project="p", environment="e") + suffix
    messages = []

    async def inner(scope, receive, send):
        assert scope["path"] == path and scope["root_path"] == root_path
        await send({"type": "http.response.start", "status": 403, "headers": [(b"Cache-Control", b"public")]})
        await send({"type": "http.response.body", "body": b"safe refusal"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    await BodyBoundary(inner)(
        {"type": "http", "method": "GET", "root_path": root_path, "path": path, "headers": []}, receive, send
    )
    assert messages[0]["status"] == 403 and messages[1]["body"] == b"safe refusal"
    assert [(name.lower(), value) for name, value in messages[0]["headers"] if name.lower() == b"cache-control"] == [
        (b"cache-control", b"no-store" if expected else b"public")
    ]
