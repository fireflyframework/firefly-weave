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
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
from firefly_weave.contracts.ai import AIConnectionTestRequest
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision
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
        self.saved, self.secrets, self.required = saved, secrets or ScopedSecrets(), []
        self.session = Session()
        session = self.session

        class Open:
            @asynccontextmanager
            async def open(self, scope, mutation=True):
                yield SimpleNamespace(session=session, scope=scope)

        self.uow = Open()

    def require(self, actor, scope, capability, context):
        self.required.append(capability)

    async def ready_revision(self, actor, scope, revision_id, capability, *, context):
        assert revision_id == self.saved.id and capability == "connection.manage"
        return self.saved


class Gateway:
    configured = True

    def __init__(self, answer=None):
        self.answer, self.calls = answer or ANSWER, []

    async def test(self, config, credential, *, provider, model, probe_tools, timeout_seconds=120):
        self.calls.append({"credential": credential, "provider": provider, "model": model, "probe_tools": probe_tools})
        return self.answer


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
    assert connections.required == ["connection.manage"]
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


async def test_tests_and_refreshes_share_six_per_minute_per_principal():
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
    failing = Gateway({**ANSWER, "ok": False, "code": "LLM_UNREACHABLE"})
    with po.installed(POLICY):
        assert (await GatewayConnectionTester(failing)(BoundConnection("test", revision(), no_secret))).ok is False
