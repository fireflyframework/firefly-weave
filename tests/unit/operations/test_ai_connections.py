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

"""AI connection tests run in the AI gateway, are rate limited, recorded and audited, and never carry model text."""

import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, AgenticConnectionAdapter
from firefly_weave.contracts.ai import AIConnectionTestRequest
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision, ResolvedSecret
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import ai_connections
from firefly_weave.operations.ai_connections import AIConnectionService, AIRateLimit, GatewayConnectionTester

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
ACTOR = Principal(id=uuid4(), kind="human")
CONTEXT = AuditContext(request_id=uuid4())
POLICY = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)
ANSWER = {"ok": True, "code": "ok", "latency_ms": 812, "model": "qwen3:4b", "tool_calling": "supported"}
REQUEST = AIConnectionTestRequest(model="qwen3:4b")


def revision(**changes):
    value = {
        "id": uuid4(),
        "revision": 1,
        "name": "ollama-local",
        "connector_version_id": uuid4(),
        "connector": "weave-agentic-provider@1.0.0",
        "connector_digest": AGENTIC_DESCRIPTOR.manifest.digest,
        "adapter": "weave-agentic-provider",
        "config": {"provider": "openai-chat", "endpoint": "http://ollama:11434/v1", "secretSlot": "apiKey"},
        "secretRef": {"apiKey": "no-credential"},
        "allowed_destinations": ("http://ollama:11434",),
    }
    value.update(changes)
    return ConnectionRevision.model_validate(value)


class Session:
    def __init__(self):
        self.statements = []

    async def execute(self, statement, values=None):
        self.statements.append((str(statement), values or {}))
        return SimpleNamespace()


class Connections:
    def __init__(self, saved, secrets=None):
        self.saved, self.secrets, self.required, self.refused = saved, secrets or ScopedSecrets(), [], False
        self.session = Session()
        session = self.session

        class Open:
            @asynccontextmanager
            async def open(self, scope, mutation=True):
                yield SimpleNamespace(session=session, scope=scope)

        self.uow = Open()

    def require(self, actor, scope, capability, context):
        self.required.append(capability)
        if self.refused:
            raise AccessDenied()

    async def ready_revision(self, actor, scope, revision_id, capability, *, context):
        assert revision_id == self.saved.id and capability == "connection.manage"
        return self.saved

    async def revalidate(self, actor, scope, revision, capability, context):
        self.require(actor, scope, capability, context)


class Gateway:
    configured = True

    def __init__(self, answer=None):
        self.answer, self.calls, self.timeouts = answer or ANSWER, [], []

    async def test(self, config, credential, *, provider, model, probe_tools, timeout_seconds=120):
        self.calls.append({"credential": credential, "provider": provider, "model": model, "probe_tools": probe_tools})
        self.timeouts.append(timeout_seconds)
        return self.answer


class Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Store:
    """The statements a generic connection test issues, over one saved revision and an active person."""

    def __init__(self, saved):
        self.saved = saved
        self.active = True

    async def execute(self, statement, values=None):
        if str(statement).startswith("SELECT id,kind,active FROM principals"):
            return Rows([{"id": values["id"], "kind": "human", "active": self.active}])
        return Rows()

    async def scalar(self, statement, values=None):
        sql = str(statement)
        if "platform_administrators" in sql:
            return None
        assert sql.startswith("SELECT payload FROM connection_revisions"), sql
        return self.saved.model_dump(mode="json", by_alias=True)


class Definitions:
    def __init__(self):
        self.refused = False
        self.retired = False

    def require(self, actor, scope, capability, context):
        if self.refused or not actor.active:
            raise AccessDenied()

    async def connector_contract(self, actor, scope, identifier, *, capability, context, tx, resource=None):
        if self.retired:
            raise CatalogError(409, "WV-CONNECTION", "The connection's connector was retired")
        manifest = AGENTIC_DESCRIPTOR.manifest.value
        return {
            "kind": "Connector",
            "name": "weave-agentic-provider",
            "version": "1.0.0",
            "retired": self.retired,
            "definition_digest": FrozenDocument.from_value(manifest).digest,
            "document": manifest,
            "artifact": {"executable": {"dependencies": []}},
        }


def generic_connections(saved, gateway):
    """A real connection service whose AI adapter tests through the given gateway, as the app wires it."""
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter(GatewayConnectionTester(gateway)))
    store, definitions = Store(saved), Definitions()
    connections = ConnectionService(SimpleNamespace(), definitions, registry, ScopedSecrets())

    class Open:
        session = store

        @asynccontextmanager
        async def open(self, scope, mutation=True):
            yield SimpleNamespace(session=store, scope=scope)

    connections.uow = Open()
    return connections, definitions


@pytest.mark.parametrize("revocation", ["principal", "grant", "connection"])
async def test_dedicated_test_rechecks_authority_and_readiness_after_secret_resolution(revocation, audits):
    saved = revision(
        config={"provider": "openai-chat", "endpoint": "https://model.test/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "model-key"},
        allowed_destinations=("https://model.test",),
    )
    gateway = Gateway()
    connections, definitions = generic_connections(saved, gateway)

    class Secrets:
        def check(self, *args, **kwargs):
            pass

        def resolve(self, *args):
            if revocation == "principal":
                connections.uow.session.active = False
            elif revocation == "grant":
                definitions.refused = True
            else:
                definitions.retired = True
            return ResolvedSecret(value="must-not-escape")

    connections.secrets = Secrets()
    error = {"principal": AuthenticationFailed, "grant": AccessDenied, "connection": CatalogError}[revocation]
    with pytest.raises(error):
        await AIConnectionService(connections, gateway).test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
    assert not gateway.calls and not audits


@pytest.fixture
def audits(monkeypatch):
    found = []

    async def record(session, actor, action, target, **kwargs):
        found.append((action, target, kwargs["capability"], kwargs["details"]))

    monkeypatch.setattr(ai_connections, "audit", record)
    return found


async def test_a_keyless_test_sends_no_credential_and_is_recorded_and_audited(audits):
    saved = revision()
    connections, gateway = Connections(saved), Gateway()
    service = AIConnectionService(connections, gateway)
    with po.installed(POLICY):
        result = await service.test(ACTOR, SCOPE, saved.id, AIConnectionTestRequest(model="qwen3:4b"), context=CONTEXT)
    assert result.model_dump(mode="json") == ANSWER
    assert connections.required == ["connection.manage", "connection.manage"]
    assert gateway.calls == [{"credential": None, "provider": "openai-chat", "model": "qwen3:4b", "probe_tools": True}]
    stored = [values for sql, values in connections.session.statements if "connection_test_results" in sql]
    payload = json.loads(stored[0]["payload"])
    assert payload["kind"] == "ai" and payload["ok"] is True and "tested_at" in payload
    assert audits == [
        (
            "ai.connection.test",
            str(saved.id),
            "connection.manage",
            {"ok": True, "code": "ok", "model": "qwen3:4b", "tool_calling": "supported", "latency_ms": 812},
        )
    ]


async def test_a_credentialed_connection_resolves_its_handle(monkeypatch, audits):
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_OPENAI", "provider-key")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()},
        (SecretGrant(SCOPE, "openai-api-key", "env", "WEAVE_CONNECTION_SECRET_OPENAI"),),
    )
    saved = revision(
        config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "openai-api-key"},
        allowed_destinations=("https://api.openai.com",),
    )
    gateway = Gateway({**ANSWER, "model": "gpt-5-mini"})
    service = AIConnectionService(Connections(saved, secrets), gateway)
    await service.test(ACTOR, SCOPE, saved.id, AIConnectionTestRequest(model="gpt-5-mini"), context=CONTEXT)
    assert gateway.calls[0]["credential"] == "provider-key"


async def test_ai_connection_tests_allow_six_per_minute_per_person():
    now = [0.0]
    limit = AIRateLimit(clock=lambda: now[0])
    for _ in range(6):
        limit.take(ACTOR.id)
    with pytest.raises(CatalogError) as limited:
        limit.take(ACTOR.id)
    assert (limited.value.status, limited.value.code) == (429, "WV-AI-RATE-LIMITED")
    limit.take(uuid4())
    now[0] = 60.0
    limit.take(ACTOR.id)


async def test_only_ai_connections_can_be_tested(audits):
    saved = revision(connector="weave-http@2.0.0", adapter="weave-http-v2")
    with pytest.raises(CatalogError) as refused:
        await AIConnectionService(Connections(saved), Gateway()).test(
            ACTOR, SCOPE, saved.id, AIConnectionTestRequest(model="qwen3:4b"), context=CONTEXT
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-AI-CONNECTION")


async def test_without_a_gateway_the_test_names_it(audits):
    gateway = Gateway()
    gateway.configured = False
    saved = revision()
    with po.installed(POLICY), pytest.raises(CatalogError) as missing:
        await AIConnectionService(Connections(saved), gateway).test(
            ACTOR, SCOPE, saved.id, AIConnectionTestRequest(model="qwen3:4b"), context=CONTEXT
        )
    assert (missing.value.status, missing.value.code) == (503, "WV-AI-GATEWAY-MISSING")


async def test_a_gateway_answer_with_model_text_is_refused(audits):
    saved = revision()
    gateway = Gateway({**ANSWER, "text": "ready-canary"})
    with po.installed(POLICY), pytest.raises(CatalogError) as refused:
        await AIConnectionService(Connections(saved), gateway).test(
            ACTOR, SCOPE, saved.id, AIConnectionTestRequest(model="qwen3:4b"), context=CONTEXT
        )
    assert refused.value.code == "WV-AI-GATEWAY-UNAVAILABLE" and audits == []


async def test_the_generic_connection_test_delegates_without_a_tool_probe():
    gateway = Gateway()
    tester = GatewayConnectionTester(gateway)

    def no_secret(name):
        raise AssertionError("A keyless connection is never resolved")

    with po.installed(POLICY):
        result = await tester(BoundConnection("test", revision(), no_secret))
    assert result.ok is True and result.code == "ok"
    assert gateway.calls == [{"credential": None, "provider": "openai-chat", "model": None, "probe_tools": False}]
    # The gateway gives up before the generic test's own 30-second budget ends, so no probe outlives it.
    assert gateway.timeouts == [20]
    failing = Gateway({**ANSWER, "ok": False, "code": "LLM_UNREACHABLE"})
    with po.installed(POLICY):
        assert (await GatewayConnectionTester(failing)(BoundConnection("test", revision(), no_secret))).ok is False


async def test_the_service_takes_a_slot_per_test_and_none_for_a_refused_caller(audits):
    saved, gateway = revision(), Gateway()
    connections = Connections(saved)
    service = AIConnectionService(connections, gateway)
    with po.installed(POLICY):
        connections.refused = True
        with pytest.raises(AccessDenied):
            await service.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
        connections.refused = False
        for _ in range(6):
            await service.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
        with pytest.raises(CatalogError) as limited:
            await service.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
    assert (limited.value.status, limited.value.code) == (429, "WV-AI-RATE-LIMITED")
    assert len(gateway.calls) == 6


async def test_generic_and_dedicated_tests_of_ai_connections_share_one_limit_per_person(audits):
    saved, gateway = revision(), Gateway()
    connections, definitions = generic_connections(saved, gateway)
    service = AIConnectionService(connections, gateway)
    connections.test_admission = service.admit

    def generic():
        return connections.test_connection(ACTOR, SCOPE, saved.id, context=CONTEXT)

    def dedicated():
        return service.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)

    with po.installed(POLICY):
        definitions.refused = True
        with pytest.raises(AccessDenied):
            await generic()
        definitions.refused = False
        for _ in range(3):
            assert (await generic()).ok is True
            assert (await dedicated()).ok is True
        for seventh in (generic, dedicated):
            with pytest.raises(CatalogError) as limited:
                await seventh()
            assert (limited.value.status, limited.value.code) == (429, "WV-AI-RATE-LIMITED")
    assert [call["probe_tools"] for call in gateway.calls] == [False, True] * 3


async def test_a_generic_test_of_another_kind_of_connection_takes_no_slot(audits):
    saved = revision()
    service = AIConnectionService(Connections(saved), Gateway())
    http = revision(connector="weave-http@2.0.0", adapter="weave-http-v2")
    for _ in range(10):
        service.admit(ACTOR, http)
    with po.installed(POLICY):
        for _ in range(6):
            await service.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)


async def test_a_gateway_answer_for_another_model_is_refused(audits):
    saved = revision()
    connections = Connections(saved)
    gateway = Gateway({**ANSWER, "model": "llama3.2:1b"})
    with po.installed(POLICY), pytest.raises(CatalogError) as refused:
        await AIConnectionService(connections, gateway).test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
    assert (refused.value.status, refused.value.code) == (503, "WV-AI-GATEWAY-UNAVAILABLE")
    assert audits == [] and connections.session.statements == []


async def test_a_generic_answer_without_a_valid_model_name_fails():
    def no_secret(name):
        raise AssertionError("A keyless connection is never resolved")

    for name in ("two words", "qwen3:4b\n", "-leading-dash"):
        tester = GatewayConnectionTester(Gateway({**ANSWER, "model": name}))
        with po.installed(POLICY):
            assert (await tester(BoundConnection("test", revision(), no_secret))).ok is False


async def test_a_secret_handle_that_does_not_answer_in_time_is_refused(monkeypatch, audits):
    release = threading.Event()

    class Slow:
        def resolve(self, handle):
            release.wait(2)
            return ResolvedSecret(value="late")

    monkeypatch.setattr(ai_connections, "SECRET_SECONDS", 0.05)
    secrets = ScopedSecrets(
        {"slow": Slow()}, (SecretGrant(SCOPE, "openai-api-key", "slow", "WEAVE_CONNECTION_SECRET_OPENAI"),)
    )
    saved = revision(
        config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "openai-api-key"},
        allowed_destinations=("https://api.openai.com",),
    )
    gateway = Gateway()
    try:
        with pytest.raises(CatalogError) as refused:
            await AIConnectionService(Connections(saved, secrets), gateway).test(
                ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT
            )
    finally:
        release.set()
    assert (refused.value.status, refused.value.code) == (503, "WV-AI-SECRET")
    assert gateway.calls == [] and audits == []


@pytest.fixture
def generic_http_case(tmp_path, monkeypatch):
    import asyncio
    import time

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from firefly_weave.api.connections import PREFIX, ConnectionController
    from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings

    def build(phase):
        entered, cancelled, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        credentials, owned_tasks, sent = [], [], []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                owned_tasks.append(asyncio.current_task())
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
            blocked = "/v1/test" if phase == "generation" else "/v1/models"
            if request.url.path == blocked:
                return httpx.Response(200, stream=Stream())
            return httpx.Response(200, json=ANSWER)

        token = tmp_path / "gateway-token"
        token.write_text("gateway-test-token")
        gateway = LumiGatewayClient(
            LumiGatewaySettings(endpoint="https://gateway.example/v1/lumi", token_file=str(token)),
            transport=httpx.MockTransport(upstream),
        )
        saved = revision(
            config={"provider": "openai-chat", "endpoint": "https://model.test/v1", "secretSlot": "apiKey"},
            secretRef={"apiKey": "model-key"},
            allowed_destinations=("https://model.test",),
        )
        service, _ = generic_connections(saved, gateway)
        service.secrets = SimpleNamespace(
            check=lambda *args, **kwargs: None, resolve=lambda *args: ResolvedSecret(value="credential-canary")
        )
        adapter = service.registry.get(saved.adapter)
        original = adapter.test_connection

        async def capture(bound):
            credentials.append(bound.credentials)
            return await original(bound)

        monkeypatch.setattr(adapter, "test_connection", capture)

        async def refresh(actor, scope, identifier, deadline, context):
            await gateway.models(
                dict(saved.config),
                "credential-canary",
                provider="openai-chat",
                timeout_seconds=max(0.001, deadline - time.monotonic()),
            )

        service.refresh = refresh
        controller = ConnectionController(service, SimpleNamespace())
        app = Starlette(routes=[Route("/api/v1" + PREFIX + "/{identifier}/test", controller.test, methods=["POST"])])
        http_scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": (
                f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}"
                f"/environments/{SCOPE.environment_id}/connections/{saved.id}/test"
            ),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "state": {"principal": ACTOR, "audit_context": CONTEXT},
        }
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            await entered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        return SimpleNamespace(
            app=app,
            http_scope=http_scope,
            receive=receive,
            send=send,
            entered=entered,
            cancelled=cancelled,
            closed=closed,
            gateway=gateway,
            credentials=credentials,
            owned_tasks=owned_tasks,
        )

    return build


@pytest.mark.parametrize("phase", ["generation", "refresh"])
async def test_generic_http_disconnect_owns_generation_and_refresh(generic_http_case, phase):
    import asyncio

    from firefly_weave.connections.secrets import SecretUnavailable

    case = generic_http_case(phase)
    before = asyncio.all_tasks()
    async with asyncio.timeout(2) as detector:
        with pytest.raises(asyncio.CancelledError):
            await case.app(case.http_scope, case.receive, case.send)
        assert case.entered.is_set() and case.cancelled.is_set() and case.closed.is_set()
    assert not detector.expired(), "The HTTP disconnect must cancel before the deadlock detector"
    assert case.gateway.active == 0
    assert case.owned_tasks and all(task.done() for task in case.owned_tasks)
    with pytest.raises(SecretUnavailable):
        case.credentials[0]("apiKey")
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("route,budget", [("dedicated", 130.0), ("generic", 30.0)])
@pytest.mark.parametrize("elapsed", [0.25, 0.0, -1.0])
async def test_test_refresh_receives_only_the_original_remaining_budget(monkeypatch, audits, route, budget, elapsed):
    import time

    from firefly_weave.connections import service as connection_module
    from firefly_weave.connections.secrets import SecretUnavailable

    now, hook_calls = [1000.0], []
    gateway = Gateway()
    saved = revision(
        config={"provider": "openai-chat", "endpoint": "https://model.test/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "model-key"},
        allowed_destinations=("https://model.test",),
    )
    connections, _ = generic_connections(saved, gateway)
    connections.secrets = SimpleNamespace(
        check=lambda *a, **kw: None, resolve=lambda *a: ResolvedSecret(value="credential-canary")
    )
    tests = AIConnectionService(connections, gateway)
    credentials = []
    adapter = connections.registry.get(saved.adapter)
    original = adapter.test_connection

    async def capture(bound):
        credentials.append(bound.credentials)
        return await original(bound)

    monkeypatch.setattr(adapter, "test_connection", capture)
    original_test = gateway.test

    async def finish(*args, **kwargs):
        answer = await original_test(*args, **kwargs)
        now[0] = 1000.0 + budget - elapsed
        return answer

    gateway.test = finish
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(ai_connections, "time", clock)
    monkeypatch.setattr(connection_module, "time", clock)

    async def refresh(actor, scope, identifier, deadline, context):
        hook_calls.append(deadline - now[0])
        if credentials:
            with pytest.raises(SecretUnavailable):
                credentials[0]("apiKey")
        assert deadline == 1000.0 + budget
        assert time.monotonic() > 0

    tests.refresh = connections.refresh = refresh
    result = (
        await tests.test(ACTOR, SCOPE, saved.id, REQUEST, context=CONTEXT)
        if route == "dedicated"
        else (await connections.test_connection(ACTOR, SCOPE, saved.id, context=CONTEXT))
    )
    assert result.ok is True
    assert hook_calls == ([elapsed] if elapsed > 0 else [])
