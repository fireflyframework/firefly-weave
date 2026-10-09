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

"""Resource-scoped discovery and binding share current authority without exposing configuration."""

import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.uow import Transaction


def modules():
    return (
        importlib.import_module("firefly_weave.connections.bindable"),
        importlib.import_module("firefly_weave.contracts.bindable_connections"),
    )


def principal(scope, resources=()):
    return Principal(id=uuid4(), kind="human", grants=(Grant(role="deployer", scope=scope, resources=resources),))


def revision(number, **changes):
    values = dict(
        id=UUID(int=number),
        revision=1,
        name=f"connection-{number}",
        connector_version_id=uuid4(),
        connector=CONNECTOR_REFERENCE,
        connector_digest=AGENTIC_DESCRIPTOR.manifest.digest,
        adapter="weave-agentic-provider",
        config={"provider": "openai-chat", "endpoint": "https://model.example/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "secret-canary"},
        allowed_destinations=("https://model.example",),
    )
    values.update(changes)
    return ConnectionRevision.model_validate(values)


@pytest.fixture
def catalog(monkeypatch):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    state = SimpleNamespace(scope=scope, records=[revision(i) for i in range(1, 206)], sizes={}, queries=[], opened=0)
    state.actor = principal(scope, (str(UUID(int=101)), str(UUID(int=205))))
    state.current = state.actor
    active = SimpleNamespace(is_active=True)

    async def execute(statement, values):
        state.queries.append((str(statement), values))
        assert values["tenant"] == scope.tenant_id and values["project"] == scope.project_id
        assert values["environment"] == scope.environment_id
        rows = [
            r
            for r in state.records
            if (values["after"] is None or r.id > values["after"])
            and (values["connector"] is None or r.connector == values["connector"])
        ]
        return SimpleNamespace(
            all=lambda: [SimpleNamespace(id=r.id, logical_bytes=state.sizes.get(r.id, 100)) for r in rows[:101]]
        )

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=execute),
        get_transaction=lambda: active,
        scalar=AsyncMock(return_value=str(scope.tenant_id)),
    )
    tx = Transaction(session, scope)

    @asynccontextmanager
    async def opened(selected_scope, *, mutation=True):
        assert selected_scope == scope and mutation is False
        state.opened += 1
        yield tx

    definitions = object.__new__(DefinitionService)
    definitions.authorization = AuthorizationService()
    definitions.uow = SimpleNamespace(open=opened)
    state.connection = ConnectionService(
        definitions.uow, definitions, ConnectorRegistry(), SimpleNamespace(check=lambda *a, **kw: None)
    )
    state.ready = AsyncMock()
    monkeypatch.setattr(state.connection, "_ready", state.ready)
    state.load = AsyncMock(side_effect=lambda *args: state.current)
    monkeypatch.setattr("firefly_weave.access.repository.load_principal", state.load)
    monkeypatch.setattr("firefly_weave.connections.service.load_principal", state.load)
    monkeypatch.setattr(modules()[0], "load_principal", state.load)
    state.fetch = AsyncMock(side_effect=lambda identifier: next(r for r in state.records if r.id == identifier))
    monkeypatch.setattr("firefly_weave.connections.repository.ConnectionRepository.revision", state.fetch)
    state.tx = tx
    return state


async def listing(state, **query):
    service, contracts = modules()
    return await service.BindableConnectionService(state.connection).list(
        state.actor, state.scope, contracts.BindableConnectionQuery(**query), context=AuditContext()
    )


def test_binding_requires_the_revision_in_both_principal_snapshots():
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    selected, other = uuid4(), uuid4()
    actor = principal(scope, (str(selected),))
    current = actor.model_copy(update={"grants": (Grant(role="deployer", scope=scope, resources=(str(other),)),)})
    service = object.__new__(ConnectionService)
    service.definitions = SimpleNamespace(authorization=AuthorizationService())
    service.require_revision(actor, actor, scope, selected, "connection.bind", AuditContext())
    with pytest.raises(AccessDenied):
        service.require_revision(actor, current, scope, selected, "connection.bind", AuditContext())


async def test_filtering_advances_past_denied_pages_and_never_reads_hidden_payloads(catalog):
    first = await listing(catalog, limit=1)
    assert [item.revision_id for item in first.items] == [UUID(int=101)]
    assert first.next_cursor
    second = await listing(catalog, limit=1, cursor=first.next_cursor)
    assert [item.revision_id for item in second.items] == [UUID(int=205)]
    assert second.next_cursor is None
    assert [call.args[0] for call in catalog.fetch.await_args_list] == [UUID(int=101), UUID(int=205)]
    encoded = first.model_dump_json() + second.model_dump_json()
    assert "secret-canary" not in encoded and "secretRef" not in encoded and "config" not in encoded
    assert "connection-100" not in encoded
    assert first.items[0].endpoint_origin == "https://model.example:443"
    assert isinstance(json.loads(first.model_dump_json())["items"][0]["endpoint_origin"], str)


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_revoked_revision_is_filtered_from_either_authority_snapshot(catalog, snapshot):
    value = getattr(catalog, snapshot)
    setattr(
        catalog,
        snapshot,
        value.model_copy(
            update={"grants": (Grant(role="deployer", scope=catalog.scope, resources=(str(UUID(int=205)),)),)}
        ),
    )
    page = await listing(catalog)
    assert [item.revision_id for item in page.items] == [UUID(int=205)]


@pytest.mark.parametrize("change", ["inactive", "no-grants", "other-environment", "worker", "no-environment"])
async def test_requests_without_current_bind_authority_fail_before_candidate_query(catalog, change):
    if change == "inactive":
        catalog.current = catalog.current.model_copy(update={"active": False})
    elif change == "no-grants":
        catalog.current = catalog.current.model_copy(update={"grants": ()})
    elif change == "worker":
        catalog.current = catalog.current.model_copy(update={"kind": "worker"})
    elif change == "no-environment":
        catalog.scope = Scope(tenant_id=catalog.scope.tenant_id, project_id=catalog.scope.project_id)
    else:
        other = catalog.scope.model_copy(update={"environment_id": uuid4()})
        catalog.current = principal(other)
    with pytest.raises(AccessDenied):
        await listing(catalog)
    assert not catalog.queries and catalog.fetch.await_count == 0


@pytest.mark.parametrize("change", ["connector", "principal", "scope"])
async def test_cursor_is_bound_to_filter_principal_and_scope(catalog, change):
    page = await listing(catalog, limit=1)
    catalog.queries.clear()
    query = {"cursor": page.next_cursor}
    if change == "connector":
        query["connector"] = CONNECTOR_REFERENCE
    elif change == "principal":
        catalog.actor = catalog.actor.model_copy(update={"id": uuid4()})
    else:
        catalog.scope = catalog.scope.model_copy(update={"environment_id": uuid4()})
    with pytest.raises(ValueError):
        await listing(catalog, **query)
    assert not catalog.queries


@pytest.mark.parametrize("cursor", ["invalid", "x" * 2049])
async def test_malformed_cursor_is_rejected_before_storage(catalog, cursor):
    with pytest.raises(ValueError):
        await listing(catalog, cursor=cursor)
    assert not catalog.queries and catalog.opened == 0


async def test_cursor_rejects_noncanonical_uuid_and_sort_value(catalog):
    import base64

    from firefly_weave.api.transport import encode_cursor_v2

    page = await listing(catalog, limit=1)
    parts = json.loads(base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4)))
    for sort, identifier in [("wrong", parts[4]), (None, UUID(parts[4]).hex)]:
        with pytest.raises(ValueError):
            await listing(catalog, cursor=encode_cursor_v2(catalog.scope, parts[2], sort, identifier))


async def test_scan_budget_returns_continuation_without_dropping_the_next_candidate(catalog):
    catalog.records = [revision(i) for i in range(1, 1002)]
    catalog.actor = principal(catalog.scope, (str(UUID(int=1001)),))
    catalog.current = catalog.actor
    page = await listing(catalog)
    assert page.items == [] and page.next_cursor
    assert len(catalog.queries) == 10 and catalog.fetch.await_count == 0
    final = await listing(catalog, cursor=page.next_cursor)
    assert [item.revision_id for item in final.items] == [UUID(int=1001)]
    assert final.next_cursor is None


@pytest.mark.parametrize("sizes", [{1: 8 * 1024 * 1024 + 1}, {1: 4 * 1024 * 1024, 101: 4 * 1024 * 1024 + 1}])
async def test_selected_bytes_are_bounded_before_payload_fetch(catalog, sizes):
    catalog.sizes = {UUID(int=k): v for k, v in sizes.items()}
    with pytest.raises(CatalogError) as error:
        await listing(catalog)
    assert (error.value.status, error.value.code) == (429, "WV-PAGE-LIMIT")
    catalog.fetch.assert_not_awaited()


async def test_connector_filter_is_applied_before_projection(catalog):
    catalog.records[100] = catalog.records[100].model_copy(update={"connector": "other@1.0.0"})
    page = await listing(catalog, connector=CONNECTOR_REFERENCE)
    assert [item.revision_id for item in page.items] == [UUID(int=205)]
    sql, params = catalog.queries[0]
    assert "payload->>'connector'" in sql and params["connector"] == CONNECTOR_REFERENCE
    assert "ORDER BY id LIMIT" in sql and "SELECT id, octet_length(payload::text)" in sql


@pytest.mark.parametrize("code", ["WV-CONNECTION", "WV-LEGACY-UNAVAILABLE", "WV-NOT-FOUND"])
async def test_known_unavailable_revisions_keep_only_safe_authorized_metadata(catalog, code):
    catalog.ready.side_effect = CatalogError(422, code, "private-readiness-canary")
    page = await listing(catalog)
    assert len(page.items) == 2 and all(not item.enabled for item in page.items)
    assert "private-readiness-canary" not in page.model_dump_json()


async def test_unexpected_readiness_failure_is_not_reported_as_disabled(catalog):
    catalog.ready.side_effect = CatalogError(500, "WV-INTERNAL", "Unexpected failure")
    with pytest.raises(CatalogError, match="Unexpected failure"):
        await listing(catalog)


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("https://MODEL.example/v1?ignored=canary", "https://model.example:443"),
        ("https://model.example:443/v1", "https://model.example:443"),
        ("https://model.example:8443/v1", "https://model.example:8443"),
        ("http://[::1]:11434/v1", "http://[::1]:11434"),
        ("https://[2001:db8::1]/v1", "https://[2001:db8::1]:443"),
        ("https://user:secret@model.example/v1", None),
        ("https://model.example:0/v1", None),
        ("https://[broken/v1", None),
        ("not-a-url", None),
    ],
)
def test_endpoint_origin_is_only_a_validated_origin_string(endpoint, expected):
    assert modules()[0].endpoint_origin(endpoint) == expected


async def test_invalid_stored_endpoint_is_omitted_and_disabled(catalog):
    catalog.records[100] = catalog.records[100].model_copy(
        update={"config": {"provider": "openai-chat", "endpoint": "https://user:private-canary@model.example/v1"}}
    )
    page = await listing(catalog, limit=1)
    assert page.items[0].enabled is False
    assert "endpoint_origin" not in page.items[0].model_dump()
    assert "private-canary" not in page.model_dump_json()


@pytest.mark.parametrize("field,value", [("connector", "other@1.0.0"), ("connector_digest", "sha256:" + "0" * 64)])
async def test_ai_metadata_requires_exact_connector_reference_and_manifest_digest(catalog, field, value):
    catalog.records[100] = catalog.records[100].model_copy(update={field: value})
    page = await listing(catalog, limit=1)
    assert "provider" not in page.items[0].model_dump() and "endpoint_origin" not in page.items[0].model_dump()


@pytest.mark.parametrize("revoked", [False, True])
async def test_actual_binding_uses_revision_authority_and_current_principal(catalog, revoked):
    if revoked:
        catalog.current = catalog.current.model_copy(update={"grants": ()})
        with pytest.raises(AccessDenied):
            await catalog.connection.resolve_binding(
                catalog.actor, catalog.scope, "model", UUID(int=101), CONNECTOR_REFERENCE, context=AuditContext()
            )
        catalog.fetch.assert_not_awaited()
    else:
        bound = await catalog.connection.resolve_binding(
            catalog.actor, catalog.scope, "model", UUID(int=101), CONNECTOR_REFERENCE, context=AuditContext()
        )
        assert bound.revision.id == UUID(int=101)
        assert catalog.ready.await_args.kwargs["resource"] == str(UUID(int=101))
        with pytest.raises(SecretUnavailable):
            bound.credentials("apiKey")


@pytest.mark.parametrize("unavailable", [None, "retired", "missing-adapter", "revoked-secret"])
async def test_resource_authority_passes_through_real_connector_readiness(catalog, monkeypatch, unavailable):
    monkeypatch.setattr(catalog.connection, "_ready", ConnectionService._ready.__get__(catalog.connection))
    manifest = AGENTIC_DESCRIPTOR.manifest.value
    row = {
        "kind": "Connector",
        "retired": unavailable == "retired",
        "definition_digest": AGENTIC_DESCRIPTOR.manifest.digest,
        "document": manifest,
        "artifact": {"executable": {"dependencies": []}},
    }
    monkeypatch.setattr(
        "firefly_weave.definitions.repository.DefinitionRepository.version", AsyncMock(return_value=row)
    )

    def adapter(name):
        if unavailable == "missing-adapter":
            raise CatalogError(422, "WV-CONNECTION", "Adapter absent")
        return SimpleNamespace()

    monkeypatch.setattr(catalog.connection.registry, "get", adapter)

    def secret(*args, **kwargs):
        if unavailable == "revoked-secret":
            raise CatalogError(422, "WV-CONNECTION", "Credential unavailable")

    catalog.connection.secrets.check = secret
    page = await listing(catalog, limit=1)
    assert page.items[0].enabled is (unavailable is None)
    if unavailable is None:
        bound = await catalog.connection.resolve_binding(
            catalog.actor, catalog.scope, "model", UUID(int=101), CONNECTOR_REFERENCE, context=AuditContext()
        )
        assert bound.revision.id == UUID(int=101)


@pytest.mark.parametrize(
    "query", ["unknown=1", "limit=1&limit=2", "connector=a&connector=b", "limit=0", "limit=101", "cursor=" + "x" * 2049]
)
async def test_controller_rejects_invalid_queries_before_service_access(query):
    controller = importlib.import_module("firefly_weave.api.bindable_connections").BindableConnectionController
    service = SimpleNamespace(list=AsyncMock())
    request = Request({"type": "http", "query_string": query.encode(), "headers": []})
    with pytest.raises(ValueError):
        await controller(service).list(request)
    service.list.assert_not_awaited()


async def test_controller_serializes_safe_page_with_no_store(catalog):
    controller = importlib.import_module("firefly_weave.api.bindable_connections").BindableConnectionController
    service, _ = modules()
    request = Request(
        {
            "type": "http",
            "query_string": b"limit=1",
            "headers": [],
            "path_params": {
                "tenant": str(catalog.scope.tenant_id),
                "project": str(catalog.scope.project_id),
                "environment": str(catalog.scope.environment_id),
            },
        }
    )
    request.state.principal = catalog.actor
    request.state.audit_context = AuditContext()
    response = await controller(service.BindableConnectionService(catalog.connection)).list(request)
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body)["items"][0]["revision_id"] == str(UUID(int=101))


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_actual_binding_denies_a_different_revision_grant(catalog, snapshot):
    value = getattr(catalog, snapshot)
    setattr(
        catalog,
        snapshot,
        value.model_copy(
            update={"grants": (Grant(role="deployer", scope=catalog.scope, resources=(str(UUID(int=205)),)),)}
        ),
    )
    with pytest.raises(AccessDenied):
        await catalog.connection.resolve_binding(
            catalog.actor, catalog.scope, "model", UUID(int=101), CONNECTOR_REFERENCE, context=AuditContext()
        )
    catalog.fetch.assert_not_awaited()


async def test_bind_only_authority_cannot_read_administrative_connection_payload(catalog):
    with pytest.raises(AccessDenied):
        await catalog.connection.read(catalog.actor, catalog.scope, UUID(int=101), context=AuditContext())
    catalog.fetch.assert_not_awaited()


@pytest.mark.parametrize("count,limit", [(0, 50), (1, 1), (100, 100), (101, 100)])
async def test_empty_final_and_full_pages_keep_exact_continuations(catalog, count, limit):
    catalog.records = [revision(i) for i in range(1, count + 1)]
    catalog.actor = principal(catalog.scope)
    catalog.current = catalog.actor
    page = await listing(catalog, limit=limit)
    assert len(page.items) == min(count, limit)
    assert bool(page.next_cursor) is (count > limit)
    if page.next_cursor:
        final = await listing(catalog, limit=limit, cursor=page.next_cursor)
        assert [item.revision_id for item in final.items] == [UUID(int=101)]
        assert final.next_cursor is None


async def test_native_application_resolves_the_connection_choice_route_and_service(catalog):
    from starlette.routing import Match

    from firefly_weave import private_origins
    from firefly_weave.api.bindable_connections import BindableConnectionController
    from firefly_weave.app import make_app
    from firefly_weave.connections.bindable import BindableConnectionService
    from firefly_weave.settings import Settings

    with private_origins.installed(private_origins.PrivateOrigins.empty()):
        app = make_app(Settings(database_url="postgresql+asyncpg://unused@127.0.0.1:1/unused"))
        try:
            context = app.state.pyfly.context
            assert context.container.get_registration(BindableConnectionService) is not None
            context.container.register_instance(ConnectionService, catalog.connection)
            controller = context.get_bean(BindableConnectionController)
            assert controller.service is context.get_bean(BindableConnectionService)
            scope = {
                "type": "http",
                "method": "GET",
                "root_path": "",
                "path": f"/api/v1/tenants/{uuid4()}/projects/{uuid4()}/environments/{uuid4()}/bindable-connections",
            }
            route = next(r for r in app.router.routes if r.matches(scope)[0] == Match.FULL)
            assert route.name == "bindable_connections.list"
        finally:
            await app.state.resources.close()
            for owner in app.state.telemetry:
                owner.close()
