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

"""Lumi delegates one pinned connection without broad credential or resource access."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, AgenticConnectionAdapter
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfigurationRequest, LumiReply
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.lumi import LumiService

pytestmark = pytest.mark.integration


class Gateway:
    configured = True

    def __init__(self):
        self.calls = []
        self.callback = None

    async def ask(self, config, request, attachments, connection, credential):
        self.calls.append((config, request, attachments, connection, credential))
        if self.callback:
            await self.callback()
        return LumiReply(answer="Review this proposal")


@pytest.fixture
async def lumi(services, access_db, provisioned, monkeypatch):
    admin, scopes = provisioned
    scope = scopes[0]
    for role in ("developer", "lumi_manager"):
        await access_db[2].grant(
            admin, admin.id, Grant(role=role, scope=scope.model_copy(update={"environment_id": None}))
        )
    actor = await access_db[2].load_principal(admin.id)
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_TEST"),)
    )
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "private-provider-key")
    graph = services(access_db[0], registry=registry, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    connections = graph.resolve(ConnectionService)
    version = await definitions.publish(
        actor,
        scope.model_copy(update={"environment_id": None}),
        "Connector",
        json.dumps(AGENTIC_DESCRIPTOR.manifest.value),
        "json",
        "lumi-connector",
        context=AuditContext(),
    )
    revision = await connections.create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="lumi",
            connector_version_id=version.id,
            config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
            secretRef={"apiKey": "approved"},
            allowed_destinations=("https://api.openai.com",),
        ),
        context=AuditContext(),
    )
    gateway = Gateway()
    service = LumiService(
        connections,
        graph.resolve(HistoryService),
        graph.resolve(DebugService),
        gateway,
        graph.resolve(DeploymentService),
    )
    config = LumiConfigurationRequest(
        connection_revision_id=revision.id,
        profile={
            "provider": "openai-chat",
            "model": "fixture",
            "options": {"max_tokens": 1024},
            "outputSchema": LUMI_REPLY_SCHEMA,
        },
    )
    return service, actor, scope, config, gateway


async def test_configuration_cas_scope_and_reply_do_not_persist_conversation(lumi, access_db, provisioned):
    service, actor, scope, config, gateway = lumi
    saved = await service.configure(actor, scope, config, None, context=AuditContext())
    assert saved.revision == 1
    with pytest.raises(CatalogError):
        await service.configure(actor, scope, config, None, context=AuditContext())
    with pytest.raises(AccessDenied):
        await service.status(actor, provisioned[1][1], context=AuditContext())
    response = await service.ask(actor, scope, LumiAskRequest(message="private-user-prompt"), context=AuditContext())
    assert response.answer == "Review this proposal"
    assert gateway.calls[0][4] == "private-provider-key"
    async with access_db[1]() as connection:
        value = await connection.scalar(text("SELECT payload::text FROM lumi_configurations"))
        assert "private-user-prompt" not in value and "private-provider-key" not in value
        assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0


async def test_lumi_grant_does_not_grant_resource_or_credential_access(lumi, access_db, provisioned):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())
    from firefly_weave.access.service import bootstrap_identity

    identifier = await bootstrap_identity(
        access_db[1], provider_id="test", issuer="https://test.invalid", subject="lumi-user", kind="human"
    )
    await access_db[2].grant(provisioned[0], identifier, Grant(role="lumi_user", scope=scope))
    user = await access_db[2].load_principal(identifier)
    assert (await service.ask(user, scope, LumiAskRequest(message="Help"), context=AuditContext())).answer
    with pytest.raises(AccessDenied):
        await service.configuration(user, scope, context=AuditContext())
    for kind in ("draft", "run", "simulation"):
        with pytest.raises(AccessDenied):
            await service.ask(
                user,
                scope,
                LumiAskRequest(message="Explain", attachments=[{"kind": kind, "id": uuid4()}]),
                context=AuditContext(),
            )
    assert len(gateway.calls) == 1


async def test_changed_configuration_discards_inflight_reply(lumi):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())

    async def disable():
        await service.configure(actor, scope, config.model_copy(update={"enabled": False}), 1, context=AuditContext())

    gateway.callback = disable
    with pytest.raises(CatalogError):
        await service.ask(actor, scope, LumiAskRequest(message="Help"), context=AuditContext())


async def test_revoked_lumi_grant_discards_inflight_reply(lumi, access_db):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())

    async def revoke():
        async with access_db[1].begin() as session:
            await session.execute(
                text("DELETE FROM role_bindings WHERE principal_id=:id AND role='lumi_manager'"), {"id": actor.id}
            )

    gateway.callback = revoke
    with pytest.raises(AccessDenied):
        await service.ask(actor, scope, LumiAskRequest(message="Help"), context=AuditContext())


async def test_native_lumi_routes_require_roles_and_do_not_cache(client, headers, env_url):
    response = await client.get(env_url + "/lumi/status", headers=headers)
    assert response.status_code == 403
    denied = await client.post(env_url + "/lumi/ask", headers=headers, json={"message": "Help"})
    assert denied.status_code == 403


async def test_native_lumi_ask_is_private_and_uses_only_admin_configuration(
    lumi, client, headers, env_url, access_db, provisioned
):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())
    async with access_db[0].begin() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], identifier, Grant(role="lumi_user", scope=scope))
    bound = client._transport.app.state.pyfly.context.get_bean(LumiService)
    bound.gateway = gateway
    bound.connections.secrets = service.connections.secrets
    status = await client.get(env_url + "/lumi/status", headers=headers)
    assert status.status_code == 200 and status.json()["configured"]
    assert "connection_revision_id" not in status.text
    response = await client.post(env_url + "/lumi/ask", headers=headers, json={"message": "private-http-prompt"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["answer"] == "Review this proposal"
    override = await client.post(
        env_url + "/lumi/ask",
        headers=headers,
        json={"message": "Help", "profile": config.profile.model_dump(by_alias=True)},
    )
    assert override.status_code == 422
    assert len(gateway.calls) == 1


async def test_unsaved_inline_source_does_not_fetch_uris_or_require_catalog_grants(lumi, monkeypatch):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())

    async def forbidden_read(*args, **kwargs):
        pytest.fail("Inline text was treated as a saved resource")

    monkeypatch.setattr(service.definitions, "read", forbidden_read)
    source = "steps: [incomplete\nurl: https://private.invalid/secret"
    response = await service.ask(
        actor,
        scope,
        LumiAskRequest(message="Fix this", draft={"format": "yaml", "source": source}),
        context=AuditContext(),
    )
    assert response.answer
    assert gateway.calls[0][2]["resources"] == [{"kind": "inline-source", "format": "yaml", "source": source}]


async def test_inline_and_saved_context_share_one_byte_budget(lumi, monkeypatch):
    service, actor, scope, config, gateway = lumi
    await service.configure(actor, scope, config, None, context=AuditContext())

    async def authorized_draft(*args, **kwargs):
        return {"document": {"value": "x" * 200000}, "revision": 1}

    monkeypatch.setattr(service.definitions, "read", authorized_draft)
    request = LumiAskRequest(
        message="Help", draft={"format": "yaml", "source": "x" * 70000}, attachments=[{"kind": "draft", "id": uuid4()}]
    )
    with pytest.raises(CatalogError) as failure:
        await service.ask(actor, scope, request, context=AuditContext())
    assert failure.value.status == 413 and not gateway.calls


@pytest.mark.parametrize("revoke", [False, True])
async def test_operations_attachment_requires_target_read_and_rechecks_it(lumi, access_db, provisioned, revoke):
    from firefly_weave.contracts.deployments import TargetRequest

    service, actor, scope, config, gateway = lumi
    admin = provisioned[0]
    await access_db[2].grant(admin, actor.id, Grant(role="deployment_planner", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    runner = await access_db[2].create_principal(admin, "application")
    target = await service.deployments.create_target(
        TargetRequest(
            name="lumi-target",
            adapter="docker-compose",
            external_identity="private-provider-path",
            boundary="private-boundary",
            runner_principal_id=runner,
            capabilities=["observe"],
        ),
        str(uuid4()),
        actor=actor,
        scope=scope,
        context=AuditContext(),
    )
    await service.configure(actor, scope, config, None, context=AuditContext())
    user_id = await access_db[2].create_principal(admin, "application")
    await access_db[2].grant(admin, user_id, Grant(role="lumi_user", scope=scope))
    user = await access_db[2].load_principal(user_id)
    request = LumiAskRequest(
        message="Explain this saved target", attachments=[{"kind": "deployment-target", "id": target.id}]
    )
    with pytest.raises(AccessDenied):
        await service.ask(user, scope, request, context=AuditContext())
    assert not gateway.calls
    await access_db[2].grant(admin, user_id, Grant(role="deployment_reader", scope=scope, resources=(str(target.id),)))
    user = await access_db[2].load_principal(user_id)
    if revoke:

        async def remove_reader():
            async with access_db[1].begin() as session:
                await session.execute(
                    text("DELETE FROM role_bindings WHERE principal_id=:id AND role='deployment_reader'"),
                    {"id": user_id},
                )

        gateway.callback = remove_reader
        with pytest.raises(AccessDenied):
            await service.ask(user, scope, request, context=AuditContext())
    else:
        result = await service.ask(user, scope, request, context=AuditContext())
        assert result.answer and result.proposals == []
    assert len(gateway.calls) == 1
    context = gateway.calls[0][2]
    assert context["resources"][0]["data"] == {
        "name": "lumi-target",
        "adapter": "docker-compose",
        "revision": 1,
        "disabled": False,
        "capabilities": ["observe"],
    }
    assert "private-" not in json.dumps(context)
