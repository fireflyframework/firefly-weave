# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Teams transactional reference gates; requires task-owned PostgreSQL for backend cases."""

import asyncio
import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.teams import TeamsReactivateRequest
from firefly_weave.providers.teams.references import TeamsReferences


@pytest.fixture
async def teams(worker_setup, access_db, provisioned, monkeypatch, signed_activity, scheduler_url):
    import httpx
    from pyfly.client.ports.outbound import BoundedHttpClientPort

    from firefly_weave.access.models import Grant
    from firefly_weave.app import make_app
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.teams import TeamsConnector, package
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.providers import ProviderSourceRequest, provider_schema_digest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.providers.service import ProviderIngressService
    from firefly_weave.providers.teams.bridge import TeamsVerifier
    from firefly_weave.settings import Settings

    _, _, _, actor, scope, activation, _ = worker_setup
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    data, activity, token, jwks = signed_activity
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_E3_TEST", "e3-fixture-only")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()},
        (SecretGrant(scope, "e3-secret", "env", "WEAVE_CONNECTION_SECRET_E3_TEST"),),
    )
    settings = Settings(
        scheduler_enabled=False,
        scheduler_database_url=scheduler_url,
        connector_packages=("firefly-weave:weave-teams:firefly_weave.connectors.teams:package",),
        database_url=access_db[3].render_as_string(hide_password=False),
    )
    app = make_app(settings, secrets=secrets)

    class Wire:
        calls = []

        async def request_bounded(self, method, url, **kwargs):
            self.calls.append((method, url))
            assert method == "GET" and url == "https://login.botframework.com/v1/.well-known/keys"
            return httpx.Response(200, content=jwks)

    wire = Wire()
    context = app.state.pyfly.context
    context.container.register_instance(BoundedHttpClientPort, wire)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://local") as client,
    ):
        assert app.state.compatibility.ready
        definitions = context.get_bean(DefinitionService)
        registry = context.get_bean(
            __import__("firefly_weave.connections.registry", fromlist=["ConnectorRegistry"]).ConnectorRegistry
        )
        assert registry.get("weave-teams") is context.get_bean(TeamsConnector)
        version = await definitions.publish(
            actor,
            scope,
            "Connector",
            json.dumps(package.metadata.model.manifest.model_dump(by_alias=True)),
            "json",
            "teams-fixture",
            context=AuditContext(),
        )
        service = context.get_bean(ProviderIngressService)

        async def create(generation=1):
            policy = {**data, "installation_generation": generation}
            connection = await context.get_bean(ConnectionService).create_revision(
                actor,
                scope,
                ConnectionRequest(
                    name="teams",
                    connector_version_id=version.id,
                    config=policy,
                    secretRef={"client_secret": "e3-secret"},
                    allowed_destinations=("https://login.microsoftonline.com", "https://smba.trafficmanager.net"),
                ),
                context=AuditContext(),
            )
            request = ProviderSourceRequest(
                name="teams",
                provider="teams",
                package="firefly-weave",
                package_version="0.1.0a1",
                adapter_version="1.0.0",
                schema_digest=provider_schema_digest(
                    package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds
                ),
                connection_revision_id=connection.id,
                policy=policy,
                kind="run",
                activation_id=activation.id,
            )
            return await service.create(actor, scope, request, context=AuditContext())

        source = await create()
        assert service.verifier(source) is context.get_bean(TeamsVerifier)
        assert registry.builtin_verifier("weave-teams") is service.verifier(source)
        assert registry.builtin_verifier("weave-teams").references is context.get_bean(TeamsReferences)

        async def deliver(changes=None, selected=None):
            return await client.post(
                f"/provider-ingress/{(selected or source).id}",
                content=json.dumps({**activity, **(changes or {})}).encode(),
                headers={"authorization": "Bearer " + token()},
            )

        yield dict(
            app=app,
            client=client,
            context=context,
            service=service,
            source=source,
            scope=scope,
            actor=actor,
            create=create,
            deliver=deliver,
            wire=wire,
            activity=activity,
        )


async def items(teams, access_db, table):
    from firefly_weave.persistence.uow import UnitOfWork

    async with UnitOfWork(access_db[0]).open(teams["scope"]) as tx:
        return list((await tx.session.execute(text(f"SELECT * FROM {table}"))).mappings())


@pytest.mark.integration
async def test_native_teams_admits_reference_before_dispatch_without_token(teams, access_db):
    results = await asyncio.gather(teams["deliver"](), teams["deliver"]())
    assert [r.status_code for r in results] == [202, 202], [r.text for r in results]
    receipts = await items(teams, access_db, "provider_receipts")
    refs = await items(teams, access_db, "teams_references")
    assert len(receipts) == len(refs) == 1 and receipts[0]["state"] == "pending"
    assert refs[0]["state"] == "active"
    assert len(teams["wire"].calls) == 1


@pytest.mark.integration
async def test_removal_late_message_explicit_reactivation_and_cross_source_duplicate(teams, access_db):
    assert (await teams["deliver"]()).status_code == 202
    removal = {"type": "installationUpdate", "action": "remove", "id": "removed"}
    assert (await teams["deliver"](removal)).status_code == 202
    assert (await teams["deliver"](removal)).status_code == 202
    assert (await teams["deliver"]({"id": "late"})).status_code == 409
    assert (
        await teams["deliver"]({"type": "installationUpdate", "action": "add", "id": "late-add"})
    ).status_code == 409
    refs = teams["context"].get_bean(TeamsReferences)
    row = (await items(teams, access_db, "teams_references"))[0]
    identifier = row["id"]
    next_source = await teams["create"](2)
    assert (await teams["deliver"]({"id": "premature"}, next_source)).status_code == 409
    command = TeamsReactivateRequest(expected_generation=1, source_id=next_source.id, request_id=uuid4())
    result = await refs.change(teams["actor"], teams["scope"], identifier, command, context=AuditContext())
    assert result.state == "active" and result.generation == 2
    assert await refs.change(teams["actor"], teams["scope"], identifier, command, context=AuditContext()) == result
    assert (await teams["deliver"](removal, next_source)).status_code == 202
    assert (await refs.read(teams["actor"], teams["scope"], identifier, context=AuditContext())).state == "active"
    assert (await teams["deliver"]({"id": "old-generation"})).status_code == 409
    assert (await teams["deliver"]({"id": "new-generation"}, next_source)).status_code == 202
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises(CatalogError):
        await refs.resolve(teams["scope"], teams["source"].connection_revision_id, identifier, 1, None)


@pytest.mark.integration
async def test_teams_commit_failure_has_no_ack_or_reference(teams, access_db):
    def fail(session):
        raise RuntimeError("e3 fixture commit denial")

    event.listen(AsyncSession.sync_session_class, "before_commit", fail)
    try:
        response = await teams["deliver"]()
        assert response.status_code >= 500
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", fail)
    assert await items(teams, access_db, "teams_references") == []
    assert await items(teams, access_db, "provider_receipts") == []


def test_teams_reference_migration_is_forward_only():
    from importlib.resources import files

    from alembic.script import ScriptDirectory

    from firefly_weave.persistence.migrations import SCHEMA_VERSION

    scripts = ScriptDirectory(str(files("firefly_weave.persistence").joinpath("alembic")))
    assert scripts.get_current_head() == SCHEMA_VERSION
    teams_revision = scripts.get_revision("0019_teams_references")
    assert teams_revision is not None
    assert teams_revision.down_revision == "0018_provider_inbox"
    assert teams_revision.revision in {revision.revision for revision in scripts.walk_revisions()}


@pytest.mark.integration
async def test_disabled_old_source_can_be_revoked_then_explicitly_reactivated(teams, access_db, provisioned):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.teams import TeamsRevokeRequest

    assert (await teams["deliver"]()).status_code == 202
    refs = teams["context"].get_bean(TeamsReferences)
    identifier = (await items(teams, access_db, "teams_references"))[0]["id"]
    await teams["service"].disable(teams["actor"], teams["scope"], teams["source"].id, context=AuditContext())
    unprivileged_id = await access_db[2].create_principal(provisioned[0], "application")
    unprivileged = await access_db[2].load_principal(unprivileged_id)
    with pytest.raises(AccessDenied):
        await refs.change(
            unprivileged, teams["scope"], identifier, TeamsRevokeRequest(expected_generation=1), context=AuditContext()
        )
    revoked = await refs.change(
        teams["actor"], teams["scope"], identifier, TeamsRevokeRequest(expected_generation=1), context=AuditContext()
    )
    assert revoked.state == "revoked"
    source = await teams["create"](2)
    result = await refs.change(
        teams["actor"],
        teams["scope"],
        identifier,
        TeamsReactivateRequest(expected_generation=1, source_id=source.id, request_id=uuid4()),
        context=AuditContext(),
    )
    assert result.generation == 2 and result.state == "active"


@pytest.mark.integration
async def test_message_to_native_lease_reply_and_revocation_during_token(teams, access_db, monkeypatch):
    import httpx

    from firefly_weave.connections.repository import ConnectionRepository
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.connectors.teams import package
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.contracts.providers import ProviderSourceRequest
    from firefly_weave.contracts.teams import TeamsRevokeRequest
    from firefly_weave.contracts.workers import CredentialGrantRequest, InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.providers.dispatcher import ProviderDispatcher
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.sdk.worker import Worker
    from firefly_weave.workers.service import WorkerService

    context = teams["context"]
    actor = teams["actor"]
    scope = teams["scope"]
    definitions = context.get_bean(DefinitionService)
    workers = context.get_bean(WorkerService)
    descriptor = package.descriptor
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "e" * 64,
            capabilities=list(descriptor.capabilities),
            connector_bindings=list(descriptor.bindings),
            credential_capabilities=[b.task_reference for b in descriptor.bindings],
        ),
        context=AuditContext(),
    )
    async with UnitOfWork(access_db[0]).open(scope) as tx:
        connection = await ConnectionRepository(tx).revision(teams["source"].connection_revision_id)
    action_contract = package.metadata.model.manifest.spec.actions["reply"]
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "teams-reply", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "weave-teams@1.0.0", "action": "reply", "config": {}},
            "connection": {"connector": "weave-teams@1.0.0"},
            "inputSchema": action_contract.input_schema,
            "outputSchema": action_contract.output_schema,
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 30,
        },
    }
    await definitions.publish(
        actor, scope, "Action", json.dumps(action), "json", "reply-action", context=AuditContext()
    )
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[3] / "examples/teams/reply.workflow.yaml").read_text())
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(workflow), "json", "reply-flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            version_id=published.id,
            artifact_digest=published.digest,
            scope=scope,
            connection_revision_ids={"teams": connection.id},
            connector_release_ids={connection.connector_version_id: release.id},
        ),
        "reply-active",
        context=AuditContext(),
    )
    binding = next(b for b in descriptor.bindings if b.action == "reply")
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=[binding.task_reference], capacity=1),
        context=AuditContext(),
    )
    await workers.grant_connection(
        actor,
        scope,
        CredentialGrantRequest(
            release_id=release.id, connection_revision_id=connection.id, capability=binding.task_reference
        ),
        context=AuditContext(),
    )
    request = ProviderSourceRequest(
        **{key: getattr(teams["source"], key) for key in ProviderSourceRequest.model_fields}
    ).model_copy(update={"name": "reply-source", "activation_id": activation.id})
    source = await teams["service"].create(actor, scope, request, context=AuditContext())
    assert (await teams["deliver"](selected=source)).status_code == 202
    receipt = (await items(teams, access_db, "provider_receipts"))[0]
    dispatched = await context.get_bean(ProviderDispatcher).dispatch_one(scope, receipt["id"])
    executor = context.get_bean(ConnectorExecutionService)
    transport = ServiceTransport(executor, scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]
    sends = []
    tokens = []
    revoke = False
    references = context.get_bean(TeamsReferences)

    async def token_response(request):
        tokens.append(request)
        assert b"e3-fixture-only" in request.content
        if revoke:
            identifier = (await items(teams, access_db, "teams_references"))[0]["id"]
            await references.change(
                actor, scope, identifier, TeamsRevokeRequest(expected_generation=1), context=AuditContext()
            )
        return httpx.Response(
            200, json={"access_token": "native-fixture-bearer", "token_type": "Bearer", "expires_in": 3600}
        )

    monkeypatch.setattr(
        "firefly_weave.connections.machine_tokens.PinnedTransport", lambda *a, **k: httpx.MockTransport(token_response)
    )
    original = teams["wire"].request_bounded

    async def send(method, url, **kwargs):
        if method == "GET":
            return await original(method, url, **kwargs)
        sends.append((url, json.loads(kwargs["content"])))
        return httpx.Response(200, json={"id": "accepted-native"})

    monkeypatch.setattr(teams["wire"], "request_bounded", send)

    failures = []

    async def execute(selected):
        try:
            return await executor.execute(scope, actor.id, selected)
        except Exception as error:
            failures.append((type(error).__name__, getattr(error, "code", None)))
            raise

    await Worker(transport, {lease.capability: execute}, 1)._execute(lease)
    assert not failures, (failures, len(tokens), len(sends))
    view = await context.get_bean(RuntimeService).read(actor, scope, dispatched.run_id, context=AuditContext())
    assert view.state.output == {"id": "accepted-native", "status": "accepted"}
    assert len(tokens) == len(sends) == 1
    assert sends[0][0].endswith("/emea/v3/conversations/conv%2F1/activities/activity-1")
    assert (await teams["deliver"]({"id": "activity-2"}, source)).status_code == 202
    receipt = next(r for r in await items(teams, access_db, "provider_receipts") if r["event_id"] == "activity-2")
    await context.get_bean(ProviderDispatcher).dispatch_one(scope, receipt["id"])
    lease = (await transport.claim(1))[0]
    revoke = True
    with pytest.raises(ConnectorFailure) as error:
        await executor.execute(scope, actor.id, lease)
    assert error.value.outcome == "not_started" and len(tokens) == 2 and len(sends) == 1
    assert "native-fixture-bearer" not in lease.model_dump_json()


@pytest.mark.integration
async def test_native_reference_sdk_routes_and_scoped_authority(teams, access_db, monkeypatch):
    from firefly_weave.access.authentication import AuthenticationService
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.teams import TeamsRevokeRequest
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError

    async def fixture_authentication(value):
        assert value == "teams-route-fixture"
        return teams["actor"]

    monkeypatch.setattr(teams["context"].get_bean(AuthenticationService), "authenticate", fixture_authentication)
    assert (await teams["deliver"]()).status_code == 202
    identifier = (await items(teams, access_db, "teams_references"))[0]["id"]
    async with WeaveClient(
        "http://localhost", lambda: "teams-route-fixture", teams["scope"], transport=ASGITransport(teams["app"])
    ) as sdk:
        reference = await sdk.read_teams_reference(identifier)
        assert (await sdk.list_teams_references(limit=1)).items == [reference]
        assert (
            await sdk.revoke_teams_reference(identifier, TeamsRevokeRequest(expected_generation=1))
        ).state == "revoked"
        source = await teams["create"](2)
        request = TeamsReactivateRequest(expected_generation=1, source_id=source.id, request_id=uuid4())
        active = await sdk.reactivate_teams_reference(identifier, request)
        assert active.generation == 2 and active.state == "active"
        assert await sdk.reactivate_teams_reference(identifier, request) == active
        with pytest.raises(WeaveError):
            await sdk.reactivate_teams_reference(identifier, request.model_copy(update={"request_id": uuid4()}))
    async with WeaveClient(
        "http://localhost",
        lambda: "teams-route-fixture",
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        transport=ASGITransport(teams["app"]),
    ) as sdk:
        with pytest.raises(WeaveError):
            await sdk.read_teams_reference(identifier)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["reactivate", "replay", "resolve"])
async def test_reference_authority_rechecks_retired_target(teams, access_db, operation):
    from firefly_weave.contracts.teams import TeamsRevokeRequest
    from firefly_weave.definitions.models import CatalogError

    assert (await teams["deliver"]()).status_code == 202
    refs = teams["context"].get_bean(TeamsReferences)
    identifier = (await items(teams, access_db, "teams_references"))[0]["id"]
    await refs.change(
        teams["actor"], teams["scope"], identifier, TeamsRevokeRequest(expected_generation=1), context=AuditContext()
    )
    source = await teams["create"](2)
    request = TeamsReactivateRequest(expected_generation=1, source_id=source.id, request_id=uuid4())
    if operation != "reactivate":
        await refs.change(teams["actor"], teams["scope"], identifier, request, context=AuditContext())
    before = await refs.read(teams["actor"], teams["scope"], identifier, context=AuditContext())
    await teams["service"].runtime.definitions.retire_activation(
        teams["actor"], teams["scope"], source.activation_id, str(uuid4()), 1, context=AuditContext()
    )
    with pytest.raises(CatalogError):
        if operation == "resolve":
            await refs.resolve(teams["scope"], source.connection_revision_id, identifier, 2, None)
        else:
            await refs.change(teams["actor"], teams["scope"], identifier, request, context=AuditContext())
    assert await refs.read(teams["actor"], teams["scope"], identifier, context=AuditContext()) == before


@pytest.mark.integration
async def test_reactivation_and_ingress_share_outer_project_fence(teams, access_db, monkeypatch):
    from firefly_weave.contracts.teams import TeamsRevokeRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.providers.repository import ProviderRepository

    assert (await teams["deliver"]()).status_code == 202
    refs = teams["context"].get_bean(TeamsReferences)
    identifier = (await items(teams, access_db, "teams_references"))[0]["id"]
    await refs.change(
        teams["actor"], teams["scope"], identifier, TeamsRevokeRequest(expected_generation=1), context=AuditContext()
    )
    source = await teams["create"](2)
    request = TeamsReactivateRequest(expected_generation=1, source_id=source.id, request_id=uuid4())
    source_held, admin_ready, resume_ingress = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ProviderRepository.source
    admin = None

    async def controlled_source(repository, selected, *, lock=False):
        administrative = asyncio.current_task() is admin
        if selected == source.id and administrative:
            admin_ready.set()
        result = await original(repository, selected, lock=lock)
        if selected == source.id and lock and not administrative:
            source_held.set()
            async with asyncio.timeout(5):
                await resume_ingress.wait()
        return result

    monkeypatch.setattr(ProviderRepository, "source", controlled_source)
    incoming = asyncio.create_task(teams["deliver"]({"id": "during-reactivation"}, source))
    try:
        async with asyncio.timeout(8):
            await source_held.wait()
            admin = asyncio.create_task(
                refs.change(teams["actor"], teams["scope"], identifier, request, context=AuditContext())
            )
            with pytest.raises(CatalogError) as denied:
                await admin
            assert denied.value.code == "WV-OPERATION-CAPACITY"
            assert not admin_ready.is_set()
            resume_ingress.set()
            response = await incoming
            assert response.status_code == 409
            before = await refs.read(teams["actor"], teams["scope"], identifier, context=AuditContext())
            assert before.generation == 1 and before.state == "revoked"
            active = await refs.change(teams["actor"], teams["scope"], identifier, request, context=AuditContext())
            assert active.generation == 2 and active.state == "active"
    finally:
        resume_ingress.set()
        for task in (incoming, admin):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (incoming, admin) if task is not None), return_exceptions=True)
    assert (await teams["deliver"]({"id": "after-reactivation"}, source)).status_code == 202
