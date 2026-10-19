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
"""Native two-lifespan provider ingress with real scoped PostgreSQL transactions."""

import asyncio
import hashlib
import hmac
import json
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.definitions import RefExpression
from firefly_weave.contracts.providers import ProviderSourceRequest, provider_schema_digest
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.providers.service import ProviderIngressService

pytestmark = pytest.mark.integration
IDENTITY = "e2-inbox-fixture:e2-inbox-fixture:e2_inbox_fixture:package"


def envelope(*events, account="installation"):
    raw = json.dumps({"account": account, "events": events}).encode()
    return raw, {"x-fixture-signature": hmac.new(b"e2-test-only-secret", raw, hashlib.sha256).hexdigest()}


def message(identity="event-1", value=3, **changes):
    return {"event_id": identity, "kind": "message", "payload": {"value": value}, **changes}


@pytest.fixture
async def inbox(worker_setup, access_db, provisioned, monkeypatch, scheduler_url):
    from e2_inbox_fixture import FixtureVerifier, package

    from firefly_weave.access.models import Grant
    from firefly_weave.app import make_app
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.settings import Settings

    _, _, _, actor, scope, activation, _ = worker_setup
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    async with access_db[1]() as session, session.begin():
        await session.execute(text("CREATE TABLE provider_fixture_effects(source_id uuid,event_id text)"))
        await session.execute(text("GRANT SELECT,INSERT ON provider_fixture_effects TO weave_app"))
    settings = Settings(
        scheduler_enabled=False,
        scheduler_database_url=scheduler_url,
        connector_packages=(IDENTITY,),
        database_url=access_db[3].render_as_string(hide_password=False),
    )
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant

    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_E2", "e2-test-only-secret")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "e2-signature", "env", "WEAVE_CONNECTION_SECRET_E2"),)
    )
    apps = [make_app(settings, secrets=secrets), make_app(settings, secrets=secrets)]
    async with AsyncExitStack() as stack:
        clients = []
        for app in apps:
            await stack.enter_async_context(app.router.lifespan_context(app))
            clients.append(
                await stack.enter_async_context(
                    AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://local")
                )
            )
        context = apps[0].state.pyfly.context
        definitions = context.get_bean(DefinitionService)
        version = await definitions.publish(
            actor,
            scope,
            "Connector",
            json.dumps(package.metadata.model.manifest.model_dump(by_alias=True)),
            "json",
            "provider-fixture",
            context=AuditContext(),
        )
        connection = await context.get_bean(ConnectionService).create_revision(
            actor,
            scope,
            ConnectionRequest(
                name="provider",
                connector_version_id=version.id,
                config={"account_id": "installation"},
                secretRef={"signature": "e2-signature"},
            ),
            context=AuditContext(),
        )
        service = context.get_bean(ProviderIngressService)
        request = ProviderSourceRequest(
            name="inbox",
            provider="fixture",
            package="e2-inbox-fixture",
            package_version="1.0.0",
            schema_digest=provider_schema_digest(
                package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds
            ),
            connection_revision_id=connection.id,
            policy={"account_id": "installation"},
            kind="run",
            activation_id=activation.id,
            mapping=RefExpression(ref="/payload/value"),
        )
        source = await service.create(actor, scope, request, context=AuditContext())
        yield dict(
            settings=settings,
            apps=apps,
            clients=clients,
            service=service,
            source=source,
            scope=scope,
            actor=actor,
            verifier=context.get_bean(FixtureVerifier),
            dispatchers=[app.state.pyfly.context.get_bean(ProviderDispatcher) for app in apps],
        )


async def deliver(inbox, *events, client=0, account="installation"):
    raw, headers = envelope(*events, account=account)
    return await inbox["clients"][client].post(f"/provider-ingress/{inbox['source'].id}", content=raw, headers=headers)


async def rows(inbox, access_db, table="provider_receipts"):
    from firefly_weave.persistence.uow import UnitOfWork

    async with UnitOfWork(access_db[0]).open(inbox["scope"]) as tx:
        return list((await tx.session.execute(text(f"SELECT * FROM {table}"))).mappings())


async def test_duplicate_dispatch_creates_one_run(inbox, access_db):
    a, b = await asyncio.gather(deliver(inbox, message()), deliver(inbox, message(), client=1))
    assert a.status_code == b.status_code == 200
    receipts = await rows(inbox, access_db)
    assert len(receipts) == 1
    results = await asyncio.gather(*(d.dispatch_one(inbox["scope"], receipts[0]["id"]) for d in inbox["dispatchers"]))
    assert results[0] == results[1]
    assert results[0].state == "dispatched"
    assert len(await rows(inbox, access_db, "runs")) == 1
    assert [row["origin"] for row in await rows(inbox, access_db, "run_facts")] == ["provider"]
    assert len(await rows(inbox, access_db, "provider_fixture_effects")) == 1


async def test_failed_admission_never_acknowledges(inbox, access_db):
    def fail(session):
        if session.info.get("e2_fail_commit"):
            raise RuntimeError("fixture commit failure")

    event.listen(AsyncSession.sync_session_class, "before_commit", fail)
    try:
        inbox["verifier"].fail_commit = True
        response = await deliver(inbox, message())
        assert response.status_code >= 500
        assert await rows(inbox, access_db) == []
        assert await rows(inbox, access_db, "provider_fixture_effects") == []
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", fail)


async def test_mixed_duplicates_hook_once_and_conflict_rolls_back_whole_batch(inbox, access_db):
    assert (await deliver(inbox, message("old"))).status_code == 200
    assert (await deliver(inbox, message("old"), message("new"))).status_code == 200
    assert (await deliver(inbox, message("newer"), message("old", 4))).status_code == 409
    assert len(await rows(inbox, access_db)) == 2
    assert len(await rows(inbox, access_db, "provider_fixture_effects")) == 2
    inbox["verifier"].fail_hook = True
    assert (await deliver(inbox, message("hook-fail"))).status_code >= 500
    assert len(await rows(inbox, access_db)) == 2
    assert len(await rows(inbox, access_db, "provider_fixture_effects")) == 2


async def test_authenticated_ignored_lifecycle_and_secret_admission(inbox, access_db):
    response = await deliver(inbox, message(kind="lifecycle", disposition="ignore", reason="lifecycle"))
    assert response.status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "ignored" and result.attempts == 0
    assert await rows(inbox, access_db, "runs") == []
    assert (await deliver(inbox, message("secret", kind="secret"))).status_code == 422
    assert "value" not in result.model_dump_json()


async def test_invalid_auth_installation_headers_batch_and_body(inbox, access_db):
    path = f"/provider-ingress/{inbox['source'].id}"
    client = inbox["clients"][0]
    response = await client.post(path, content=b"not-json", headers={"x-fixture-signature": "invalid"})
    assert response.status_code == 401
    assert (await deliver(inbox, message(), account="wrong")).status_code == 401
    raw, headers = envelope(message())
    response = await client.post(path, content=raw, headers=[*headers.items(), *headers.items()])
    assert response.status_code == 401
    assert (await deliver(inbox, *(message(str(i)) for i in range(101)))).status_code == 413
    response = await client.post(path, content=b" " * 1048577)
    assert response.status_code == 413
    assert await rows(inbox, access_db) == []


async def test_disable_between_ack_and_dispatch_blocks_receipt(inbox, access_db):
    assert (await deliver(inbox, message())).status_code == 200
    ready, resume = asyncio.Event(), asyncio.Event()
    dispatcher = inbox["dispatchers"][1]
    checked = dispatcher.ingress.checked

    async def barrier(tx, source):
        ready.set()
        await resume.wait()
        return await checked(tx, source)

    dispatcher.ingress.checked = barrier
    receipt = (await rows(inbox, access_db))[0]
    from firefly_weave.definitions.models import CatalogError

    # The outer project fence serializes revocation behind an admitted dispatch.
    task = asyncio.create_task(dispatcher.dispatch_one(inbox["scope"], receipt["id"]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        with pytest.raises(CatalogError) as denied:
            await inbox["service"].bindings.revoke(
                inbox["actor"], inbox["scope"], inbox["source"].binding_id, context=AuditContext()
            )
        assert denied.value.code == "WV-OPERATION-CAPACITY"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        dispatcher.ingress.checked = checked
    assert (await rows(inbox, access_db))[0]["state"] == "pending"
    assert await rows(inbox, access_db, "runs") == []
    await inbox["service"].bindings.revoke(
        inbox["actor"], inbox["scope"], inbox["source"].binding_id, context=AuditContext()
    )
    result = await dispatcher.dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "blocked"
    assert await rows(inbox, access_db, "runs") == []


async def test_rls_requires_full_scope_and_does_not_bleed_pool(inbox, access_db):
    from firefly_weave.persistence.uow import UnitOfWork

    assert (await deliver(inbox, message())).status_code == 200
    uow = UnitOfWork(access_db[0])
    for scope in (
        inbox["scope"].model_copy(update={"environment_id": None}),
        inbox["scope"].model_copy(update={"project_id": uuid4()}),
        inbox["scope"].model_copy(update={"tenant_id": uuid4()}),
    ):
        async with uow.open(scope) as tx:
            assert await tx.session.scalar(text("SELECT count(*) FROM provider_receipts")) == 0
    assert len(await rows(inbox, access_db)) == 1


async def test_challenge_never_creates_receipts(inbox, access_db):
    path = f"/provider-ingress/{inbox['source'].id}"
    client = inbox["clients"][0]
    assert (await client.get(path, params={"token": "wrong"})).status_code == 401
    response = await client.get(path, params={"token": "fixture-challenge", "challenge": "answer"})
    assert response.status_code == 200 and response.text == "answer"
    assert await rows(inbox, access_db) == []


async def test_disabled_source_is_visible_and_retry_cannot_restore_authority(inbox, access_db):
    from firefly_weave.definitions.models import CatalogError

    assert (await deliver(inbox, message())).status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    await inbox["service"].disable(inbox["actor"], inbox["scope"], inbox["source"].id, context=AuditContext())
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "blocked"
    with pytest.raises(CatalogError):
        await inbox["dispatchers"][0].retry(inbox["actor"], inbox["scope"], receipt["id"], context=AuditContext())
    assert await rows(inbox, access_db, "runs") == []


async def test_missing_package_blocks_and_explicit_retry_is_idempotent(inbox, access_db, monkeypatch):
    from firefly_weave.definitions.models import CatalogError

    assert (await deliver(inbox, message())).status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    registry = inbox["service"].registry

    def unavailable(*args):
        raise CatalogError(422, "WV-CONNECTION", "Unavailable")

    with monkeypatch.context() as change:
        change.setattr(registry, "provider_verifier", unavailable)
        result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
        assert result.state == "blocked"
    first = await inbox["dispatchers"][0].retry(inbox["actor"], inbox["scope"], receipt["id"], context=AuditContext())
    second = await inbox["dispatchers"][0].retry(inbox["actor"], inbox["scope"], receipt["id"], context=AuditContext())
    assert first == second and first.state == "pending"
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "dispatched" and result.attempts == 2
    assert len(await rows(inbox, access_db, "runs")) == 1


async def test_revoked_owner_after_ack_never_dispatches(inbox, access_db):
    assert (await deliver(inbox, message())).status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    async with access_db[1]() as session, session.begin():
        await session.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": inbox["actor"].id})
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "blocked"
    assert await rows(inbox, access_db, "runs") == []


async def test_source_pins_owner_package_schema_and_exact_target(inbox):
    from firefly_weave.definitions.models import CatalogError

    source = inbox["source"]
    request = ProviderSourceRequest(**{key: getattr(source, key) for key in ProviderSourceRequest.model_fields})
    assert source.principal_id == inbox["actor"].id
    for changes in (
        {"package_version": "9.9.9"},
        {"schema_digest": "0" * 64},
        {"activation_id": uuid4()},
        {"connection_revision_id": uuid4()},
    ):
        with pytest.raises(CatalogError):
            await inbox["service"].create(
                inbox["actor"], inbox["scope"], request.model_copy(update=changes), context=AuditContext()
            )


async def test_native_sdk_pages_receipts_and_permissions(
    inbox, access_db, authenticated_client, headers, other_headers, env_url
):
    from firefly_weave.app import make_app
    from firefly_weave.sdk.client import WeaveClient

    assert (await deliver(inbox, message("one"), message("two"))).status_code == 200
    auth_app = authenticated_client[0]._transport.app
    app = make_app(
        inbox["settings"],
        secrets=inbox["service"].bindings.connections.secrets,
        verifiers=auth_app.state.restart_verifiers,
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
    ):
        assert app.state.compatibility.ready
        async with WeaveClient(
            "http://localhost",
            lambda: headers["Authorization"].removeprefix("Bearer "),
            inbox["scope"],
            transport=ASGITransport(app),
        ) as sdk:
            source = await sdk.read_provider_source(inbox["source"].id)
            assert source.activation_id == inbox["source"].activation_id
            page = await sdk.list_provider_receipts(limit=1)
            assert len(page.items) == 1 and page.next_cursor
            second = await sdk.list_provider_receipts(limit=1, cursor=page.next_cursor)
            assert second.items[0].id != page.items[0].id
            assert await sdk.read_provider_receipt(page.items[0].id) == page.items[0]
        receipt = page.items[0].id
        denied = await client.post("/api/v1" + env_url + f"/provider-receipts/{receipt}/retry", headers=headers)
        assert denied.status_code == 403
        denied = await client.get("/api/v1" + env_url + f"/provider-receipts/{receipt}", headers=other_headers)
        assert denied.status_code == 403


async def signal_source(inbox):
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    runtime = inbox["service"].runtime
    source = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: e2-signal, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {type: integer}
  timeoutSeconds: 600
  steps:
    - id: incoming
      kind: signal
      name: incoming
      timeoutSeconds: 300
      payloadSchema: {type: integer}
  output: {ref: /steps/incoming/output}
"""
    version = await runtime.definitions.publish(
        inbox["actor"], inbox["scope"], "Workflow", source, "yaml", "e2-signal", context=AuditContext()
    )
    activation = await runtime.definitions.activate(
        inbox["actor"],
        inbox["scope"],
        ActivationRequest(scope=inbox["scope"], version_id=version.id, artifact_digest=version.digest),
        "e2-signal",
        context=AuditContext(),
    )
    run = await runtime.start(
        inbox["actor"],
        inbox["scope"],
        StartRunRequest(activation_id=activation.id, input={}),
        "e2-signal-run",
        context=AuditContext(),
    )
    base = inbox["source"]
    request = ProviderSourceRequest(
        **{key: getattr(base, key) for key in ProviderSourceRequest.model_fields}
    ).model_copy(update={"kind": "signal", "activation_id": None, "run_id": run.id, "signal": "incoming"})
    source = await inbox["service"].create(inbox["actor"], inbox["scope"], request, context=AuditContext())
    return {**inbox, "source": source}, run


async def test_signal_settlement_and_terminal_target(inbox, access_db):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.signals import SignalService

    inbox, run = await signal_source(inbox)
    assert (await deliver(inbox, message())).status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    signals = inbox["apps"][0].state.pyfly.context.get_bean(SignalService)
    async with UnitOfWork(access_db[0]).open(inbox["scope"]) as tx:
        await signals.deliver(
            tx, run.id, "other-event", "incoming", 4, actor=inbox["actor"], scope=inbox["scope"], context=AuditContext()
        )
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "failed" and result.reason == "target_terminal"
    assert len(await rows(inbox, access_db, "signal_receipts")) == 1


async def test_dispatch_commit_failure_rolls_back_runtime_and_receipt(inbox, access_db, monkeypatch):
    from firefly_weave.providers.repository import ProviderRepository

    assert (await deliver(inbox, message())).status_code == 200
    receipt = (await rows(inbox, access_db))[0]
    settle = ProviderRepository.settle

    async def fail_after_runtime(self, value):
        await settle(self, value)
        raise RuntimeError("fixture settlement failure")

    with monkeypatch.context() as change:
        change.setattr(ProviderRepository, "settle", fail_after_runtime)
        with pytest.raises(RuntimeError):
            await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert await rows(inbox, access_db, "runs") == []
    assert (await rows(inbox, access_db))[0]["state"] == "pending"
    result = await inbox["dispatchers"][1].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "dispatched"
    assert len(await rows(inbox, access_db, "runs")) == 1


async def test_source_credentials_recheck_disable(inbox):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.providers.credentials import ProviderCredentials

    credentials = inbox["apps"][0].state.pyfly.context.get_bean(ProviderCredentials)
    revision, secrets = await credentials.resolve(inbox["source"])
    assert revision.id == inbox["source"].connection_revision_id
    assert secrets["signature"].value == "e2-test-only-secret"
    await inbox["service"].disable(inbox["actor"], inbox["scope"], inbox["source"].id, context=AuditContext())
    with pytest.raises(CatalogError):
        await credentials.resolve(inbox["source"])


async def test_lifecycle_replay_after_removal_has_no_new_hook_effect(inbox, access_db):
    installed = message("installed", kind="lifecycle", disposition="ignore", reason="installed")
    removed = message("removed", kind="lifecycle", disposition="ignore", reason="removed")
    assert (await deliver(inbox, installed)).status_code == 200
    assert (await deliver(inbox, removed)).status_code == 200
    assert (await deliver(inbox, installed)).status_code == 200
    effects = await rows(inbox, access_db, "provider_fixture_effects")
    assert [row["event_id"] for row in effects] == ["installed", "removed"]
    assert await rows(inbox, access_db, "runs") == []


async def test_transient_cooldown_allows_later_receipts_and_cursor_survives_restart(
    inbox, access_db, scheduler_url, monkeypatch
):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.access.scheduler import next_scope
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.providers.loop import ProviderLoop
    from firefly_weave.settings import Settings

    assert (await deliver(inbox, *(message(str(i)) for i in range(11)))).status_code == 200
    uow = UnitOfWork(access_db[0])
    authority = await next_scope(uow, inbox["scope"].tenant_id, provider=True)
    dispatcher = inbox["dispatchers"][0]

    async def transient(scope, identifier):
        raise RuntimeError("fixture transient")

    with monkeypatch.context() as change:
        change.setattr(dispatcher, "dispatch_one", transient)
        assert await dispatcher.scan(authority) == 10
    async with uow.open(inbox["scope"]) as tx:
        assert (
            await tx.session.scalar(
                text("SELECT count(*) FROM provider_intents WHERE pending AND eligible_at>clock_timestamp()")
            )
            == 10
        )
    assert sum(row["receipt"]["attempts"] for row in await rows(inbox, access_db)) == 10
    assert await dispatcher.scan(authority) == 1
    assert len(await rows(inbox, access_db, "runs")) == 1
    engine = create_async_engine(scheduler_url)
    scopes = []

    try:
        for _ in range(2):
            loop = ProviderLoop(
                Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False)),
                uow,
                dispatcher,
            )
            loop.engine = engine
            with monkeypatch.context() as change:
                # Observe only: dispatch already exercised above; retain durable traversal calls.
                async def record(authority, limit):
                    assert limit == 10
                    scopes.append(authority.scope)
                    return 0

                change.setattr(dispatcher, "scan", record)
                await loop.cycle()
        assert len({scope.tenant_id for scope in scopes}) == 2
    finally:
        await engine.dispose()


async def test_unknown_null_policy_key_and_missing_installation_are_rejected(inbox):
    from firefly_weave.definitions.models import CatalogError

    source = inbox["source"]
    request = ProviderSourceRequest(**{key: getattr(source, key) for key in ProviderSourceRequest.model_fields})
    for policy in ({"account_id": "installation", "absent": None}, {}):
        with pytest.raises(CatalogError):
            await inbox["service"].create(
                inbox["actor"], inbox["scope"], request.model_copy(update={"policy": policy}), context=AuditContext()
            )


async def test_creation_validator_is_safe_and_receives_defensive_copies(inbox):
    from firefly_weave.definitions.models import CatalogError

    source = inbox["source"]
    request = ProviderSourceRequest(**{key: getattr(source, key) for key in ProviderSourceRequest.model_fields})
    inbox["verifier"].reject_source = True
    with pytest.raises(CatalogError) as failure:
        await inbox["service"].create(inbox["actor"], inbox["scope"], request, context=AuditContext())
    assert failure.value.code == "WV-PROVIDER-PAYLOAD"
    assert "fixture-secret" not in str(failure.value)
    inbox["verifier"].reject_source = False
    inbox["verifier"].mutate_validation = True
    created = await inbox["service"].create(inbox["actor"], inbox["scope"], request, context=AuditContext())
    assert created.policy == request.policy == {"account_id": "installation"}


async def test_provider_migration_privileges_and_lifespan_loop(inbox, access_db, scheduler_url, monkeypatch):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.providers.loop import ProviderLoop
    from firefly_weave.settings import Settings

    async with access_db[1]() as session:
        values = (
            await session.execute(
                text(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN "
                    "('provider_sources','provider_receipts','provider_intents')"
                )
            )
        ).all()
        assert len(values) == 3 and all(row[1] and row[2] for row in values)
    uow = UnitOfWork(access_db[0])
    async with uow.open(inbox["scope"]) as tx:
        assert not await tx.session.scalar(
            text("SELECT has_column_privilege(current_user,'provider_sources','payload','UPDATE')")
        )
        assert not await tx.session.scalar(
            text("SELECT has_column_privilege(current_user,'provider_receipts','event','UPDATE')")
        )
        route = (
            (await tx.session.execute(text("SELECT * FROM weave_provider_route(:id)"), {"id": inbox["source"].id}))
            .mappings()
            .one()
        )
        assert set(route) == {"tenant_id", "project_id", "environment_id"}
    async with uow.open(Scope(tenant_id=inbox["scope"].tenant_id)) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM provider_sources")) == 0
    loop = ProviderLoop(
        Settings(
            scheduler_enabled=True,
            database_url=access_db[3].render_as_string(hide_password=False),
            scheduler_database_url=scheduler_url,
        ),
        uow,
        inbox["dispatchers"][0],
    )
    started = asyncio.Event()

    async def held_poll():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(loop, "poll", held_poll)
    await loop.open()
    await started.wait()
    assert loop.task and not loop.task.done()
    await loop.close()
    assert loop.task is None and loop.engine is None


async def test_terminal_target_still_admits_lifecycle_and_visible_failed_dispatch(inbox, access_db):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.signals import SignalService

    inbox, run = await signal_source(inbox)
    signals = inbox["apps"][0].state.pyfly.context.get_bean(SignalService)
    async with UnitOfWork(access_db[0]).open(inbox["scope"]) as tx:
        await signals.deliver(
            tx, run.id, "finished", "incoming", 3, actor=inbox["actor"], scope=inbox["scope"], context=AuditContext()
        )
    assert (
        await deliver(inbox, message("lifecycle", kind="lifecycle", disposition="ignore", reason="removed"))
    ).status_code == 200
    assert (await deliver(inbox, message("after-terminal"))).status_code == 200
    receipts = await rows(inbox, access_db)
    assert len(receipts) == 2
    receipt = next(row for row in receipts if row["state"] == "pending")
    result = await inbox["dispatchers"][0].dispatch_one(inbox["scope"], receipt["id"])
    assert result.state == "failed" and result.reason == "target_terminal"
    assert len(await rows(inbox, access_db, "signal_receipts")) == 1


async def test_native_sdk_creation_assigns_authenticated_owner_and_concrete_pins(
    inbox, access_db, provisioned, authenticated_client, headers, scheduler_url
):
    from firefly_weave.access.models import Grant
    from firefly_weave.app import make_app
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.settings import Settings

    token = headers["Authorization"].removeprefix("Bearer ")
    auth_app = authenticated_client[0]._transport.app
    actor = await auth_app.state.authentication.authenticate(token)
    for role in ("tenant_admin", "deployer", "operator"):
        await access_db[2].grant(provisioned[0], actor.id, Grant(role=role, scope=inbox["scope"]))
    app = make_app(
        Settings(
            scheduler_enabled=False,
            scheduler_database_url=scheduler_url,
            connector_packages=(IDENTITY,),
            database_url=access_db[3].render_as_string(hide_password=False),
        ),
        secrets=inbox["service"].bindings.connections.secrets,
        verifiers=auth_app.state.restart_verifiers,
    )
    async with (
        app.router.lifespan_context(app),
        WeaveClient("http://localhost", lambda: token, inbox["scope"], transport=ASGITransport(app)) as sdk,
    ):
        assert app.state.compatibility.ready
        original = inbox["source"]
        request = ProviderSourceRequest(
            **{key: getattr(original, key) for key in ProviderSourceRequest.model_fields}
        ).model_copy(update={"adapter_version": None})
        created = await sdk.create_provider_source(request)
        assert created.principal_id == actor.id and created.principal_id != original.principal_id
        assert created.adapter_version == "1.0.0"
        assert created.activation_id == original.activation_id
        assert created.connection_revision_id == original.connection_revision_id
        assert (await sdk.disable_provider_source(created.id)).disabled


async def test_ignored_only_schema_cannot_dispatch_but_can_record_lifecycle(inbox, access_db):
    inbox, _ = await signal_source(inbox)
    lifecycle = message(
        "lifecycle-text", value="removed", kind="lifecycle_string", disposition="ignore", reason="removed"
    )
    assert (await deliver(inbox, lifecycle)).status_code == 200
    assert (await rows(inbox, access_db))[0]["state"] == "ignored"
    forbidden = message("forbidden", value="removed", kind="lifecycle_string")
    assert (await deliver(inbox, forbidden)).status_code == 422
    assert (await deliver(inbox, message("invalid-message", value="string"))).status_code == 422
    assert len(await rows(inbox, access_db)) == 1
