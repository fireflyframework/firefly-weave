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
"""Weave AI runs on a keyless local model connection: no secret is checked, resolved or sent."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfiguration, LumiReply
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import lumi as module
from firefly_weave.operations.lumi import LumiService
from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
OLLAMA = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)


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


def configuration(connection_id):
    return LumiConfiguration.model_validate(
        {
            "enabled": True,
            "connection_revision_id": connection_id,
            "revision": 1,
            "profile": {
                "provider": "openai-chat",
                "model": "qwen3:4b",
                "options": {"max_tokens": 1024},
                "timeoutSeconds": 180,
                "outputSchema": LUMI_REPLY_SCHEMA,
            },
        }
    )


class Secrets:
    def __init__(self):
        self.checked, self.resolved = [], []

    def check(self, scope, handle, *, keyless=False):
        self.checked.append((handle, keyless))
        if handle == "no-credential" and not keyless:
            raise CatalogError(422, "WV-SECRET", "Reserved handle")

    def resolve(self, scope, handle):
        self.resolved.append(handle)
        raise AssertionError("Weave AI must never resolve the reserved handle")


def service(secrets, gateway=None):
    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=SCOPE)

    connections = SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), secrets=secrets)
    return LumiService(connections, None, None, gateway, None)


def test_a_keyless_local_connection_is_accepted_without_a_secret():
    secrets = Secrets()
    with po.installed(OLLAMA):
        connection = revision()
        assert service(secrets)._connection(SCOPE, configuration(connection.id), connection) is None
    assert secrets.checked == [] and secrets.resolved == []


def test_a_keyed_connection_still_checks_its_secret_handle():
    secrets = Secrets()
    connection = revision(
        config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": "openai-key"},
        allowed_destinations=("https://api.openai.com",),
    )
    assert service(secrets)._connection(SCOPE, configuration(connection.id), connection) == "openai-key"
    assert secrets.checked == [("openai-key", False)]


def test_the_reserved_handle_outside_a_local_model_entry_is_refused():
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(Exception):  # noqa: B017 - refused before any check
        service(Secrets())._connection(SCOPE, configuration(uuid4()), revision())


async def test_asking_on_a_keyless_connection_sends_no_credential(monkeypatch):
    secrets = Secrets()
    reply = LumiReply(answer="Use a decision table for the thresholds.")
    gateway = SimpleNamespace(configured=True, ask=AsyncMock(return_value=reply))
    weave_ai = service(secrets, gateway)
    actor = SimpleNamespace(id=uuid4())
    with po.installed(OLLAMA):
        connection = revision()
        weave_ai._require = AsyncMock(return_value=actor)
        weave_ai._read = AsyncMock(return_value=configuration(connection.id))
        weave_ai._attachments = AsyncMock(return_value={"resources": []})
        monkeypatch.setattr(
            module, "ConnectionRepository", lambda tx: SimpleNamespace(revision=AsyncMock(return_value=connection))
        )
        result = await weave_ai.ask(actor, SCOPE, LumiAskRequest(message="Explain"), context=AuditContext())
    assert result.answer == reply.answer
    assert gateway.ask.await_args.args[4] is None
    assert secrets.resolved == []


async def test_the_gateway_request_leaves_out_a_missing_credential(tmp_path):
    sent = []

    async def answer(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"answer": "Ready."})

    token = tmp_path / "token"
    token.write_text("gateway-token\n")
    token.chmod(0o600)
    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://gateway.weave.test/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(answer),
    )
    connection = revision()
    request = LumiAskRequest(message="Explain")
    reply = await client.ask(configuration(connection.id), request, {"resources": []}, dict(connection.config), None)
    assert reply.answer == "Ready." and "credential" not in sent[0]
