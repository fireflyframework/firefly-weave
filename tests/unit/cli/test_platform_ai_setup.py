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

"""AI's server records are created idempotently through public operations, with no secret sent."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from ai_platform_support import SCOPE, catalog_output, manifest_output

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
from firefly_weave.contracts.ai import AIConnectionTestResult
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.sdk import platform
from firefly_weave.sdk import platform_ai_setup as setup

IMAGE = "sha256:" + "e" * 64
RELEASE = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = UUID("55555555-5555-4555-8555-555555555555")
CONNECTOR = UUID("66666666-6666-4666-8666-666666666666")
REVISION = UUID("77777777-7777-4777-8777-777777777777")
SUBJECT = "88888888-8888-4888-8888-888888888888"
KEYCLOAK = "http://localhost:55202"


class Client:
    """The public operations the AI setup uses, recorded."""

    def __init__(self, connections=(), presence="recent", answer=None):
        self.calls, self.connections, self.presence = [], list(connections), presence
        self.answer = answer or {
            "ok": True,
            "code": "ok",
            "latency_ms": 900,
            "model": "qwen2.5:1.5b",
            "tool_calling": "supported",
        }
        self.runs = 0

    async def publish(self, collection, source, format, *, idempotency_key):
        self.calls.append(("publish", collection, idempotency_key))
        return SimpleNamespace(id=CONNECTOR if collection == "connectors" else uuid4(), digest="a" * 64)

    async def invoke(self, operation, **kwargs):
        self.calls.append((operation, kwargs.get("body"), kwargs.get("identifier")))
        if operation == "releases.create":
            return SimpleNamespace(id=RELEASE)
        if operation == "principals.create":
            return SimpleNamespace(id=PRINCIPAL)
        if operation == "connections.list":
            return SimpleNamespace(items=self.connections, next_cursor=None)
        if operation == "connections.create":
            return SimpleNamespace(id=REVISION)
        if operation == "workers.list":
            worker = SimpleNamespace(
                id=uuid4(), release_id=RELEASE, presence=self.presence, revoked=False, last_seen_at=datetime.now(UTC)
            )
            return SimpleNamespace(items=[worker], next_cursor=None)
        if operation == "ai_connections.test":
            return AIConnectionTestResult.model_validate(self.answer)
        return SimpleNamespace()

    async def activate(self, request, *, idempotency_key):
        self.calls.append(("activate", request, idempotency_key))
        return SimpleNamespace(id=uuid4())

    async def start_run(self, request, *, idempotency_key):
        self.calls.append(("start", request, idempotency_key))
        return SimpleNamespace(id=uuid4())

    async def read_run(self, identifier):
        self.runs += 1
        status = "running" if self.runs == 1 else "succeeded"
        output = None if status == "running" else "Weave keeps durable records of every run."
        return SimpleNamespace(state=SimpleNamespace(status=status, output=output))

    async def history(self, identifier, *, limit=100):
        usage = {"requests": 1, "inputTokens": 40, "outputTokens": 12}
        return SimpleNamespace(events=[SimpleNamespace(type="task_completed", data={"output": {"usage": usage}})])

    async def replay(self, identifier):
        return SimpleNamespace(status="consistent")

    def operations(self):
        return [call[0] for call in self.calls]


async def immediately(seconds):
    return None


def test_the_image_catalog_must_be_this_servers_catalog():
    found = setup.verify_catalog(catalog_output())
    assert FrozenDocument.from_value(found["connector"]).digest == setup.CONNECTOR_DIGEST
    assert FrozenDocument.from_value(found["action"]).digest == setup.ACTION_DIGEST
    changed = json.loads(catalog_output())
    changed["definitions"][1]["document"]["spec"]["timeoutSeconds"] = 5
    with pytest.raises(platform.PlatformError, match="does not carry this server's AI catalog"):
        setup.verify_catalog(json.dumps(changed).encode())
    assert setup.release_manifest(manifest_output())["credential_capabilities"] == ["weave-agentic.generate@1.0.0"]


async def test_definitions_publish_with_digest_keys_and_the_release_by_image():
    client = Client()
    found = setup.verify_catalog(catalog_output())
    assert await setup.publish(client, "connectors", found["connector"], setup.CONNECTOR_DIGEST) == str(CONNECTOR)
    assert await setup.admit_release(client, IMAGE, setup.release_manifest(manifest_output())) == str(RELEASE)
    assert client.calls[0] == ("publish", "connectors", "weave-platform-ai-connectors-" + setup.CONNECTOR_DIGEST[:40])
    release = client.calls[1][1]
    assert release.image_digest == IMAGE and release.credential_capabilities == ["weave-agentic.generate@1.0.0"]


async def test_the_worker_principal_is_created_linked_and_granted_once():
    client, receipt, subjects = Client(), {}, []

    async def subject():
        subjects.append(1)
        return SUBJECT

    def save(update):
        receipt.update(update)

    arguments = {"release_id": str(RELEASE), "issuer": KEYCLOAK + "/realms/weave", "subject": subject}
    assert await setup.worker_principal(client, SCOPE, receipt=receipt, save=save, **arguments) is True
    assert receipt == {"principal_id": str(PRINCIPAL), "linked": True, "granted_release_id": str(RELEASE)}
    link = next(call for call in client.calls if call[0] == "principals.link")
    assert link[1].subject == SUBJECT and link[1].provider_id == "local-keycloak" and link[2] == PRINCIPAL
    grant = next(call for call in client.calls if call[0] == "members.grant")[1]
    assert grant.role == "worker" and grant.resources == (str(RELEASE), "weave-agentic.generate@1.0.0")
    again = Client()
    assert await setup.worker_principal(again, SCOPE, receipt=receipt, save=save, **arguments) is False
    assert again.calls == [] and subjects == [1]


async def test_the_worker_subject_is_the_keycloak_service_account():
    def keycloak(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if request.url.path.endswith("/clients"):
            assert request.url.params["clientId"] == "weave-worker"
            return httpx.Response(200, json=[{"id": "client-1", "clientId": "weave-worker"}])
        assert request.url.path.endswith("/clients/client-1/service-account-user")
        return httpx.Response(200, json={"id": SUBJECT})

    assert await setup.worker_subject(KEYCLOAK, "admin-secret", httpx.MockTransport(keycloak)) == SUBJECT


def basic_scope():
    return {
        "id": SUBJECT,
        "name": "basic",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
        "protocolMappers": [
            {"id": "sub-mapper", "name": "sub", "protocolMapper": "oidc-sub-mapper", "config": {}},
            {"id": "time-mapper", "name": "auth_time", "protocolMapper": "oidc-usersessionmodel-note-mapper"},
        ],
    }


async def test_managed_worker_scope_is_visible_without_replacing_mappers_or_other_attributes():
    original = basic_scope()
    stored = json.loads(json.dumps(original))

    def keycloak(request):
        nonlocal stored
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if request.method == "GET":
            assert request.url.path.endswith("/client-scopes")
            return httpx.Response(200, json=[stored, {"id": "other", "name": "profile"}])
        assert request.method == "PUT" and request.url.path.endswith("/client-scopes/" + SUBJECT)
        stored = json.loads(request.content)
        return httpx.Response(204)

    transport = httpx.MockTransport(keycloak)
    assert await setup.ensure_worker_scope(KEYCLOAK, "admin-secret", transport) is True
    assert stored == {
        **original,
        "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
    }
    assert await setup.ensure_worker_scope(KEYCLOAK, "admin-secret", transport) is False


@pytest.mark.parametrize("broken", [[], [basic_scope(), basic_scope()], [{**basic_scope(), "attributes": None}]])
async def test_an_unrecognized_managed_scope_is_refused_before_any_update(broken):
    def keycloak(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        assert request.method == "GET"
        return httpx.Response(200, json=broken)

    with pytest.raises(platform.PlatformError, match="worker scope"):
        await setup.ensure_worker_scope(KEYCLOAK, "admin-secret", httpx.MockTransport(keycloak))


async def test_a_refused_managed_scope_update_reports_a_safe_failure():
    def keycloak(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        if request.method == "GET":
            return httpx.Response(200, json=[basic_scope()])
        return httpx.Response(403, json={"error": "private-provider-detail"})

    with pytest.raises(platform.PlatformError, match="refused.*worker scope") as failure:
        await setup.ensure_worker_scope(KEYCLOAK, "admin-secret", httpx.MockTransport(keycloak))
    assert "private-provider-detail" not in str(failure.value) and "admin-secret" not in str(failure.value)


def saved_connection(**changes):
    value = {
        "id": REVISION,
        "revision": 1,
        "name": "ollama-local",
        "connector_version_id": CONNECTOR,
        "connector": "weave-agentic-provider@1.0.0",
        "connector_digest": AGENTIC_DESCRIPTOR.manifest.digest,
        "adapter": "weave-agentic-provider",
        "config": {"provider": "openai-chat", "endpoint": "http://ollama:11434/v1", "secretSlot": "apiKey"},
        "secretRef": {"apiKey": "no-credential"},
        "allowed_destinations": ("http://ollama:11434",),
    }
    value.update(changes)
    return ConnectionRevision.model_validate(value)


async def test_the_connection_is_keyless_and_reused_when_it_matches():
    request = setup.connection_request(str(CONNECTOR), "http://ollama:11434/v1", "http://ollama:11434")
    assert request.secret_refs == {"apiKey": "no-credential"} and request.allowed_destinations == (
        "http://ollama:11434",
    )
    client = Client(connections=[saved_connection()])
    assert await setup.ensure_connection(client, request) == (str(REVISION), False)
    assert "connections.create" not in client.operations()
    other = Client(
        connections=[
            saved_connection(
                config={
                    "provider": "openai-chat",
                    "endpoint": "http://host.docker.internal:11434/v1",
                    "secretSlot": "apiKey",
                }
            )
        ]
    )
    assert await setup.ensure_connection(other, request) == (str(REVISION), True)


async def test_the_worker_must_come_online_in_time():
    found = await setup.wait_online(Client(), str(RELEASE), sleep=immediately)
    assert found["presence"] == "recent"
    ticks = iter(range(0, 1000, 30))
    with pytest.raises(platform.PlatformError, match="did not come online"):
        await setup.wait_online(Client(presence="stale"), str(RELEASE), sleep=immediately, clock=lambda: next(ticks))


async def test_the_connection_test_returns_its_result_with_a_timestamp():
    client = Client()
    result = await setup.test_connection(client, str(REVISION), "qwen2.5:1.5b")
    assert client.operations() == ["ai_connections.test"]
    assert result["ok"] is True and result["tool_calling"] == "supported" and "tested_at" in result


async def test_the_smoke_run_succeeds_counts_requests_and_replays():
    client = Client()
    result = await setup.smoke(
        client, SCOPE, release_id=str(RELEASE), revision_id=str(REVISION), model="qwen2.5:1.5b", sleep=immediately
    )
    assert result["status"] == "succeeded" and result["requests"] == 1 and result["replay"] == "consistent"
    activation = next(call for call in client.calls if call[0] == "activate")[1]
    assert activation.worker_release_ids == {"weave-agentic.generate": RELEASE}
    assert activation.connection_revision_ids == {"ai": REVISION}
    workflow = setup.smoke_workflow("qwen2.5:1.5b")
    assert workflow["spec"]["llmProfiles"]["smoke"]["model"] == "qwen2.5:1.5b"
    assert workflow["metadata"]["name"] != setup.smoke_workflow("qwen3:4b")["metadata"]["name"]


async def test_a_failed_smoke_run_reports_the_model_code():
    class Failing(Client):
        async def read_run(self, identifier):
            return SimpleNamespace(state=SimpleNamespace(status="running", output=None))

        async def history(self, identifier, *, limit=100):
            failed = SimpleNamespace(type="task_failed", data={"output": {"code": "LLM_MODEL_NOT_FOUND"}})
            return SimpleNamespace(events=[failed])

    with pytest.raises(platform.PlatformError, match="LLM_MODEL_NOT_FOUND"):
        await setup.smoke(
            Failing(), SCOPE, release_id=str(RELEASE), revision_id=str(REVISION), model="qwen3:4b", sleep=immediately
        )


def test_refusals_name_the_code_and_send_no_secret():
    error = setup.refused("connection", SimpleNamespace(code="WV-CONNECTION"))
    assert "WV-CONNECTION" in str(error) and "No secret was sent" in str(error)
