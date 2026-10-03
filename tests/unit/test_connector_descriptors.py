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

"""Installed connector descriptors are discoverable read-only, with their exact publication source."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.api.connector_descriptors import ConnectorDescriptorController
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.connections.descriptors import PUBLISHED, ConnectorDescriptorService
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR, POSTGRES_DESCRIPTOR
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connector_descriptors import ConnectorDescriptorView
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.definitions.models import CatalogError

PUBLISHED_ID = uuid4()


class NeverExecute:
    async def execute(self, *args):
        raise AssertionError("Discovery never executes connectors")

    async def test_connection(self, *args):
        raise AssertionError("Discovery never tests connections")


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class Session:
    def __init__(self):
        self.statements = []

    async def execute(self, statement, values=None):
        self.statements.append((str(statement), values))
        return Rows([{"id": PUBLISHED_ID, "definition_digest": HTTP_PROFILE_DESCRIPTOR.manifest.digest}])


class Definitions:
    def __init__(self, allowed=True):
        self.allowed, self.checks, self.session = allowed, [], Session()

    def require(self, actor, scope, capability, context):
        self.checks.append(capability)
        if not self.allowed:
            raise AccessDenied()

    @asynccontextmanager
    async def transaction(self, scope, supplied, *, mutation=True):
        assert supplied is None and mutation is False
        yield SimpleNamespace(session=self.session, scope=scope)


@pytest.fixture
def registry():
    value = ConnectorRegistry()
    for descriptor in (HTTP_PROFILE_DESCRIPTOR, HTTP_DESCRIPTOR, POSTGRES_DESCRIPTOR):
        value.register_descriptor(descriptor, NeverExecute())
    return value


@pytest.fixture
def actor(monkeypatch):
    import firefly_weave.access.repository as repository

    principal = Principal(id=uuid4(), kind="human")

    async def load_principal(session, identifier):
        return principal

    monkeypatch.setattr(repository, "load_principal", load_principal)
    return principal


SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4())
CONTEXT = AuditContext(request_id=uuid4())


async def test_list_returns_installed_descriptors_with_publication_match(registry, actor):
    definitions = Definitions()
    page = await ConnectorDescriptorService(definitions, registry).list(actor, SCOPE, context=CONTEXT)
    assert [item["adapter"] for item in page["items"]] == ["weave-http", "weave-http-v2", "weave-postgresql"]
    assert page["next_cursor"] is None
    by_adapter = {item["adapter"]: item for item in page["items"]}
    assert by_adapter["weave-http-v2"]["published_version_id"] == str(PUBLISHED_ID)
    assert by_adapter["weave-http"]["published_version_id"] is None
    assert definitions.checks == ["catalog.read", "catalog.read"]
    sql, values = definitions.session.statements[0]
    assert sql == PUBLISHED and "definition_retirements" in sql
    assert values["tenant"] == SCOPE.tenant_id and values["project"] == SCOPE.project_id


async def test_list_pages_by_adapter_name_and_validates_inputs(registry, actor):
    service = ConnectorDescriptorService(Definitions(), registry)
    first = await service.list(actor, SCOPE, limit=2, context=CONTEXT)
    assert [item["adapter"] for item in first["items"]] == ["weave-http", "weave-http-v2"]
    assert first["next_cursor"] == "weave-http-v2"
    rest = await service.list(actor, SCOPE, limit=2, cursor=first["next_cursor"], context=CONTEXT)
    assert [item["adapter"] for item in rest["items"]] == ["weave-postgresql"] and rest["next_cursor"] is None
    for kwargs in ({"limit": 0}, {"limit": 101}, {"cursor": "../x"}):
        with pytest.raises(CatalogError) as failure:
            await service.list(actor, SCOPE, context=CONTEXT, **kwargs)
        assert failure.value.status == 422


async def test_read_returns_the_exact_publication_source(registry, actor):
    view = await ConnectorDescriptorService(Definitions(), registry).read(
        actor, SCOPE, "weave-http-v2", context=CONTEXT
    )
    assert view.digest == HTTP_PROFILE_DESCRIPTOR.manifest.digest
    assert view.reference == "weave-http@2.0.0" and view.implementation_version == "2.0.0"
    assert view.published_version_id == PUBLISHED_ID
    # Publishing ``source`` as JSON yields exactly the digest the installed registry requires.
    model = load_definition(json.loads(view.source))
    assert FrozenDocument.from_value(model.model_dump(by_alias=True)).digest == view.digest
    assert view.manifest == HTTP_PROFILE_DESCRIPTOR.manifest.value
    result = compile_source(
        view.source, format="json", catalog=CatalogSnapshot.from_definitions([], adapters=[view.adapter])
    )
    assert result.ok, result.diagnostics
    assert [action.name for action in view.actions] == ["read", "write"]
    assert {action.name: action.side_effect for action in view.actions} == {
        "read": "read_only",
        "write": "non_idempotent",
    }
    assert view.connection.auth_schema["properties"].keys() >= {"api_key", "token"}
    assert {binding.task_reference for binding in view.bindings} == {
        "weave-connector-http-read@2.0.0",
        "weave-connector-http-write@2.0.0",
    }
    wire = view.model_dump(mode="json", by_alias=True)
    assert wire["capabilities"][0]["taskType"] == "weave-connector-http-read"
    ConnectorDescriptorView.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize("adapter", ["missing", "Weave HTTP", "../weave-http", "-x"])
async def test_unknown_or_malformed_adapters_are_not_found_after_authorization(registry, actor, adapter):
    definitions = Definitions()
    with pytest.raises(CatalogError) as failure:
        await ConnectorDescriptorService(definitions, registry).read(actor, SCOPE, adapter, context=CONTEXT)
    assert failure.value.status == 404 and definitions.checks == ["catalog.read", "catalog.read"]


async def test_retired_adapters_are_not_discoverable(registry, actor):
    registry.retire("weave-http")
    service = ConnectorDescriptorService(Definitions(), registry)
    page = await service.list(actor, SCOPE, context=CONTEXT)
    assert "weave-http" not in [item["adapter"] for item in page["items"]]
    with pytest.raises(CatalogError):
        await service.read(actor, SCOPE, "weave-http", context=CONTEXT)


async def test_catalog_read_is_required_before_any_lookup(registry, actor):
    definitions = Definitions(allowed=False)
    service = ConnectorDescriptorService(definitions, registry)
    with pytest.raises(AccessDenied):
        await service.list(actor, SCOPE, context=CONTEXT)
    with pytest.raises(AccessDenied):
        await service.read(actor, SCOPE, "weave-http-v2", context=CONTEXT)
    assert definitions.session.statements == []


def request(path_params, query=b""):
    value = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tenants/t/projects/p/connector-descriptors",
            "query_string": query,
            "headers": [],
            "path_params": path_params,
        }
    )
    value.state.principal = Principal(id=uuid4(), kind="human")
    value.state.audit_context = CONTEXT
    return value


async def test_controller_maps_scope_paging_and_adapter(registry, actor):
    controller = ConnectorDescriptorController(ConnectorDescriptorService(Definitions(), registry))
    params = {"tenant": str(SCOPE.tenant_id), "project": str(SCOPE.project_id)}
    page = json.loads((await controller.list(request(params, b"limit=1"))).body)
    assert [item["adapter"] for item in page["items"]] == ["weave-http"] and page["next_cursor"] == "weave-http"
    with pytest.raises(CatalogError):
        await controller.list(request(params, b"limit=many"))
    body = json.loads((await controller.read(request({**params, "adapter": "weave-http-v2"}))).body)
    assert body["adapter"] == "weave-http-v2" and body["published_version_id"] == str(PUBLISHED_ID)


async def test_sdk_reads_descriptors_by_validated_adapter():
    from firefly_weave.contracts.connector_descriptors import descriptor_view
    from firefly_weave.sdk.client import WeaveClient

    view = descriptor_view(HTTP_PROFILE_DESCRIPTOR, PUBLISHED_ID).model_dump(mode="json", by_alias=True)
    calls = []

    def receive(http_request):
        calls.append(http_request)
        if http_request.url.path.endswith("/connector-descriptors"):
            return httpx.Response(200, json={"items": [view], "next_cursor": None})
        return httpx.Response(200, json=view)

    async with WeaveClient(
        "https://api.example", lambda: "access", SCOPE, transport=httpx.MockTransport(receive)
    ) as sdk:
        page = await sdk.list_connector_descriptors(limit=10)
        single = await sdk.read_connector_descriptor("weave-http-v2")
        with pytest.raises(ValueError):
            await sdk.read_connector_descriptor("../admin")
    assert page.items[0].digest == single.digest == HTTP_PROFILE_DESCRIPTOR.manifest.digest
    assert [call.url.path for call in calls] == [
        f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/connector-descriptors",
        f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/connector-descriptors/weave-http-v2",
    ]
    assert calls[0].url.params["limit"] == "10"


async def test_native_routes_send_descriptor_paths_to_discovery_not_the_catalog_matcher():
    from starlette.routing import Match

    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    app = make_app(Settings(database_url="postgresql+asyncpg://user@localhost:1234/db"))
    try:
        project = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}"

        def route(path):
            scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
            return next(r.name for r in app.router.routes if r.matches(scope)[0] == Match.FULL)

        assert route(project + "/connector-descriptors") == "connector_descriptors.list"
        assert route(project + "/connector-descriptors/weave-http-v2") == "connector_descriptors.read"
        assert route(project + "/workflows") == "definitions.list"
    finally:
        await app.state.resources.close()
