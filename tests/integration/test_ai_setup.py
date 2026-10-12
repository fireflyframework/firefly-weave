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

"""AI discovery, readiness, setup and activation admission on the guarded PostgreSQL backend.

Every record is written by the real services; SQL here only counts rows, reads them back, and
marks a worker as recently seen. The AI gateway is an in-process fake: no model is called.
"""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration

AI_CONNECTOR = "weave-agentic-provider@1.0.0"
CAPABILITY = "weave-agentic.generate@1.0.0"
ENDPOINT = "https://models.example/v1"
OLLAMA = "http://ollama:11434"
SECRET_CANARY = "wv-canary-ai-setup-secret-" + uuid4().hex
PROVIDER_CANARY = "provider-body-canary-" + uuid4().hex[:12]
PROMPT_CANARY = "prompt-canary-" + uuid4().hex
EXAMPLE_CONNECTOR = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Connector",
    "metadata": {"name": "example", "version": "1.0.0"},
    "spec": {
        "adapter": "test-adapter",
        "configSchema": {},
        "authSchema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        "actions": {"get": {"inputSchema": {}, "outputSchema": {}, "sideEffect": "read_only", "timeoutSeconds": 10}},
        "compatibility": {"apiVersion": "weave/v1alpha1"},
        "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 10},
    },
}


def write_policy(path: Path, *models: str, url: str = ENDPOINT, credential: str = "required") -> None:
    endpoint = {
        "id": "models",
        "label": "Models",
        "url": url,
        "providers": ["openai-chat"],
        "credential": credential,
        "models": "served" if models == ("served",) else list(models),
    }
    path.write_text(json.dumps({"version": 2, "endpoints": [endpoint]}))
    path.chmod(0o600)


@pytest.fixture
async def ai_setup(services, access_db, provisioned, tmp_path, monkeypatch):
    from firefly_weave.connections.bindable import BindableConnectionService
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, task_capability
    from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionTestResult
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.ai_connections import AIConnectionService
    from firefly_weave.operations.ai_models import AIModelService
    from firefly_weave.operations.ai_policy import APIAIPolicy
    from firefly_weave.operations.ai_readiness import AIReadinessService
    from firefly_weave.operations.ai_setup import AISetupService
    from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings
    from firefly_weave.workers.service import WorkerService

    sessions, owner, access, _ = access_db
    admin, (scope, other) = provisioned
    project = scope.model_copy(update={"environment_id": None})

    async def principal(*grants):
        identifier = await access.create_principal(admin, "application")
        for role, where, resources in grants:
            await access.grant(admin, identifier, Grant(role=role, scope=where, resources=resources))
        return await access.load_principal(identifier)

    operator = await principal(
        ("developer", project, ()),
        ("deployer", scope, ()),
        ("tenant_admin", scope, ()),
        ("viewer", scope, ()),
        ("worker", scope, ()),
        ("deployer", other, ()),
    )
    viewer = await principal(("viewer", scope, ()))

    class Unused:
        async def execute(self, input, context):
            raise AssertionError("AI setup executed a connector call")

        async def test_connection(self, connection):
            raise AssertionError("AI setup tested through the connector adapter")

    class Example:
        async def execute(self, input, context):
            return input

        async def test_connection(self, connection):
            return ConnectionTestResult(ok=True)

    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, Unused())
    registry.register_trusted("test-adapter", Example())
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_AI_SETUP", SECRET_CANARY)
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()},
        (
            SecretGrant(scope, "ai-key", "env", "WEAVE_CONNECTION_SECRET_AI_SETUP"),
            SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_AI_SETUP"),
        ),
    )
    token = tmp_path / "gateway-token"
    token.write_text("gateway-service-token")
    token.chmod(0o600)
    gateway_calls = []

    def gateway_answer(request):
        body = json.loads(request.content)
        gateway_calls.append((request.url.path, body))
        if request.url.path == "/v1/test":
            return httpx.Response(
                200,
                json={"ok": True, "code": "ok", "latency_ms": 12, "model": body["model"], "tool_calling": "supported"},
            )
        assert request.url.path == "/v1/models"
        # The AI gateway's answer for this listed endpoint, which is not Ollama: provider names carry no
        # tool facts, and each approved name the provider did not report is added as unavailable.
        return httpx.Response(
            200,
            json={
                "discovery": "ok",
                "models": [
                    {"name": "chosen", "approved": True, "available": True, "tools": "unknown"},
                    {"name": PROVIDER_CANARY, "approved": False, "available": True, "tools": "unknown"},
                    {"name": "other", "approved": True, "available": False, "tools": "unknown"},
                ],
            },
        )

    gateway = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://gateway.test/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(gateway_answer),
    )
    policy_path = tmp_path / "ai-policy.json"
    write_policy(policy_path, "chosen", "other")
    policy = APIAIPolicy(policy_path)
    graph = services(sessions, registry=registry, secrets=secrets)
    graph.register_instance(LumiGatewayClient, gateway)
    graph.register_instance(APIAIPolicy, policy)
    env = SimpleNamespace(
        sessions=sessions,
        owner=owner,
        access=access,
        admin=admin,
        scope=scope,
        other=other,
        project=project,
        operator=operator,
        viewer=viewer,
        principal=principal,
        registry=registry,
        secrets=secrets,
        gateway_calls=gateway_calls,
        policy_path=policy_path,
        policy=policy,
        definitions=graph.resolve(DefinitionService),
        connections=graph.resolve(ConnectionService),
        workers=graph.resolve(WorkerService),
        setup=graph.resolve(AISetupService),
        models=graph.resolve(AIModelService),
        readiness=graph.resolve(AIReadinessService),
        tests=graph.resolve(AIConnectionService),
        bindable=graph.resolve(BindableConnectionService),
    )
    assert env.definitions.ai_policy is policy and env.models.policy is policy and env.models.gateway is gateway

    async def release(index=0, *, target=scope, capability=None, credential=True):
        value = (capability or task_capability()).model_dump(mode="json", by_alias=True)
        request = ReleaseRequest.model_validate_json(
            json.dumps(
                {
                    "image_digest": f"sha256:{index:064x}",
                    "capabilities": [value],
                    "credential_capabilities": [CAPABILITY] if credential else [],
                }
            )
        )
        return await env.workers.register_release(operator, target, request, context=AuditContext())

    async def connection(name="provider", *, endpoint=ENDPOINT, handle="ai-key", version_id=None):
        if version_id is None:
            version_id = (await env.setup.publish(operator, scope, context=AuditContext())).connector.id
        origin = endpoint.removesuffix("/v1")
        request = ConnectionRequest.model_validate_json(
            json.dumps(
                {
                    "name": name,
                    "connector_version_id": str(version_id),
                    "config": {"provider": "openai-chat", "endpoint": endpoint, "secretSlot": "apiKey"},
                    "secretRef": {"apiKey": handle},
                    "allowed_destinations": [origin],
                }
            )
        )
        return await env.connections.create_revision(operator, scope, request, context=AuditContext())

    published = {}

    async def example_connection(name):
        if "example" not in published:
            published["example"] = await env.definitions.publish(
                operator, project, "Connector", json.dumps(EXAMPLE_CONNECTOR), "json", "example", context=AuditContext()
            )
        request = ConnectionRequest.model_validate_json(
            json.dumps(
                {
                    "name": name,
                    "connector_version_id": str(published["example"].id),
                    "secretRef": {"token": "approved"},
                    "allowed_destinations": ["https://example.invalid"],
                }
            )
        )
        return await env.connections.create_revision(operator, scope, request, context=AuditContext())

    async def online(release_id):
        instance = await env.workers.register_instance(
            operator,
            scope,
            InstanceRequest(release_id=release_id, task_types=[CAPABILITY], capacity=1),
            context=AuditContext(),
        )
        async with owner.begin() as session:
            await session.execute(
                text("UPDATE worker_instances SET last_seen_at=clock_timestamp() WHERE id=:id"), {"id": instance.id}
            )
        return instance

    async def scalar(sql, **params):
        async with owner.begin() as session:
            return await session.scalar(text(sql), params)

    async def rows(sql, **params):
        async with owner.begin() as session:
            return [tuple(row) for row in (await session.execute(text(sql), params)).all()]

    env.release, env.connection, env.example_connection, env.online = release, connection, example_connection, online
    env.scalar, env.rows = scalar, rows
    return env


def scoped(env, table, target=None, **filters):
    target = target or env.scope
    sql = f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant AND project_id=:project"
    if target.environment_id is not None:
        sql += " AND environment_id=:environment"
    for name in filters:
        sql += f" AND {name}=:{name}"
    return env.scalar(
        sql, tenant=target.tenant_id, project=target.project_id, environment=target.environment_id, **filters
    )


async def http_caller(env):
    async with env.owner.begin() as session:
        return await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))


def items(result):
    return {item.id: item for item in result.items}


# Setup publication and authorization are idempotent and stay in their scope.


async def test_repeated_publish_requests_return_the_same_versions_and_exactly_two_definitions(
    ai_setup, client, headers, env_url
):
    env = ai_setup
    await env.release()
    caller = await http_caller(env)
    await env.access.grant(env.admin, caller, Grant(role="developer", scope=env.project))
    first = await client.post(env_url + "/ai/setup/publish", headers=headers, json={})
    again = await client.post(env_url + "/ai/setup/publish", headers=headers)
    assert (first.status_code, again.status_code) == (200, 200), first.text
    assert first.json() == again.json()
    assert first.headers.get_list("cache-control") == ["no-store"]
    published = first.json()
    assert (published["connector"]["name"], [item["name"] for item in published["actions"]]) == (
        "weave-agentic-provider",
        ["weave-agentic-generate"],
    )
    # Another principal's request reuses the same immutable versions.
    reused = await env.setup.publish(env.operator, env.scope, context=AuditContext())
    assert reused.model_dump(mode="json") == published
    assert await env.rows(
        "SELECT kind,name,version FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project "
        "ORDER BY kind",
        tenant=env.scope.tenant_id,
        project=env.scope.project_id,
    ) == [("Action", "weave-agentic-generate", "1.0.0"), ("Connector", "weave-agentic-provider", "1.0.0")]
    assert await env.scalar("SELECT count(*) FROM access_audit WHERE action='ai.setup.publish'") == 3


async def test_publish_without_an_admitted_ai_worker_writes_nothing(ai_setup):
    env = ai_setup
    with pytest.raises(CatalogError) as refused:
        await env.setup.publish(env.operator, env.scope, context=AuditContext())
    assert (refused.value.status, refused.value.code) == (422, "WV-AI-WORKER-MISSING")
    assert await scoped(env, "definition_versions", env.project) == 0
    assert await env.scalar("SELECT count(*) FROM access_audit WHERE action='ai.setup.publish'") == 0


async def test_repeated_grant_requests_leave_one_active_pair_for_each_of_101_releases(
    ai_setup, client, headers, env_url, authenticated_client
):
    from firefly_weave.connections.service import ConnectionService

    env = ai_setup
    for index in range(101):
        await env.release(index)
    # A release without credential declarations and one in another environment get no grant.
    await env.release(500, credential=False)
    await env.release(600, target=env.other)
    revision = await env.connection()
    app = authenticated_client[0]._transport.app
    app.state.pyfly.context.get_bean(ConnectionService).secrets = env.secrets
    caller = await http_caller(env)
    await env.access.grant(env.admin, caller, Grant(role="tenant_admin", scope=env.scope))
    body = {"connection_revision_id": str(revision.id)}
    first = await client.post(env_url + "/ai/setup/grant", headers=headers, json=body)
    again = await client.post(env_url + "/ai/setup/grant", headers=headers, json=body)
    assert (first.status_code, again.status_code) == (200, 200), first.text
    assert first.json() == again.json() == {"granted": True, "not_needed": False}
    assert first.headers.get_list("cache-control") == ["no-store"]
    pairs = await env.rows(
        "SELECT release_id,count(*) FROM worker_connection_grants WHERE tenant_id=:tenant AND project_id=:project "
        "AND environment_id=:environment AND connection_id=:revision AND capability=:capability AND NOT revoked "
        "GROUP BY release_id",
        tenant=env.scope.tenant_id,
        project=env.scope.project_id,
        environment=env.scope.environment_id,
        revision=revision.id,
        capability=CAPABILITY,
    )
    assert len(pairs) == 101 and {count for _, count in pairs} == {1}
    assert await scoped(env, "worker_connection_grants") == 101
    assert await scoped(env, "worker_connection_grants", env.other) == 0
    assert await env.scalar("SELECT count(*) FROM access_audit WHERE action='ai.setup.grant'") == 2


async def test_a_release_that_changes_the_generate_contract_is_refused(ai_setup):
    from firefly_weave.contracts.agentic import task_capability

    env = ai_setup
    await env.release()
    changed = task_capability().model_copy(update={"timeout_seconds": 60})
    with pytest.raises(CatalogError) as refused:
        await env.release(1, capability=changed)
    assert (refused.value.status, refused.value.code) == (409, "WV-CAPABILITY-CONFLICT")
    assert await scoped(env, "worker_releases") == 1


async def test_grant_refuses_a_connection_that_is_not_an_ai_connection(ai_setup):
    from firefly_weave.contracts.ai import AISetupGrantRequest

    env = ai_setup
    await env.release()
    other = await env.example_connection("example")
    with pytest.raises(CatalogError) as refused:
        await env.setup.grant(
            env.operator, env.scope, AISetupGrantRequest(connection_revision_id=other.id), context=AuditContext()
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-AI-CONNECTION")
    assert await scoped(env, "worker_connection_grants") == 0


# Bindable connection choices: exact per-revision authority across scope-bound pages.


async def test_bindable_pages_return_exactly_the_granted_revisions_across_205_connections(ai_setup):
    from firefly_weave.contracts.bindable_connections import BindableConnectionQuery

    env = ai_setup
    await env.release()
    ai = [await env.connection(f"ai-{index:03d}") for index in range(5)]
    others = [await env.example_connection(f"example-{index:03d}") for index in range(200)]
    revisions = sorted([*ai, *others], key=lambda row: row.id)
    assert len(revisions) == 205
    allowed = {row.id for row in revisions[::7]} | {ai[0].id, ai[3].id}
    binder = await env.principal(
        ("deployer", env.scope, tuple(sorted(str(identifier) for identifier in allowed))),
        ("deployer", env.other, ()),
    )
    unrestricted = await env.principal(("deployer", env.scope, ()))
    decoded = runpy.run_path(str(Path(__file__).resolve().parents[1] / "unit/test_bindable_connections.py"))[
        "decoded_cursor_identifier"
    ]

    async def walk(principal, limit, connector=None, visible=None):
        seen, cursor, pages = [], None, 0
        while True:
            page = await env.bindable.list(
                principal,
                env.scope,
                BindableConnectionQuery(connector=connector, limit=limit, cursor=cursor),
                context=AuditContext(),
            )
            pages += 1
            seen.extend(page.items)
            if page.next_cursor is None:
                return seen, pages
            # A continuation names only the last revision this caller may bind, never a denied one.
            assert decoded(page) == page.items[-1].revision_id
            assert visible is None or decoded(page) in visible
            cursor = page.next_cursor

    granted, pages = await walk(binder, 7, visible=allowed)
    assert [item.revision_id for item in granted] == sorted(allowed)
    assert pages == -(-len(allowed) // 7)
    everything, _ = await walk(unrestricted, 50)
    assert [item.revision_id for item in everything] == [row.id for row in revisions]
    filtered, _ = await walk(binder, 50, AI_CONNECTOR, visible=allowed)
    assert [item.revision_id for item in filtered] == sorted({ai[0].id, ai[3].id} | {row.id for row in ai} & allowed)
    for item in filtered:
        assert (item.connection_id, item.provider, item.endpoint_origin, item.enabled) == (
            item.revision_id,
            "openai-chat",
            "https://models.example:443",
            True,
        )
    serialized = json.dumps([item.model_dump(mode="json") for item in everything])
    for leak in ("ai-key", "approved", "/v1", "secretRef", "config", "allowed_destinations", SECRET_CANARY):
        assert leak not in serialized
    # Cursors are bound to their scope, principal and connector filter.
    first = await env.bindable.list(binder, env.scope, BindableConnectionQuery(limit=1), context=AuditContext())
    assert first.next_cursor is not None and decoded(first) in allowed
    # The caller may list the other environment, but not continue this list there.
    elsewhere = await env.bindable.list(binder, env.other, BindableConnectionQuery(limit=1), context=AuditContext())
    assert elsewhere.items == []
    with pytest.raises(ValueError):
        await env.bindable.list(
            binder, env.other, BindableConnectionQuery(limit=1, cursor=first.next_cursor), context=AuditContext()
        )
    for principal, query in (
        (unrestricted, BindableConnectionQuery(limit=1, cursor=first.next_cursor)),
        (binder, BindableConnectionQuery(limit=1, cursor=first.next_cursor, connector=AI_CONNECTOR)),
    ):
        with pytest.raises(ValueError):
            await env.bindable.list(principal, env.scope, query, context=AuditContext())
    with pytest.raises(AccessDenied):
        await env.bindable.list(env.viewer, env.scope, BindableConnectionQuery(), context=AuditContext())


async def test_bindable_http_pages_default_to_fifty_and_disable_storage(ai_setup, client, headers, env_url):
    env = ai_setup
    for index in range(55):
        await env.example_connection(f"example-{index:03d}")
    caller = await http_caller(env)
    assert (await client.get(env_url + "/bindable-connections", headers=headers)).status_code == 403
    await env.access.grant(env.admin, caller, Grant(role="deployer", scope=env.scope))
    page = await client.get(env_url + "/bindable-connections", headers=headers)
    assert page.status_code == 200 and page.headers.get_list("cache-control") == ["no-store"]
    assert len(page.json()["items"]) == 50 and page.json()["next_cursor"]
    rest = await client.get(
        env_url + "/bindable-connections", headers=headers, params={"cursor": page.json()["next_cursor"]}
    )
    assert len(rest.json()["items"]) == 5 and rest.json()["next_cursor"] is None
    assert (await client.get(env_url + "/bindable-connections?limit=101", headers=headers)).status_code == 422


# Discovery metadata: refresh needs management, cached reads need only the catalog.


async def test_model_metadata_refreshes_once_and_viewers_read_the_cached_policy_answer(ai_setup):
    from firefly_weave.contracts.ai import AIModelsQuery

    env = ai_setup
    await env.release()
    revision = await env.connection()
    passive = AIModelsQuery(connection=revision.id)
    cold = await env.models.models(env.viewer, env.scope, passive, context=AuditContext())
    assert (cold.approval, cold.discovery, cold.models) == ("listed", "unknown", [])
    with pytest.raises(AccessDenied):
        await env.models.models(
            env.viewer, env.scope, AIModelsQuery(connection=revision.id, refresh=True), context=AuditContext()
        )
    assert env.gateway_calls == []
    refreshed = await env.models.models(
        env.operator, env.scope, AIModelsQuery(connection=revision.id, refresh=True), context=AuditContext()
    )
    # Approved names remain, including the listed one the provider did not report; the gateway's
    # unapproved row never reaches the caller.
    assert (refreshed.approval, refreshed.discovery) == ("listed", "ok")
    assert [(row.name, row.approved, row.available, row.tools) for row in refreshed.models] == [
        ("chosen", True, True, "unknown"),
        ("other", True, False, "unknown"),
    ]
    assert len(env.gateway_calls) == 1 and env.gateway_calls[0][1]["credential"] == SECRET_CANARY
    assert await env.models.models(env.viewer, env.scope, passive, context=AuditContext()) == refreshed
    # A changed policy is another cache entry: passive reads never reuse the old answer.
    write_policy(env.policy_path, "chosen")
    changed = await env.models.models(env.viewer, env.scope, passive, context=AuditContext())
    assert (changed.discovery, changed.models) == ("unknown", [])
    assert len(env.gateway_calls) == 1


# Readiness: one read-only snapshot, redacted by the caller's current permissions.


async def test_readiness_is_complete_for_the_operator_and_redacted_for_a_viewer(ai_setup, monkeypatch):
    from firefly_weave.connections.secrets import ScopedSecrets
    from firefly_weave.contracts.ai import AIConnectionTestRequest, AISetupGrantRequest

    env = ai_setup
    release = await env.release()
    await env.online(release.id)
    revision = await env.connection()
    await env.setup.grant(
        env.operator, env.scope, AISetupGrantRequest(connection_revision_id=revision.id), context=AuditContext()
    )
    await env.tests.test(
        env.operator, env.scope, revision.id, AIConnectionTestRequest(model="chosen"), context=AuditContext()
    )
    tables = ("access_audit", "connection_test_jobs", "worker_connection_grants", "definition_versions")
    before = [await env.scalar(f"SELECT count(*) FROM {table}") for table in tables]
    calls = len(env.gateway_calls)

    def resolved(*args, **kwargs):
        raise AssertionError("Readiness resolved a secret value")

    monkeypatch.setattr(ScopedSecrets, "resolve", resolved)
    complete = items(await env.readiness.read(env.operator, env.scope, context=AuditContext()))
    assert {name: (item.status, item.detail) for name, item in complete.items()} == {
        "worker_installed": ("done", "worker_installed"),
        "actions_published": ("done", "actions_published"),
        "worker_online": ("done", "worker_online"),
        "connection": ("done", "connection_available"),
        "key_available": ("done", "key_grant_available"),
        "connection_authorized": ("done", "connection_authorized"),
        "model_responds": ("done", "test_passed"),
        "weave_ai": ("unknown", "requires_lumi_user"),
    }
    redacted = await env.readiness.read(env.viewer, env.scope, context=AuditContext())
    assert {name: (item.status, item.detail) for name, item in items(redacted).items()} == {
        "worker_installed": ("done", "worker_installed"),
        "actions_published": ("unknown", "requires_developer"),
        "worker_online": ("done", "worker_online"),
        "connection": ("unknown", "requires_deployer"),
        "key_available": ("unknown", "requires_tenant_admin"),
        "connection_authorized": ("unknown", "requires_tenant_admin"),
        "model_responds": ("unknown", "requires_tenant_admin"),
        "weave_ai": ("unknown", "requires_lumi_user"),
    }
    serialized = redacted.model_dump_json()
    for leak in (str(revision.id), str(release.id), "ai-key", "models.example", "chosen", SECRET_CANARY):
        assert leak not in serialized
    assert [await env.scalar(f"SELECT count(*) FROM {table}") for table in tables] == before
    assert len(env.gateway_calls) == calls


async def test_readiness_http_read_answers_a_viewer_without_storage(ai_setup, client, headers, env_url):
    env = ai_setup
    await env.release()
    response = await client.get(env_url + "/ai/readiness", headers=headers)
    assert response.status_code == 200 and response.headers.get_list("cache-control") == ["no-store"]
    found = {item["id"]: item for item in response.json()["items"]}
    assert (found["worker_installed"]["status"], found["key_available"]["detail"]) == (
        "done",
        "requires_tenant_admin",
    )


async def test_an_ai_name_moved_to_another_connector_leaves_no_ai_readiness_behind(ai_setup):
    from firefly_weave.contracts.ai import AIConnectionTestRequest, AISetupGrantRequest
    from firefly_weave.operations.ai_facts import AIFacts

    env = ai_setup
    await env.release()
    first = await env.connection("provider")
    await env.tests.test(
        env.operator, env.scope, first.id, AIConnectionTestRequest(model="chosen"), context=AuditContext()
    )
    await env.setup.grant(
        env.operator, env.scope, AISetupGrantRequest(connection_revision_id=first.id), context=AuditContext()
    )
    moved = await env.example_connection("provider")
    assert (first.revision, moved.revision, moved.connector) == (1, 2, "example@1.0.0")
    stored = await env.rows(
        "SELECT j.revision_id,r.payload->>'kind',r.payload->>'ok',j.tenant_id,j.project_id,j.environment_id "
        "FROM connection_test_jobs j JOIN connection_test_results r ON r.job_id=j.id AND r.tenant_id=j.tenant_id "
        "AND r.project_id=j.project_id AND r.environment_id=j.environment_id"
    )
    assert stored == [(first.id, "ai", "true", env.scope.tenant_id, env.scope.project_id, env.scope.environment_id)]
    async with env.definitions.transaction(env.scope, None, mutation=False) as tx:
        assert await AIFacts(tx).latest_connections() == []
    found = items(await env.readiness.read(env.operator, env.scope, context=AuditContext()))
    assert {name: (found[name].status, found[name].detail) for name in found if name != "weave_ai"} == {
        "worker_installed": ("done", "worker_installed"),
        "actions_published": ("done", "actions_published"),
        "worker_online": ("action", "worker_offline"),
        "connection": ("action", "connection_missing"),
        "key_available": ("action", "connection_missing"),
        "connection_authorized": ("action", "connection_missing"),
        "model_responds": ("action", "test_required"),
    }


async def test_a_new_ai_revision_supersedes_its_tested_predecessor_in_name_order(ai_setup):
    from firefly_weave.contracts.ai import AIConnectionTestRequest
    from firefly_weave.operations.ai_facts import AIFacts

    env = ai_setup
    await env.release()
    stable = await env.connection("b-stable")
    first = await env.connection("a-replaced")
    for revision in (stable, first):
        await env.tests.test(
            env.operator, env.scope, revision.id, AIConnectionTestRequest(model="chosen"), context=AuditContext()
        )
    found = items(await env.readiness.read(env.operator, env.scope, context=AuditContext()))
    assert (found["model_responds"].status, found["model_responds"].detail) == ("done", "test_passed")
    second = await env.connection("a-replaced")
    assert second.revision == 2
    async with env.definitions.transaction(env.scope, None, mutation=False) as tx:
        latest = await AIFacts(tx).latest_connections()
    assert [row.id for row in latest] == sorted([stable.id, second.id])
    found = items(await env.readiness.read(env.operator, env.scope, context=AuditContext()))
    assert (found["model_responds"].status, found["model_responds"].detail) == ("action", "test_required")


# The Studio bridge forwards worker grants; the server still checks current management authority.


async def test_bridged_worker_grant_without_management_is_refused_by_the_server(
    ai_setup, authenticated_client, tmp_path
):
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.studio.host import make_studio_app
    from firefly_weave.studio.service import StudioOptions, StudioProfile

    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    api, tokens, _ = authenticated_client
    api._transport.app.state.pyfly.context.get_bean(ConnectionService).secrets = env.secrets
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    origin = "http://127.0.0.1:8766"
    studio = make_studio_app(
        StudioOptions(
            origin=origin,
            assets=assets,
            pairing_code="terminal-only-pairing-code",
            profile=StudioProfile(
                name="Dev",
                base_url="https://api.example",
                tenant_id=str(env.scope.tenant_id),
                project_id=str(env.scope.project_id),
                environment_id=str(env.scope.environment_id),
            ),
            token_provider=lambda: tokens[0],
            transport=httpx.ASGITransport(api._transport.app),
        )
    )
    path = (
        f"/api/v1/tenants/{env.scope.tenant_id}/projects/{env.scope.project_id}"
        f"/environments/{env.scope.environment_id}/worker-connection-grants"
    )
    body = {"release_id": str(release.id), "connection_revision_id": str(revision.id), "capability": CAPABILITY}
    async with (
        studio.router.lifespan_context(studio),
        httpx.AsyncClient(transport=httpx.ASGITransport(studio), base_url=origin) as browser,
    ):
        paired = await browser.post(
            "/studio/session", json={"code": "terminal-only-pairing-code"}, headers={"Origin": origin}
        )
        headers = {"Origin": origin, "X-Weave-CSRF": paired.json()["csrfToken"]}
        refused = await browser.post(f"/studio/api{path}", json=body, headers=headers)
        assert (refused.status_code, refused.json()["code"]) == (403, "WV-FORBIDDEN")
        assert await scoped(env, "worker_connection_grants") == 0
        caller = await http_caller(env)
        await env.access.grant(env.admin, caller, Grant(role="tenant_admin", scope=env.scope))
        granted = await browser.post(f"/studio/api{path}", json=body, headers=headers)
        assert granted.status_code == 201, granted.text
    assert await scoped(env, "worker_connection_grants") == 1


# Activation admission reads the operator's policy for new AI activations only.


async def publish_ai_workflow(env, *, model="chosen", provider="openai-chat", name=None):
    from firefly_weave.sdk.platform_ai_setup import smoke_workflow

    document = smoke_workflow(model)
    profile = document["spec"]["llmProfiles"]["smoke"]
    profile["provider"] = provider
    document["spec"]["steps"][0]["prompt"] = {"literal": "Summarize the text. " + PROMPT_CANARY}
    if name is not None:
        document["metadata"]["name"] = name
    source = json.dumps(document)
    return await env.definitions.publish(
        env.operator, env.project, "Workflow", source, "json", "workflow-" + uuid4().hex, context=AuditContext()
    )


def activation_request(env, published, revision, release):
    from firefly_weave.contracts.agentic import TASK_TYPE
    from firefly_weave.contracts.catalog import ActivationRequest

    return ActivationRequest.model_validate_json(
        json.dumps(
            {
                "scope": env.scope.model_dump(mode="json"),
                "version_id": str(published.id),
                "artifact_digest": published.digest,
                "connection_revision_ids": {"ai": str(revision.id)},
                "worker_release_ids": {TASK_TYPE: str(release.id)},
            }
        )
    )


async def activation_rows(env):
    return [
        await scoped(env, "activation_revisions"),
        await scoped(env, "activation_connections"),
        await env.scalar("SELECT count(*) FROM access_audit WHERE action='release.activate'"),
    ]


@pytest.mark.parametrize(
    "provider,model,code",
    [
        ("openai-responses", "chosen", "WV-ACTIVATION-AI-PROVIDER"),
        ("openai-responses", "unlisted", "WV-ACTIVATION-AI-PROVIDER"),
        ("openai-chat", "unlisted", "WV-ACTIVATION-AI-MODEL"),
    ],
)
async def test_policy_refusals_insert_no_activation(ai_setup, provider, model, code):
    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    published = await publish_ai_workflow(env, model=model, provider=provider)
    with pytest.raises(CatalogError) as refused:
        await env.definitions.activate(
            env.operator,
            env.scope,
            activation_request(env, published, revision, release),
            "first",
            context=AuditContext(),
        )
    assert (refused.value.status, refused.value.code) == (422, code)
    assert await activation_rows(env) == [0, 0, 0]


async def test_without_a_policy_activation_keeps_its_existing_behavior(ai_setup):
    from firefly_weave.operations.ai_policy import APIAIPolicy

    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    published = await publish_ai_workflow(env, model="unlisted", provider="openai-responses")
    env.definitions.ai_policy = APIAIPolicy()
    activation = await env.definitions.activate(
        env.operator, env.scope, activation_request(env, published, revision, release), "absent", context=AuditContext()
    )
    assert activation.revision == 1 and await activation_rows(env) == [1, 1, 1]


async def test_a_served_policy_admits_a_model_that_is_not_installed(ai_setup):
    from firefly_weave import private_origins as po
    from firefly_weave.operations.ai_policy import APIAIPolicy

    env = ai_setup
    release = await env.release()
    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin=OLLAMA, purpose="model", networks=("10.246.27.0/24",), credentials="none")]
    )
    write_policy(env.policy_path, "served", url=OLLAMA + "/v1", credential="none")
    env.definitions.ai_policy = APIAIPolicy(env.policy_path, origins)
    with po.installed(origins):
        revision = await env.connection("ollama-local", endpoint=OLLAMA + "/v1", handle="no-credential")
        published = await publish_ai_workflow(env, model="not-installed:1b")
        activation = await env.definitions.activate(
            env.operator,
            env.scope,
            activation_request(env, published, revision, release),
            "served",
            context=AuditContext(),
        )
    assert activation.revision == 1 and await activation_rows(env) == [1, 1, 1]
    assert env.gateway_calls == []


async def test_policy_edits_apply_to_new_activations_while_committed_keys_still_replay(ai_setup):
    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    published = await publish_ai_workflow(env)
    request = activation_request(env, published, revision, release)
    committed = await env.definitions.activate(env.operator, env.scope, request, "committed", context=AuditContext())
    assert await activation_rows(env) == [1, 1, 1]
    # The policy changes without a restart: a new key is refused before any row is written.
    write_policy(env.policy_path, "other")
    with pytest.raises(CatalogError) as refused:
        await env.definitions.activate(env.operator, env.scope, request, "fresh", 1, context=AuditContext())
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-MODEL")
    assert await activation_rows(env) == [1, 1, 1]
    replayed = await env.definitions.activate(env.operator, env.scope, request, "committed", context=AuditContext())
    assert replayed.id == committed.id and await activation_rows(env) == [1, 1, 1]
    # A broken policy stops new AI activations and is read again once repaired.
    env.policy_path.write_text('{"version": 2, "endpoints": [')
    with pytest.raises(CatalogError) as unavailable:
        await env.definitions.activate(env.operator, env.scope, request, "broken", 1, context=AuditContext())
    assert (unavailable.value.status, unavailable.value.code) == (503, "WV-AI-POLICY")
    write_policy(env.policy_path, "chosen")
    second = await env.definitions.activate(env.operator, env.scope, request, "repaired", 1, context=AuditContext())
    assert second.revision == 2 and await activation_rows(env) == [2, 2, 2]


async def test_replaying_an_activation_needs_current_activation_authority(ai_setup):
    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    published = await publish_ai_workflow(env)
    identifier = await env.access.create_principal(env.admin, "application")
    binding = await env.access.grant(env.admin, identifier, Grant(role="deployer", scope=env.scope))
    deployer = await env.access.load_principal(identifier)
    request = activation_request(env, published, revision, release)
    committed = await env.definitions.activate(deployer, env.scope, request, "deployer-key", context=AuditContext())
    again = await env.definitions.activate(deployer, env.scope, request, "deployer-key", context=AuditContext())
    assert again.id == committed.id
    # The caller's token still names the role, but the current grant is gone: no stored answer is returned.
    await env.access.revoke_grant(env.admin, env.scope, binding)
    with pytest.raises(AccessDenied):
        await env.definitions.activate(deployer, env.scope, request, "deployer-key", context=AuditContext())
    assert await activation_rows(env) == [1, 1, 1]


# Audit records name each AI operation and carry no secret, prompt or provider text.


async def test_ai_audit_records_carry_no_secret_prompt_or_provider_text(ai_setup):
    from firefly_weave.contracts.ai import AIConnectionTestRequest, AIModelsQuery, AISetupGrantRequest

    env = ai_setup
    release = await env.release()
    revision = await env.connection()
    await env.setup.grant(
        env.operator, env.scope, AISetupGrantRequest(connection_revision_id=revision.id), context=AuditContext()
    )
    await env.tests.test(
        env.operator, env.scope, revision.id, AIConnectionTestRequest(model="chosen"), context=AuditContext()
    )
    await env.models.models(
        env.operator, env.scope, AIModelsQuery(connection=revision.id, refresh=True), context=AuditContext()
    )
    published = await publish_ai_workflow(env)
    await env.definitions.activate(
        env.operator,
        env.scope,
        activation_request(env, published, revision, release),
        "audited",
        context=AuditContext(),
    )
    assert [body.get("credential") for _, body in env.gateway_calls] == [SECRET_CANARY, SECRET_CANARY]
    names = await env.rows(
        "SELECT action,count(*) FROM access_audit WHERE action IN "
        "('ai.connection.test','ai.models.refresh','ai.setup.publish','ai.setup.grant') GROUP BY action ORDER BY action"
    )
    assert names == [
        ("ai.connection.test", 1),
        ("ai.models.refresh", 1),
        ("ai.setup.grant", 1),
        ("ai.setup.publish", 1),
    ]
    stored = json.dumps(await env.rows("SELECT action,target,event::text FROM access_audit"), default=str)
    stored += json.dumps(await env.rows("SELECT payload::text FROM connection_test_results"), default=str)
    for canary in (SECRET_CANARY, PROMPT_CANARY, PROVIDER_CANARY, "gateway-service-token"):
        assert canary not in stored
    details = {
        action: json.loads(event)["details"]
        for action, _, event in await env.rows(
            "SELECT action,target,event::text FROM access_audit WHERE action LIKE 'ai.%' ORDER BY action"
        )
    }
    assert details["ai.models.refresh"] == {"discovery": "ok", "models": 2}
    assert details["ai.setup.grant"] == {"not_needed": False, "pairs": 1}
