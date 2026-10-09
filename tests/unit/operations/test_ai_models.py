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

"""Approved discovery metadata stays bounded, scoped and authorized across awaited work."""

import asyncio
import importlib
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
from firefly_weave.contracts.ai import AIConnectionTestRequest, AIModel, GatewayModelList
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.ai_connections import AIConnectionService
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.private_origins import PLATFORM, PrivateOrigin, PrivateOrigins


def modules():
    assert importlib.util.find_spec("firefly_weave.operations.ai_models"), "Approved model discovery is unavailable"
    return (
        importlib.import_module("firefly_weave.operations.ai_models"),
        importlib.import_module("firefly_weave.operations.ai_cache"),
        importlib.import_module("firefly_weave.contracts.ai"),
    )


def result(name="approved"):
    return modules()[2].AIModelsResult(
        approval="listed",
        discovery="ok",
        discovered_at=datetime.now(UTC),
        models=[AIModel(name=name, approved=True, available=True)],
    )


def test_cache_bounds_policy_scope_and_expiry():
    now = [10.0]
    cache = modules()[1].AIModelCache(clock=lambda: now[0], max_entries=2, max_bytes=524288, ttl=300.0)
    prefix = (uuid4(), uuid4(), uuid4(), uuid4())
    key = (*prefix, "a" * 64)
    value = result()
    cache.put(key, value)
    assert cache.get(key) == value
    assert cache.get((*prefix, "b" * 64)) is None
    for index in range(4):
        changed = list(key)
        changed[index] = uuid4()
        assert cache.get(tuple(changed)) is None
    second, newest = (uuid4(), *key[1:]), (uuid4(), *key[1:])
    cache.put(second, value)
    cache.get(key)
    cache.put(newest, value)
    assert cache.get(second) is None and cache.get(key) == value
    now[0] += 300
    assert cache.get(newest) is None and cache.get(key) is None
    assert cache._bytes == 0


def test_cache_owns_copies_and_exact_byte_budget():
    value = result()
    size = len(value.model_dump_json().encode())
    cache = modules()[1].AIModelCache(max_entries=128, max_bytes=size * 2)
    keys = [tuple(uuid4() for _ in range(4)) + ("a" * 64,) for _ in range(3)]
    for key in keys:
        cache.put(key, value)
    assert cache.get(keys[0]) is None and cache._bytes == size * 2
    value.models.clear()
    read = cache.get(keys[1])
    assert len(read.models) == 1
    read.models.clear()
    assert len(cache.get(keys[1]).models) == 1
    scope = Scope(tenant_id=keys[1][0], project_id=keys[1][1], environment_id=keys[1][2])
    cache.discard_revision(scope, keys[1][3])
    assert cache.get(keys[1]) is None and cache._bytes == size
    cache.put(keys[2], result())
    assert cache._bytes == size


def test_cache_rejects_oversize_records_before_retaining_them():
    cache = modules()[1].AIModelCache()
    value = result()
    value.models[0] = value.models[0].model_copy(update={"family": "x" * 262145})
    with pytest.raises(CatalogError) as refused:
        cache.put(tuple(uuid4() for _ in range(4)) + ("a" * 64,), value)
    assert (refused.value.status, refused.value.code) == (429, "WV-AI-LIMIT")
    assert cache._bytes == 0


@pytest.fixture
def model_service(tmp_path, monkeypatch):
    models, _, contracts = modules()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    actor = Principal(
        id=uuid4(),
        kind="human",
        grants=(
            Grant(role="viewer", scope=scope),
            Grant(role="tenant_admin", scope=scope),
        ),
    )
    saved = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="model-endpoint",
        connector_version_id=uuid4(),
        connector="weave-agentic-provider@1.0.0",
        connector_digest=AGENTIC_DESCRIPTOR.manifest.digest,
        adapter="weave-agentic-provider",
        config={"provider": "openai-chat", "endpoint": "https://models.example/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "model-handle"},
        allowed_destinations=("https://models.example",),
    )
    state = SimpleNamespace(
        scope=scope,
        actor=actor,
        current=actor,
        revision=saved,
        context=AuditContext(),
        records={(scope.tenant_id, scope.project_id, scope.environment_id, saved.id): saved},
        audits=[],
        opened=[],
        calls=[],
        in_transaction=False,
        before_audit=None,
    )

    async def scalar(statement, params):
        key = (params["tenant"], params["project"], params["environment"], params["id"])
        row = state.records.get(key)
        return None if row is None else row.model_dump(mode="json", by_alias=True)

    state.session = SimpleNamespace(scalar=AsyncMock(side_effect=scalar), execute=AsyncMock())

    @asynccontextmanager
    async def opened(selected_scope, mutation=True):
        state.opened.append((selected_scope, mutation))
        state.in_transaction = True
        try:
            yield SimpleNamespace(session=state.session, scope=selected_scope)
        finally:
            state.in_transaction = False

    definitions = object.__new__(DefinitionService)
    definitions.authorization = AuthorizationService()
    definitions.uow = SimpleNamespace(open=opened)
    state.connections = ConnectionService(definitions.uow, definitions, ConnectorRegistry(), SimpleNamespace())
    monkeypatch.setattr(state.connections, "_ready", AsyncMock())
    loader = AsyncMock(side_effect=lambda *args: state.current)
    monkeypatch.setattr(models, "load_principal", loader)
    monkeypatch.setattr("firefly_weave.access.repository.load_principal", loader)

    async def record(session, actor, action, target, **kwargs):
        assert state.in_transaction
        if state.before_audit:
            await state.before_audit()
        state.audits.append((action, target, kwargs))

    monkeypatch.setattr(models, "audit", record)
    monkeypatch.setattr("firefly_weave.operations.ai_connections.audit", record)
    state.policy_path = tmp_path / "policy.json"
    state.document = {
        "version": 2,
        "endpoints": [
            {
                "id": "models",
                "label": "Models",
                "url": "https://models.example/v1",
                "providers": ["openai-chat"],
                "models": ["approved", "missing", "refused"],
                "compat": "ollama",
                "credential": "required",
            }
        ],
    }

    def publish():
        state.policy_path.write_text(json.dumps(state.document))
        # Ensure the test fixture represents a policy replacement on every filesystem.
        state.policy._file = None

    state.policy = APIAIPolicy(state.policy_path)
    state.publish = publish
    publish()
    state.raw = GatewayModelList(discovery="ok", models=[AIModel(name="approved", approved=True, available=True)])
    state.gateway_hook = None

    async def gateway(config, credential, *, provider, timeout_seconds):
        assert not state.in_transaction
        state.calls.append(
            {"credential_present": credential is not None, "provider": provider, "timeout_seconds": timeout_seconds}
        )
        if state.gateway_hook:
            await state.gateway_hook()
        return state.raw

    state.gateway = SimpleNamespace(configured=True, models=AsyncMock(side_effect=gateway))
    state.tests = AIConnectionService(state.connections, state.gateway)
    state.credential = AsyncMock(return_value="credential-canary")
    monkeypatch.setattr(state.tests, "credential", state.credential)
    state.service = models.AIModelService(state.connections, state.tests, state.policy, state.gateway)
    state.query = contracts.AIModelsQuery
    return state


async def listing(state, *, refresh=False, actor=None, scope=None):
    return await state.service.models(
        actor or state.actor,
        scope or state.scope,
        state.query(connection=state.revision.id, refresh=refresh),
        context=state.context,
    )


async def test_viewer_reads_cached_models_without_bind_or_manage(model_service):
    s = model_service
    refreshed = await listing(s, refresh=True)
    viewer = s.actor.model_copy(update={"grants": (Grant(role="viewer", scope=s.scope),)})
    s.current = viewer
    assert await listing(s, actor=viewer) == refreshed
    assert len(s.calls) == 1 and s.credential.await_count == 1
    assert len(s.tests.limiter._calls[s.actor.id]) == 1
    assert s.audits[0][0] == "ai.models.refresh"
    assert s.audits[0][2]["details"] == {"discovery": "ok", "models": 3}
    assert "credential-canary" not in repr(s.service.cache._records) + repr(s.audits)
    with pytest.raises(AccessDenied):
        await listing(s, refresh=True, actor=viewer)
    assert len(s.calls) == 1 and len(s.tests.limiter._calls[s.actor.id]) == 1


@pytest.mark.parametrize("field", ["tenant_id", "project_id", "environment_id"])
async def test_foreign_revision_is_not_found(model_service, field):
    s = model_service
    await listing(s, refresh=True)
    other = s.scope.model_copy(update={field: uuid4()})
    s.actor = s.actor.model_copy(update={"grants": (Grant(role="viewer", scope=other),)})
    s.current = s.actor
    with pytest.raises(CatalogError) as missing:
        await listing(s, scope=other)
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")
    assert len(s.calls) == 1


@pytest.mark.parametrize("phase", ["before", "secret", "gateway"])
@pytest.mark.parametrize("revocation", ["catalog", "manage", "inactive", "token"])
async def test_revocation_before_and_after_gateway_hides_results(model_service, phase, revocation):
    s = model_service

    async def revoke(*args):
        if revocation == "inactive":
            s.current = s.current.model_copy(update={"active": False})
        elif revocation == "token":
            s.actor = s.actor.model_copy(update={"grants": ()})
        else:
            role = "tenant_admin" if revocation == "catalog" else "viewer"
            s.current = s.current.model_copy(update={"grants": (Grant(role=role, scope=s.scope),)})
        return "credential-canary"

    # Token snapshots are immutable; revoked current authority must intersect a stale authorized token.
    if revocation == "token" and phase != "before":
        s.current = s.current.model_copy(update={"grants": ()})
    if phase == "before":
        await revoke()
    elif phase == "secret":
        s.credential.side_effect = revoke
    else:
        s.gateway_hook = revoke
    with pytest.raises((AccessDenied, AuthenticationFailed)):
        await listing(s, refresh=True)
    assert not s.audits and not s.service.cache._records
    if phase != "gateway" or revocation == "token":
        assert not s.calls


@pytest.mark.parametrize("replacement", ["listed", "invalid", "absent", "provider", "endpoint"])
async def test_policy_replacement_cannot_reuse_or_widen_cache(model_service, replacement):
    s = model_service
    await listing(s, refresh=True)
    if replacement == "absent":
        s.policy.path = None
    elif replacement == "invalid":
        s.policy_path.write_text("invalid")
        s.policy._file = None
    else:
        field, value = {
            "listed": ("models", ["another"]),
            "provider": ("providers", ["anthropic"]),
            "endpoint": ("url", "https://other.example/v1"),
        }[replacement]
        s.document["endpoints"][0][field] = value
        s.publish()
    if replacement == "invalid":
        with pytest.raises(CatalogError) as failure:
            await listing(s)
        assert (failure.value.status, failure.value.code) == (503, "WV-AI-POLICY")
    else:
        answer = await listing(s)
        assert not answer.models
        assert answer.discovery == ("LLM_POLICY" if replacement in {"provider", "endpoint"} else "unknown")
        assert answer.approval == ("listed" if replacement == "listed" else "unknown")
    assert len(s.calls) == 1


@pytest.mark.parametrize("replacement", ["listed", "invalid", "absent"])
async def test_policy_changed_during_gateway_is_not_stored(model_service, replacement):
    s = model_service

    async def change():
        if replacement == "absent":
            s.policy.path = None
        elif replacement == "invalid":
            s.policy_path.write_text("invalid")
            s.policy._file = None
        else:
            s.document["endpoints"][0]["models"] = ["another"]
            s.publish()

    s.gateway_hook = change
    with pytest.raises(CatalogError) as error:
        await listing(s, refresh=True)
    assert error.value.code == ("WV-AI-POLICY" if replacement == "invalid" else "WV-AI-POLICY-CHANGED")
    assert not s.service.cache._records and not s.audits


async def test_gateway_and_api_policies_intersect(model_service):
    s = model_service
    s.raw = GatewayModelList(
        discovery="ok",
        models=[
            AIModel(name="approved", approved=True, available=True, tools="yes"),
            AIModel(name="disallowed", approved=True, available=True),
            AIModel(name="refused", approved=False, available=True),
        ],
    )
    answer = await listing(s, refresh=True)
    assert [(m.name, m.approved, m.available, m.tools) for m in answer.models] == [
        ("approved", True, True, "yes"),
        ("missing", True, False, "unknown"),
    ]
    assert answer == await listing(s)


@pytest.mark.parametrize("code", ["LLM_TIMEOUT", "LLM_AUTH", "LLM_UNREACHABLE"])
async def test_failed_refresh_replaces_stale_health(model_service, code):
    s = model_service
    await listing(s, refresh=True)
    s.raw = GatewayModelList(discovery=code, models=[AIModel(name="approved", approved=True, available=True)])
    answer = await listing(s, refresh=True)
    assert answer.discovery == code and answer.models == []
    assert answer == await listing(s)
    assert s.audits[-1][2]["details"] == {"discovery": code, "models": 0}


@pytest.mark.parametrize(
    "code,want,status",
    [
        ("WV-AI-GATEWAY-MISSING", "LLM_UNREACHABLE", 503),
        ("WV-AI-GATEWAY-UNAVAILABLE", "LLM_UNREACHABLE", 503),
        ("WV-AI-GATEWAY-TIMEOUT", "LLM_TIMEOUT", 504),
        ("WV-AI-SECRET", "LLM_AUTH", 503),
        ("WV-LUMI-CAPACITY", None, 429),
    ],
)
async def test_transport_errors_are_safe_metadata_except_capacity(model_service, code, want, status):
    s = model_service
    error = CatalogError(status, code, "private-details-canary")
    if code == "WV-AI-SECRET":
        s.credential.side_effect = error
    else:
        s.gateway.models.side_effect = error
    if want is None:
        with pytest.raises(CatalogError) as failure:
            await listing(s, refresh=True)
        assert failure.value.status == 429
        assert not s.service.cache._records
    else:
        answer = await listing(s, refresh=True)
        assert answer.discovery == want and not answer.models
        assert "private-details-canary" not in answer.model_dump_json()


async def test_endpoint_projection_has_no_network_or_file_metadata(model_service, tmp_path):
    s = model_service
    s.document["endpoints"][0]["caBundle"] = str(tmp_path / "secret-ca.pem")
    s.publish()
    s.policy.origins = PrivateOrigins(platform=PLATFORM).with_entries(
        [
            PrivateOrigin(
                origin="https://models.example:443", purpose="model", networks=("10.246.27.0/24",), credentials="none"
            )
        ]
    )
    answer = await s.service.endpoints(s.actor, s.scope, context=s.context)
    assert answer.model_dump(mode="json") == {
        "policy": "loaded",
        "endpoints": [
            {
                "id": "models",
                "label": "Models",
                "url": "https://models.example/v1",
                "providers": ["openai-chat"],
                "compat": "ollama",
                "credential": "required",
                "models": "listed",
                "context_tokens": None,
                "development_only": True,
            }
        ],
    }
    assert not s.calls and not s.credential.await_count and not s.tests.limiter._calls
    s.policy.path = None
    assert (await s.service.endpoints(s.actor, s.scope, context=s.context)).model_dump() == {
        "policy": "absent",
        "endpoints": [],
    }


async def test_no_policy_refresh_authorizes_and_takes_one_slot_without_io(model_service):
    s = model_service
    s.policy.path = None
    assert (await listing(s, refresh=True)).discovery == "unknown"
    assert not s.calls and not s.credential.await_count
    assert len(s.tests.limiter._calls[s.actor.id]) == 1


async def test_auto_refresh_uses_remaining_budget(model_service):
    s = model_service
    await s.service.refresh_after(
        s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() - 1, context=s.context
    )
    assert not s.calls and not s.tests.limiter._calls
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    s.gateway_hook = slow
    await s.service.refresh_after(
        s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() + 0.03, context=s.context
    )
    assert cancelled.is_set() and s.calls[0]["timeout_seconds"] <= 0.03
    assert not s.tests.limiter._calls


async def test_auto_refresh_propagates_cancellation_and_programmer_errors(model_service):
    s = model_service
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    s.gateway_hook = wait
    task = asyncio.create_task(
        s.service.refresh_after(
            s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() + 20, context=s.context
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and not s.service.cache._records
    s.gateway.models.side_effect = RuntimeError("programmer error")
    with pytest.raises(RuntimeError):
        await s.service.refresh_after(
            s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() + 20, context=s.context
        )


async def test_audit_must_succeed_before_cache_storage(model_service):
    s = model_service

    async def refuse():
        raise AccessDenied()

    s.before_audit = refuse
    with pytest.raises(AccessDenied):
        await listing(s, refresh=True)
    assert not s.service.cache._records


@pytest.mark.parametrize("change", ["connector", "digest"])
async def test_only_exact_builtin_ai_revisions_are_discovered(model_service, change):
    s = model_service
    value = "other@1.0.0" if change == "connector" else "sha256:" + "0" * 64
    field = "connector" if change == "connector" else "connector_digest"
    s.records[next(iter(s.records))] = s.revision.model_copy(update={field: value})
    with pytest.raises(CatalogError) as refused:
        await listing(s)
    assert (refused.value.status, refused.value.code) == (422, "WV-AI-CONNECTION")
    await s.service.refresh_after(
        s.actor, s.scope, s.revision.id, admitted=False, deadline=time.monotonic() + 20, context=s.context
    )
    assert not s.calls and not s.tests.limiter._calls


async def test_refreshes_and_both_test_routes_share_six_slots(model_service, monkeypatch):
    from firefly_weave.contracts.agentic import AgenticConnectionAdapter
    from firefly_weave.contracts.connectors import ResolvedSecret
    from firefly_weave.operations.ai_connections import GatewayConnectionTester

    s = model_service
    s.gateway.test = AsyncMock(return_value={"ok": True, "code": "ok", "model": "approved", "tool_calling": "unknown"})
    s.connections.registry.register_descriptor(
        AGENTIC_DESCRIPTOR, AgenticConnectionAdapter(GatewayConnectionTester(s.gateway))
    )
    s.connections.secrets = SimpleNamespace(resolve=lambda *a: ResolvedSecret(value="credential-canary"))
    s.connections.test_admission = s.tests.admit

    async def refresh(actor, scope, identifier, deadline, context):
        await s.service.refresh_after(actor, scope, identifier, admitted=True, deadline=deadline, context=context)

    s.connections.refresh = s.tests.refresh = refresh

    async def dedicated():
        return await s.tests.test(
            s.actor, s.scope, s.revision.id, AIConnectionTestRequest(model="approved"), context=s.context
        )

    async def generic():
        return await s.connections.test_connection(s.actor, s.scope, s.revision.id, context=s.context)

    for _ in range(2):
        assert (await listing(s, refresh=True)).discovery == "ok"
        assert (await dedicated()).ok is True
        assert (await generic()).ok is True
    assert len(s.calls) == 6
    assert len(s.tests.limiter._calls[s.actor.id]) == 6
    for operation in (lambda: listing(s, refresh=True), dedicated, generic):
        with pytest.raises(CatalogError) as failure:
            await operation()
        assert (failure.value.status, failure.value.code) == (429, "WV-AI-RATE-LIMITED")
    assert len(s.calls) == 6 and s.gateway.test.await_count == 4


@pytest.mark.parametrize("kind", ["ok", "gateway-failure", "rate-limit", "other-connector"])
async def test_saved_revision_refreshes_after_commit(model_service, kind):
    from starlette.applications import Starlette
    from starlette.routing import Route

    from firefly_weave.api.connections import PREFIX, ConnectionController
    from firefly_weave.contracts.connectors import ConnectionRequest

    s = model_service
    events, replies = [], []
    saved = s.revision
    if kind == "other-connector":
        saved = saved.model_copy(update={"connector": "other@1.0.0", "adapter": "other"})
    if kind == "gateway-failure":
        s.gateway.models.side_effect = CatalogError(503, "WV-AI-GATEWAY-UNAVAILABLE", "Unavailable")
    if kind == "rate-limit":
        for _ in range(6):
            s.tests.limiter.take(s.actor.id)
    request_body = ConnectionRequest.model_validate_json(
        saved.model_dump_json(by_alias=True, exclude={"id", "revision", "connector", "connector_digest", "adapter"})
    )

    async def save(actor, scope, request, **kwargs):
        assert actor == s.actor and scope == s.scope and request == request_body
        assert not s.calls
        events.append("committed")
        return saved

    original = s.service.refresh_after

    async def refresh(*args, **kwargs):
        assert events == ["committed"] and kwargs["admitted"] is False
        assert 0 < kwargs["deadline"] - time.monotonic() <= 20
        events.append("refresh")
        await original(*args, **kwargs)

    controller = ConnectionController(SimpleNamespace(create_revision=save), SimpleNamespace(refresh_after=refresh))
    app = Starlette(routes=[Route("/api/v1" + PREFIX, controller.create, methods=["POST"])])
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": request_body.model_dump_json(by_alias=True).encode()}
        await asyncio.Event().wait()

    async def send(reply):
        replies.append(reply)

    http_scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": (
            f"/api/v1/tenants/{s.scope.tenant_id}/projects/{s.scope.project_id}"
            f"/environments/{s.scope.environment_id}/connections"
        ),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "state": {"principal": s.actor, "audit_context": s.context},
    }
    await app(http_scope, receive, send)
    assert replies[0]["status"] == 201
    assert json.loads(replies[1]["body"])["id"] == str(saved.id)
    assert events == (["committed"] if kind == "other-connector" else ["committed", "refresh"])
    if kind == "other-connector":
        assert not s.calls and not s.tests.limiter._calls
    else:
        assert len(s.tests.limiter._calls[s.actor.id]) == (6 if kind == "rate-limit" else 1)


@pytest.mark.parametrize("phase", ["secret", "gateway"])
async def test_revocation_during_suspended_io_never_reaches_cache(model_service, phase):
    s = model_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        entered.set()
        await release.wait()
        return "credential-canary"

    if phase == "secret":
        s.credential.side_effect = pause
    else:
        s.gateway_hook = pause
    task = asyncio.create_task(listing(s, refresh=True))
    await entered.wait()
    s.current = s.current.model_copy(update={"grants": ()})
    release.set()
    with pytest.raises(AccessDenied):
        await task
    assert not s.audits and not s.service.cache._records
    assert len(s.calls) == (0 if phase == "secret" else 1)


async def test_real_revision_save_never_refreshes_inside_its_transaction(model_service, monkeypatch):
    from firefly_weave.connections.repository import ConnectionRepository
    from firefly_weave.contracts.agentic import AgenticConnectionAdapter
    from firefly_weave.contracts.connectors import ConnectionRequest

    s = model_service
    s.connections.registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    s.connections.secrets = SimpleNamespace(check=lambda *args, **kwargs: None)
    contract = {
        "name": "weave-agentic-provider",
        "version": "1.0.0",
        "definition_digest": AGENTIC_DESCRIPTOR.manifest.digest,
        "document": AGENTIC_DESCRIPTOR.manifest.value,
        "artifact": {"executable": {"dependencies": []}},
    }
    monkeypatch.setattr(s.connections.definitions, "connector_contract", AsyncMock(return_value=contract))
    monkeypatch.setattr(ConnectionRepository, "next_revision", AsyncMock(return_value=2))
    s.connections.refresh = AsyncMock(side_effect=AssertionError("A save service must not refresh before commit"))
    request = ConnectionRequest.model_validate_json(
        s.revision.model_dump_json(
            by_alias=True, exclude={"id", "revision", "connector", "connector_digest", "adapter"}
        )
    )
    result = await s.connections.create_revision(s.actor, s.scope, request, context=s.context)
    assert result.revision == 2 and not s.in_transaction
    s.connections.refresh.assert_not_awaited()
    assert not s.calls and not s.tests.limiter._calls
    assert any("INSERT INTO connection_revisions" in str(call.args[0]) for call in s.session.execute.await_args_list)


async def test_saved_revision_http_disconnect_cancels_owned_discovery(model_service, tmp_path):
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from firefly_weave.api.connections import PREFIX, ConnectionController
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings

    s = model_service
    committed, entered, cancelled, closed = (asyncio.Event() for _ in range(4))
    owned = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            assert committed.is_set()
            owned.append(asyncio.current_task())
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield b""

        async def aclose(self):
            closed.set()

    async def upstream(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, stream=Stream())

    token = tmp_path / "token"
    token.write_text("service-test-token")
    gateway = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://gateway.example/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(upstream),
    )
    s.service.gateway = gateway

    async def save(*args, **kwargs):
        committed.set()
        return s.revision

    controller = ConnectionController(SimpleNamespace(create_revision=save), s.service)
    app = Starlette(routes=[Route("/api/v1" + PREFIX, controller.create, methods=["POST"])])
    request = ConnectionRequest.model_validate_json(
        s.revision.model_dump_json(
            by_alias=True, exclude={"id", "revision", "connector", "connector_digest", "adapter"}
        )
    )
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": request.model_dump_json(by_alias=True).encode()}
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError("A disconnected save must not send a success response")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": (
            f"/api/v1/tenants/{s.scope.tenant_id}/projects/{s.scope.project_id}"
            f"/environments/{s.scope.environment_id}/connections"
        ),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "state": {"principal": s.actor, "audit_context": s.context},
    }
    before = asyncio.all_tasks()
    async with asyncio.timeout(2) as detector:
        with pytest.raises(asyncio.CancelledError):
            await app(scope, receive, send)
    assert not detector.expired()
    assert committed.is_set() and entered.is_set() and cancelled.is_set() and closed.is_set()
    assert gateway.active == 0 and all(task.done() for task in owned)
    assert not (asyncio.all_tasks() - before) and not s.service.cache._records


async def test_authority_revoked_during_audit_is_not_cached(model_service):
    s = model_service

    async def revoke():
        s.current = s.current.model_copy(update={"grants": ()})

    s.before_audit = revoke
    with pytest.raises(AccessDenied):
        await listing(s, refresh=True)
    assert not s.service.cache._records


async def test_served_policy_filters_gateway_refusals_and_keeps_unknown_cache_misses(model_service):
    s = model_service
    s.policy.origins = PrivateOrigins(platform=PLATFORM).with_entries(
        [
            PrivateOrigin(
                origin="https://models.example:443", purpose="model", networks=("10.246.27.0/24",), credentials="none"
            )
        ]
    )
    s.document["endpoints"][0]["models"] = "served"
    s.publish()
    answer = await listing(s)
    assert answer.approval == "served" and answer.discovery == "unknown" and not answer.models
    s.raw = GatewayModelList(
        discovery="ok",
        models=[
            AIModel(name="served", approved=True, available=True),
            AIModel(name="denied", approved=False, available=True),
        ],
    )
    answer = await listing(s, refresh=True)
    assert answer.approval == "served" and [item.name for item in answer.models] == ["served"]


def database_refresh_failure(kind):
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, ProgrammingError, SQLAlchemyError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    if kind == "pool-timeout":
        return PoolTimeout("Connection pool unavailable")
    if kind == "programmer":
        return RuntimeError("Unrelated application fault")
    if kind == "sqlalchemy":
        return SQLAlchemyError("Unclassified database error")
    if kind == "cancelled":
        return asyncio.CancelledError()
    error_type = {
        "operational": OperationalError,
        "invalidated": DBAPIError,
        "dbapi": DBAPIError,
        "programming": ProgrammingError,
        "interface": InterfaceError,
    }[kind]
    return error_type(
        None, None, ConnectionError("Connection unavailable"), connection_invalidated=kind == "invalidated"
    )


@pytest.fixture
def refresh_http(model_service, tmp_path, monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from firefly_weave.api.ai import AIController
    from firefly_weave.api.connections import ConnectionController
    from firefly_weave.contracts.agentic import AgenticConnectionAdapter
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.operations.ai_connections import GatewayConnectionTester
    from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings

    s = model_service
    s.http_events, s.streams_closed, s.clients, s.bound_credentials = [], [], [], []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, path, payload):
            self.path, self.payload = path, payload

        async def __aiter__(self):
            yield json.dumps(self.payload).encode()

        async def aclose(self):
            s.streams_closed.append(self.path)

    async def upstream(request):
        s.http_events.append(request.url.path)
        payload = (
            s.raw.model_dump(mode="json")
            if request.url.path == "/v1/models"
            else {
                "ok": True,
                "code": "ok",
                "model": "approved",
                "tool_calling": "unknown",
            }
        )
        return httpx.Response(200, stream=Stream(request.url.path, payload))

    token = tmp_path / "refresh-gateway-token"
    token.write_text("gateway-test-token")
    gateway = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://gateway.example/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(upstream),
    )
    original_client = gateway._client

    def client(timeout):
        owned = original_client(timeout)
        s.clients.append(owned)
        return owned

    monkeypatch.setattr(gateway, "_client", client)
    s.service.gateway = s.tests.gateway = gateway
    s.connections.secrets = SimpleNamespace(resolve=lambda *args: ResolvedSecret(value="credential-canary"))
    adapter = AgenticConnectionAdapter(GatewayConnectionTester(gateway))
    s.connections.registry.register_descriptor(AGENTIC_DESCRIPTOR, adapter)
    original_test = adapter.test_connection

    async def capture(bound):
        s.bound_credentials.append(bound.credentials)
        return await original_test(bound)

    monkeypatch.setattr(adapter, "test_connection", capture)
    s.connections.test_admission = s.tests.admit

    async def refresh(actor, scope, identifier, deadline, context):
        s.http_events.append("refresh")
        await s.service.refresh_after(actor, scope, identifier, admitted=True, deadline=deadline, context=context)

    s.connections.refresh = s.tests.refresh = refresh

    async def save(*args, **kwargs):
        s.http_events.append("committed")
        return s.revision

    monkeypatch.setattr(s.connections, "create_revision", save)
    controller = ConnectionController(s.connections, s.service)
    ai = AIController(s.tests, s.service, SimpleNamespace())

    async def request(route):
        operation, handler, body = {
            "save": (
                "connections.create",
                controller.create,
                ConnectionRequest.model_validate_json(
                    s.revision.model_dump_json(
                        by_alias=True, exclude={"id", "revision", "connector", "connector_digest", "adapter"}
                    )
                ).model_dump(mode="json", by_alias=True),
            ),
            "dedicated": ("ai_connections.test", ai.test_connection, {"model": "approved"}),
            "generic": ("connections.test", controller.test, {}),
        }[route]
        template = OPERATIONS[operation].canonical_path
        app = Starlette(routes=[Route(template, handler, methods=["POST"])])

        async def seeded(scope, receive, send):
            scope["state"] = {"principal": s.actor, "audit_context": s.context}
            await app(scope, receive, send)

        path = template.format(
            tenant=s.scope.tenant_id,
            project=s.scope.project_id,
            environment=s.scope.environment_id,
            identifier=s.revision.id,
        )
        transport = httpx.ASGITransport(seeded, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            return await client.post(path, json=body)

    s.http_request, s.real_gateway = request, gateway
    return s


@pytest.mark.parametrize("route", ["save", "dedicated", "generic"])
@pytest.mark.parametrize("phase", ["read", "audit"])
@pytest.mark.parametrize("kind", ["operational", "invalidated", "pool-timeout"])
async def test_optional_refresh_database_failure_preserves_primary_http_result(
    refresh_http, monkeypatch, route, phase, kind
):
    from firefly_weave.connections.secrets import SecretUnavailable

    s = refresh_http
    key = (*next(iter(s.records)), s.policy.current().sha256)
    s.service.cache.put(key, result())
    failure = database_refresh_failure(kind)

    async def fail(*args, **kwargs):
        s.http_events.append("database-failed")
        raise failure

    monkeypatch.setattr(s.service, "_revision" if phase == "read" else "_record_refresh", fail)
    before = asyncio.all_tasks()
    response = await s.http_request(route)
    assert not (asyncio.all_tasks() - before)
    assert s.http_events[-1] == "database-failed"
    assert s.http_events[0] == ("committed" if route == "save" else "/v1/test")
    assert s.real_gateway.active == 0 and all(client.is_closed for client in s.clients)
    assert s.streams_closed == [path for path in s.http_events if path.startswith("/v1/")]
    if route == "generic":
        with pytest.raises(SecretUnavailable):
            s.bound_credentials[0]("apiKey")
    if route == "dedicated":
        assert any(action == "ai.connection.test" for action, _, _ in s.audits)
    assert response.status_code == (201 if route == "save" else 200)
    assert not s.service.cache._records
    if route == "save":
        assert response.json()["id"] == str(s.revision.id)
    else:
        assert response.json()["ok"] is True
        payloads = [
            json.loads(call.args[1]["payload"])
            for call in s.session.execute.await_args_list
            if "connection_test_results" in str(call.args[0])
        ]
        assert len(payloads) == 1 and payloads[0]["ok"] is True


@pytest.mark.parametrize("phase", ["read", "audit"])
@pytest.mark.parametrize("kind", ["operational", "invalidated", "pool-timeout"])
async def test_explicit_refresh_keeps_database_failures_visible(model_service, monkeypatch, phase, kind):
    s = model_service
    failure = database_refresh_failure(kind)
    monkeypatch.setattr(
        s.service, "_revision" if phase == "read" else "_record_refresh", AsyncMock(side_effect=failure)
    )
    with pytest.raises(type(failure)) as caught:
        await listing(s, refresh=True)
    assert caught.value is failure
    assert not s.service.cache._records


@pytest.mark.parametrize("phase", ["read", "audit"])
@pytest.mark.parametrize("kind", ["dbapi", "programming", "interface", "programmer", "sqlalchemy", "cancelled"])
async def test_optional_refresh_does_not_hide_programming_errors_or_cancellation(
    model_service, monkeypatch, phase, kind
):
    s = model_service
    failure = database_refresh_failure(kind)
    monkeypatch.setattr(
        s.service, "_revision" if phase == "read" else "_record_refresh", AsyncMock(side_effect=failure)
    )
    with pytest.raises(type(failure)) as caught:
        await s.service.refresh_after(
            s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() + 20, context=s.context
        )
    assert caught.value is failure


@pytest.mark.parametrize("revocation", ["inactive", "catalog", "manage"])
async def test_optional_refresh_revocation_discards_cached_metadata_without_io(model_service, revocation):
    s = model_service
    await listing(s, refresh=True)
    s.calls.clear()
    if revocation == "inactive":
        s.current = s.current.model_copy(update={"active": False})
    else:
        role = "tenant_admin" if revocation == "catalog" else "viewer"
        s.current = s.current.model_copy(update={"grants": (Grant(role=role, scope=s.scope),)})
    await s.service.refresh_after(
        s.actor, s.scope, s.revision.id, admitted=True, deadline=time.monotonic() + 20, context=s.context
    )
    assert not s.service.cache._records and not s.calls
