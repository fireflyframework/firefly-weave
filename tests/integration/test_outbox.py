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
"""Real PostgreSQL, real bounded HTTP, current authority and durable redelivery."""

import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent, SubscriptionRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.event_delivery import OutboxDispatcher
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.operations.subscriptions import SubscriptionService

pytestmark = pytest.mark.integration


@asynccontextmanager
async def receiver(database, status=204):
    state = SimpleNamespace(requests=[], effects=set(), status=status)
    with sqlite3.connect(database) as store:
        store.execute("CREATE TABLE IF NOT EXISTS effects(event_id TEXT PRIMARY KEY)")
    state.database = database

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = {
                k.decode().lower(): v.decode().strip()
                for k, v in (line.split(b":", 1) for line in head.split(b"\r\n")[1:] if b":" in line)
            }
            body = await reader.readexactly(int(headers["content-length"]))
            event = json.loads(body)
            state.requests.append(
                {"headers": headers, "body": body, "eventId": event["eventId"], "path": head.split(b" ")[1]}
            )
            from firefly_weave.triggers.webhooks import authenticate

            assert authenticate(state.key().encode(), body, headers, tolerance=300) == event["eventId"]
            with sqlite3.connect(database) as store:
                store.execute("INSERT OR IGNORE INTO effects VALUES(?)", (event["eventId"],))
                store.commit()
            state.effects.add(event["eventId"])
            if getattr(state, "slow", None) is not None and head.split(b" ")[1] == b"/events":
                await state.slow.wait()
            writer.write(f"HTTP/1.1 {state.status} Result\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def setup(services, access_db, provisioned, tmp_path):
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.definitions.service import DefinitionService

    scope = provisioned[1][0]
    actor = await access_db[2].load_principal(provisioned[0].id)
    for role in ("developer", "deployer", "operator", "viewer"):
        await access_db[2].grant(
            provisioned[0], actor.id, Grant(role=role, scope=scope.model_copy(update={"environment_id": None}))
        )
    actor = await access_db[2].load_principal(actor.id)
    provider = SimpleNamespace(value="secret-outbox-signing-canary-0123456789", version="one")

    class Provider:
        def resolve(self, handle):
            return ResolvedSecret(value=provider.value, provider_version=provider.version)

    secrets = ScopedSecrets({"test": Provider()}, (SecretGrant(scope, "signing", "test", "key"),))
    registry = ConnectorRegistry()
    registry.register_descriptor(HTTP_DESCRIPTOR, HttpConnector(SecureHttpClient(), HttpPolicy()))
    graph = services(access_db[0], registry=registry, secrets=secrets)
    # Operator-selected private egress policy, set before constructor resolution.
    graph.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    definitions = graph.resolve(DefinitionService)
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(HTTP_DESCRIPTOR.manifest.value), "json", "http", context=AuditContext()
    )
    async with receiver(tmp_path / "receiver.sqlite3") as remote:
        remote.key = lambda: provider.value
        connection = await graph.resolve(ConnectionService).create_revision(
            actor,
            scope,
            ConnectionRequest(
                name="destination",
                connector_version_id=connector.id,
                config={"baseUrl": remote.url, "auth": "bearer"},
                secretRef={"token": "signing"},
                allowed_destinations=(remote.url,),
            ),
            context=AuditContext(),
        )
        subscriptions = graph.resolve(SubscriptionService)
        request = SubscriptionRequest(
            name="notifications",
            connection_revision_id=connection.id,
            signing_slot="token",
            path="/events",
            event_types=("run.transition", "definition.published", "activation.activated", "activation.retired"),
        )
        sub = await subscriptions.save(actor, scope, request, context=AuditContext())
        dispatcher = graph.resolve(OutboxDispatcher)

        async def dispatch():
            return await dispatcher.dispatch(scope, actor=actor, context=AuditContext())

        async def append(kind="run.transition"):
            event = IntegrationEvent(
                event_id=uuid4(),
                type=kind,
                scope=scope if kind != "definition.published" else scope.model_copy(update={"environment_id": None}),
                resource_id=uuid4(),
                correlation_id=uuid4(),
                emitted_at=datetime.now(UTC),
                payload=EventMetadata(status="waiting", sequence=1) if kind == "run.transition" else EventMetadata(),
            )
            async with dispatcher.uow.open(event.scope) as tx:
                await dispatcher.outbox.append(tx, event)
            return event

        async def rows(table):
            async with access_db[1].begin() as session:
                return [dict(row) for row in (await session.execute(text("SELECT * FROM " + table))).mappings()]

        async def due():
            async with access_db[1].begin() as session:
                await session.execute(
                    text(
                        "UPDATE event_deliveries SET next_at=clock_timestamp()-interval '1 "
                        "second',lease_until=clock_timestamp()-interval '1 second'"
                    )
                )

        yield SimpleNamespace(**locals())


async def test_native_outbox_service_exists(services, db):
    graph = services(db)
    assert graph.resolve(OutboxDispatcher).outbox is graph.resolve(OutboxService)
    assert OutboxDispatcher.__pyfly_stereotype__ == "service"


async def test_rollback_and_uncommitted_visibility(setup):
    s = setup
    event = IntegrationEvent(
        event_id=uuid4(),
        type="run.transition",
        scope=s.scope,
        resource_id=uuid4(),
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        payload=EventMetadata(status="waiting", sequence=1),
    )
    with pytest.raises(RuntimeError, match="rollback"):
        async with s.dispatcher.uow.open(s.scope) as tx:
            await s.dispatcher.outbox.append(tx, event)
            # The writer owns the project fence; a second writer must fail promptly.
            async with asyncio.timeout(2):
                with pytest.raises(CatalogError) as rejected:
                    await s.dispatch()
            assert rejected.value.status == 429 and rejected.value.code == "WV-OPERATION-CAPACITY"
            async with s.dispatcher.uow.open(s.scope, mutation=False) as observer:
                assert await observer.session.scalar(text("SELECT count(*) FROM event_deliveries")) == 0
            assert not s.remote.requests
            raise RuntimeError("rollback")
    assert not await s.rows("event_deliveries")
    assert (await s.dispatch()).delivered == 0
    assert not s.remote.requests


async def test_signed_http_envelope_and_metadata_only(setup):
    s = setup
    event = await s.append()
    assert (await s.dispatch()).delivered == 1
    received = s.remote.requests[0]
    assert received["eventId"] == str(event.event_id)
    assert received["headers"]["idempotency-key"] == str(event.event_id)
    assert json.loads(received["body"])["payload"]["event_id"] == str(event.event_id)
    stamp = received["headers"]["x-weave-timestamp"]
    assert (
        received["headers"]["x-weave-signature"]
        == hmac.new(s.provider.value.encode(), stamp.encode() + b"." + received["body"], hashlib.sha256).hexdigest()
    )
    assert s.provider.value.encode() not in received["body"]
    assert (await s.rows("delivery_attempts"))[0]["provider_version"] == "one"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EventMetadata.model_validate({"secret": "canary"})


async def test_external_accept_before_ack_is_redeliverable(setup, monkeypatch):
    s = setup
    await s.append()
    original = s.dispatcher._settle

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(s.dispatcher, "_settle", crash)
    with pytest.raises(asyncio.CancelledError):
        await s.dispatch()
    assert len(s.remote.requests) == 1
    first_attempt = (await s.rows("event_deliveries"))[0]
    await s.due()
    monkeypatch.setattr(s.dispatcher, "_settle", original)
    from firefly_weave.connectors.http import HttpPolicy

    graph = s.services(s.access_db[0], registry=s.registry, secrets=s.secrets)
    graph.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    restarted = graph.resolve(OutboxDispatcher)
    assert restarted is not s.dispatcher
    assert (await restarted.dispatch(s.scope, actor=s.actor, context=AuditContext())).delivered == 1
    assert len(s.remote.requests) == 2
    from firefly_weave.operations.event_delivery import StaleDelivery

    with pytest.raises(StaleDelivery):
        await restarted._settle(s.scope, first_attempt["id"], first_attempt["token"], "ACK", True)
    assert s.remote.requests[0]["body"] == s.remote.requests[1]["body"]
    with sqlite3.connect(s.remote.database) as persisted:
        assert persisted.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    attempts = await s.rows("delivery_attempts")
    assert len(attempts) == 2 and {a["outcome"] for a in attempts} == {"ACK_UNKNOWN", "ACK"}


async def test_two_dispatchers_and_stale_fencing(setup):
    s = setup
    for _ in range(4):
        await s.append()
    other = s.services(s.access_db[0], registry=s.registry, secrets=s.secrets)
    from firefly_weave.connectors.http import HttpPolicy

    other.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    second = other.resolve(OutboxDispatcher)
    reports = await asyncio.gather(s.dispatch(), second.dispatch(s.scope, actor=s.actor, context=AuditContext()))
    assert sum(r.delivered for r in reports) == 4 and len(s.remote.requests) == 4
    from firefly_weave.operations.event_delivery import StaleDelivery

    with pytest.raises(StaleDelivery):
        await s.dispatcher._settle(s.scope, (await s.rows("event_deliveries"))[0]["id"], uuid4(), "ACK", True)


async def test_subscription_revision_does_not_reroute_and_new_subscription_does_not_replay(setup):
    s = setup
    await s.append()
    newer = await s.subscriptions.save(
        s.actor, s.scope, s.request.model_copy(update={"path": "/new"}), context=AuditContext()
    )
    assert newer.id != s.sub.id
    assert (await s.dispatch()).delivered == 1
    assert s.remote.requests[0]["path"] == b"/events"
    await s.append()
    assert (await s.dispatch()).delivered == 1
    assert s.remote.requests[1]["path"] == b"/new"
    assert len(await s.rows("event_deliveries")) == 2


async def test_exhaustion_and_manual_retry_preserve_ids_and_history(setup):
    s = setup
    event = await s.append()
    s.remote.status = 500
    for _ in range(3):
        await s.dispatch()
        await s.due()
    row = (await s.rows("event_deliveries"))[0]
    assert row["status"] == "incident" and row["attempts"] == 3
    await s.dispatcher.retry(s.actor, s.scope, row["id"], context=AuditContext())
    assert (await s.rows("event_deliveries"))[0]["attempt_limit"] == 4
    s.remote.status = 204
    assert (await s.dispatch()).delivered == 1
    assert len(await s.rows("delivery_attempts")) == 4
    assert {r["eventId"] for r in s.remote.requests} == {str(event.event_id)}
    assert {r["headers"]["x-weave-delivery-id"] for r in s.remote.requests} == {str(row["id"])}


async def test_revoked_binding_blocks_pending_and_retry(setup):
    s = setup
    await s.append()
    await s.subscriptions.bindings.revoke(s.actor, s.scope, s.sub.binding_id, context=AuditContext())
    assert (await s.dispatch()).incident == 1
    assert not s.remote.requests
    with pytest.raises(CatalogError):
        await s.dispatcher.retry(s.actor, s.scope, (await s.rows("event_deliveries"))[0]["id"], context=AuditContext())


async def test_rotation_behind_handle_and_project_event_scope(setup):
    s = setup
    await s.append("definition.published")
    s.provider.value = "rotated-signing-canary-9876543210"
    s.provider.version = "two"
    assert (await s.dispatch()).delivered == 1
    payload = json.loads(s.remote.requests[0]["body"])["payload"]
    assert payload["scope"]["environment_id"] is None
    assert (await s.rows("delivery_attempts"))[0]["provider_version"] == "two"


async def test_current_grants_rechecked_after_provider_io(setup, monkeypatch):
    s = setup
    await s.append()
    original = s.subscriptions.bindings.resolve

    async def revoke_after(*args, **kwargs):
        result = await original(*args, **kwargs)
        await s.subscriptions.bindings.revoke(s.actor, s.scope, s.sub.binding_id, context=AuditContext())
        return result

    monkeypatch.setattr(s.subscriptions.bindings, "resolve", revoke_after)
    assert (await s.dispatch()).incident == 1
    assert not s.remote.requests


async def test_subscription_cap_and_invalid_paths(setup):
    s = setup
    from pydantic import ValidationError

    for path in ("//other", "/../secret", "/%2e%2e/secret", "https://other", "/a?token=x"):
        with pytest.raises(ValidationError):
            SubscriptionRequest(**{**s.request.model_dump(), "path": path})
    for i in range(63):
        await s.subscriptions.save(
            s.actor, s.scope, s.request.model_copy(update={"name": f"n{i}"}), context=AuditContext()
        )
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises(CatalogError, match="limit"):
        await s.subscriptions.save(
            s.actor, s.scope, s.request.model_copy(update={"name": "overflow"}), context=AuditContext()
        )
    await s.append()
    assert len(await s.rows("event_deliveries")) == 64


async def test_bare_scope_and_admin_business_denied(setup):
    s = setup
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Principal

    with pytest.raises(AccessDenied):
        await s.dispatcher.dispatch(s.scope)
    admin = Principal(id=uuid4(), kind="human", grants=(Grant(role="platform_admin", scope=None),))
    with pytest.raises(AuthenticationFailed):
        await s.subscriptions.save(admin, s.scope, s.request, context=AuditContext())


async def test_actual_authoritative_mutations_emit_once_and_rollback(setup):
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    s = setup
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "metadata-flow", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [], "output": {"literal": None}},
    }
    source = json.dumps(document)
    version = await s.definitions.publish(s.actor, s.scope, "Workflow", source, "json", "flow", context=AuditContext())
    same = await s.definitions.publish(
        s.actor, s.scope, "Workflow", source, "json", "flow-again", context=AuditContext()
    )
    assert same.id == version.id
    activation = await s.definitions.activate(
        s.actor,
        s.scope,
        ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=s.scope),
        "activation",
        context=AuditContext(),
    )
    runtime = s.graph.resolve(RuntimeService)
    request = StartRunRequest(
        activation_id=activation.id, input={"secret": "business-input-canary"}, correlation_key="private-correlation"
    )
    run = await runtime.start(s.actor, s.scope, request, "run", context=AuditContext())
    assert (await runtime.start(s.actor, s.scope, request, "run", context=AuditContext())).id == run.id
    with pytest.raises(RuntimeError, match="abort"):
        async with s.dispatcher.uow.open(s.scope) as tx:
            await runtime.start(s.actor, s.scope, request, "rollback-run", context=AuditContext(), tx=tx)
            raise RuntimeError("abort")
    assert len(await s.rows("integration_events")) == 4

    await s.definitions.retire_activation(
        s.actor, s.scope, activation.id, "retirement", activation.revision, context=AuditContext()
    )
    events = await s.rows("integration_events")
    assert len(events) == 5  # Initial connector plus four source mutations.
    encoded = json.dumps([e["payload"] for e in events])
    assert "business-input-canary" not in encoded and "private-correlation" not in encoded
    assert len(await s.rows("event_deliveries")) == 4


async def test_native_sdk_metadata_and_scope_authority(setup, authenticated_client, headers, other_headers, env_url):
    from httpx import ASGITransport

    from firefly_weave.sdk.client import WeaveClient

    s = setup
    await s.append()
    await s.dispatch()
    delivery = (await s.rows("event_deliveries"))[0]["id"]
    client = authenticated_client[0]
    token = headers["Authorization"].removeprefix("Bearer ")
    async with WeaveClient(
        "http://localhost", lambda: token, s.scope, transport=ASGITransport(app=client._transport.app)
    ) as sdk:
        assert (await sdk.read_subscription(s.sub.id)).id == s.sub.id
        assert (await sdk.list_subscriptions()).items[0].id == s.sub.id
        assert (await sdk.read_delivery(delivery)).status == "delivered"
        assert (await sdk.list_deliveries()).items[0].id == delivery
        history = await sdk.delivery_attempts(delivery)
        assert history.items[0].outcome == "ACK" and history.items[0].provider_version == "one"
    denied = await client.get("/api/v1" + env_url + "/deliveries/" + str(delivery), headers=other_headers)
    assert denied.status_code == 403
    denied = await client.post("/api/v1" + env_url + "/deliveries/" + str(delivery) + "/retry", headers=headers)
    assert denied.status_code == 403


async def test_scope_constraints_and_immutable_event_rows(setup, provisioned):
    from sqlalchemy.exc import DBAPIError

    s = setup
    event = await s.append()
    async with s.dispatcher.uow.open(provisioned[1][1]) as tx:
        assert not list((await tx.session.execute(text("SELECT id FROM integration_events"))).scalars())
    with pytest.raises(DBAPIError):
        async with s.dispatcher.uow.open(s.scope) as tx:
            await tx.session.execute(
                text("UPDATE integration_events SET payload=payload WHERE id=:id"), {"id": event.event_id}
            )
    async with s.dispatcher.uow.open(s.scope) as tx:
        assert await s.dispatcher.outbox.append(tx, event) == event.event_id
    assert len(await s.rows("event_deliveries")) == 1


async def test_source_owner_revocation_prevents_send(setup):
    s = setup
    await s.append()
    async with s.access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='viewer'"), {"id": s.actor.id}
        )
    assert (await s.dispatch()).incident == 1
    assert not s.remote.requests


async def test_capacity_reserved_before_claim_and_sibling_cancellation(setup, monkeypatch):
    import firefly_weave.operations.event_delivery as module

    s = setup
    for _ in range(5):
        await s.append()
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(s.dispatcher, "_attempt", blocked)
    task = asyncio.create_task(s.dispatch())
    await asyncio.wait_for(entered.wait(), 2)
    assert module.available() == 0
    assert (await s.dispatch()).model_dump() == {"delivered": 0, "retry": 0, "incident": 0}
    rows = await s.rows("event_deliveries")
    assert sum(row["attempts"] for row in rows) == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert module.available() == 4


async def test_durable_scheduler_traversal_and_restart(setup, scheduler_url):
    from pydantic import SecretStr

    from firefly_weave.operations.outbox_loop import OutboxLoop
    from firefly_weave.settings import Settings

    s = setup
    await s.append()
    settings = Settings(
        database_url=SecretStr(s.access_db[3].render_as_string(hide_password=False)),
        scheduler_enabled=True,
        scheduler_database_url=SecretStr(scheduler_url),
    )
    first = OutboxLoop(settings, s.dispatcher.uow, s.dispatcher)
    await first.open()
    await first.close()
    second = OutboxLoop(settings, s.dispatcher.uow, s.dispatcher)
    try:
        await second.open()
        for _ in range(60):
            if s.remote.requests:
                break
            await asyncio.sleep(0.1)
        assert len(s.remote.requests) == 1
    finally:
        await second.close()


async def test_dead_and_delayed_first_page_cannot_hide_valid_delivery(setup):
    s = setup
    for _ in range(10):
        await s.append()
    await s.subscriptions.save(s.actor, s.scope, s.request.model_copy(update={"path": "/live"}), context=AuditContext())
    await s.append()
    await s.subscriptions.bindings.revoke(s.actor, s.scope, s.sub.binding_id, context=AuditContext())
    reports = [await s.dispatch() for _ in range(3)]
    assert sum(r.incident for r in reports) == 10
    assert sum(r.delivered for r in reports) == 1
    assert s.remote.requests[0]["path"] == b"/live"
    await s.append()
    async with s.access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE event_deliveries SET status='retry',next_at=clock_timestamp()+interval '1 day' "
                "WHERE status='incident'"
            )
        )
    assert (await s.dispatch()).delivered == 1


async def test_receiver_rejects_signature_body_identity_and_stale_timestamp(setup):
    from firefly_weave.triggers.webhooks import authenticate

    s = setup
    await s.append()
    await s.dispatch()
    received = s.remote.requests[0]
    headers = received["headers"]
    secret = s.provider.value.encode()
    for body, changed in [
        (received["body"] + b" ", headers),
        (received["body"], {**headers, "x-weave-event-id": str(uuid4())}),
        (received["body"], {**headers, "x-weave-timestamp": "1"}),
    ]:
        with pytest.raises(CatalogError):
            authenticate(secret, body, changed, tolerance=300)


async def test_redirect_is_not_followed_and_scope_check_is_active(setup):
    from firefly_weave.persistence.uow import Transaction

    s = setup
    await s.append()
    s.remote.status = 302
    assert (await s.dispatch()).retry == 1
    assert len(s.remote.requests) == 1
    event = IntegrationEvent(
        event_id=uuid4(),
        type="run.transition",
        scope=s.scope,
        resource_id=uuid4(),
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        payload=EventMetadata(),
    )
    async with s.access_db[0]() as session:
        with pytest.raises(ValueError, match="Active"):
            await s.dispatcher.outbox.append(Transaction(session, s.scope), event)


async def test_concurrent_manual_retry_grants_only_one_extra_attempt(setup):
    s = setup
    await s.append()
    s.remote.status = 500
    for _ in range(3):
        await s.dispatch()
        await s.due()
    row = (await s.rows("event_deliveries"))[0]
    results = await asyncio.gather(
        *(s.dispatcher.retry(s.actor, s.scope, row["id"], context=AuditContext()) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CatalogError) for r in results) == 1
    assert (await s.rows("event_deliveries"))[0]["attempt_limit"] == 4


async def test_slow_tenant_receiver_leaves_other_tenant_progress(setup, scheduler_url, monkeypatch):
    from pydantic import SecretStr

    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.http import HttpPolicy
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.outbox_loop import OutboxLoop
    from firefly_weave.settings import Settings

    s = setup
    other = s.provisioned[1][1]
    for role in ("developer", "deployer", "operator", "viewer"):
        await s.access_db[2].grant(
            s.provisioned[0], s.actor.id, Grant(role=role, scope=other.model_copy(update={"environment_id": None}))
        )
    actor = await s.access_db[2].load_principal(s.actor.id)
    secrets = ScopedSecrets(
        {"test": s.Provider()}, tuple(SecretGrant(scope, "signing", "test", "key") for scope in (s.scope, other))
    )
    graph = s.services(s.access_db[0], registry=s.graph.resolve(ConnectorRegistry), secrets=secrets)
    graph.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    definitions = graph.resolve(DefinitionService)
    published = await definitions.publish(
        actor,
        other,
        "Connector",
        json.dumps(HTTP_DESCRIPTOR.manifest.value),
        "json",
        "other-http",
        context=AuditContext(),
    )
    conn = await graph.resolve(ConnectionService).create_revision(
        actor,
        other,
        ConnectionRequest(
            name="other",
            connector_version_id=published.id,
            config={"baseUrl": s.remote.url, "auth": "bearer"},
            secretRef={"token": "signing"},
            allowed_destinations=(s.remote.url,),
        ),
        context=AuditContext(),
    )
    await graph.resolve(SubscriptionService).save(
        actor,
        other,
        s.request.model_copy(
            update={"connection_revision_id": conn.id, "path": "/fast", "event_types": ("run.transition",)}
        ),
        context=AuditContext(),
    )
    dispatcher = graph.resolve(OutboxDispatcher)
    settle = dispatcher._settle

    async def delayed_settlement(scope, *args, **kwargs):
        if scope == other:
            # Receiver arrival must not be mistaken for a committed delivery receipt.
            await asyncio.sleep(0.5)
        return await settle(scope, *args, **kwargs)

    monkeypatch.setattr(dispatcher, "_settle", delayed_settlement)
    event = IntegrationEvent(
        event_id=uuid4(),
        type="run.transition",
        scope=other,
        resource_id=uuid4(),
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        payload=EventMetadata(status="waiting", sequence=1),
    )
    async with dispatcher.uow.open(other) as tx:
        await dispatcher.outbox.append(tx, event)
    # Put the noisy tenant first in the durable catalog turn, independently of UUID order.
    async with s.access_db[1].begin() as session:
        await session.execute(
            text("UPDATE outbox_tenant_cursor SET tenant_id=:previous"), {"previous": other.tenant_id}
        )
    for _ in range(8):
        await s.append()
    s.remote.slow = asyncio.Event()
    settings = Settings(
        database_url=SecretStr(s.access_db[3].render_as_string(hide_password=False)),
        scheduler_enabled=True,
        scheduler_database_url=SecretStr(scheduler_url),
    )
    loop = OutboxLoop(settings, dispatcher.uow, dispatcher)
    try:
        await loop.open()
        async with asyncio.timeout(8):
            while True:
                rows = await s.rows("event_deliveries")
                if any(row["environment_id"] == other.environment_id and row["status"] == "delivered" for row in rows):
                    break
                await asyncio.sleep(0.1)
        assert any(r["path"] == b"/events" for r in s.remote.requests)
        assert any(r["path"] == b"/fast" for r in s.remote.requests)
        assert not s.remote.slow.is_set()
        assert any(row["environment_id"] == other.environment_id and row["status"] == "delivered" for row in rows)
        assert any(row["environment_id"] == s.scope.environment_id and row["status"] == "leased" for row in rows)
    finally:
        s.remote.slow.set()
        await loop.close()


async def test_expiry_during_provider_io_prevents_send(setup, monkeypatch):
    s = setup
    await s.append()
    original = s.subscriptions.bindings.resolve

    async def expired(*args, **kwargs):
        result = await original(*args, **kwargs)
        await s.due()
        return result

    monkeypatch.setattr(s.subscriptions.bindings, "resolve", expired)
    assert (await s.dispatch()).retry == 1
    assert not s.remote.requests


async def test_capacity_refusal_before_send_leaves_the_lease_for_recovery(setup, monkeypatch):
    s = setup
    await s.append()
    original = s.subscriptions.bindings.resolve
    held, released = asyncio.Event(), asyncio.Event()
    tasks = []

    async def hold():
        # Another writer owns the project operations fence past the 250 ms admission timeout.
        async with s.access_db[1].begin() as session:
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:' "
                    "|| cast(:tenant AS text) || ':' || cast(:project AS text),0))"
                ),
                {"tenant": str(s.scope.tenant_id), "project": str(s.scope.project_id)},
            )
            held.set()
            await released.wait()

    async def watch():
        # Release the fence once the refused writer gives up or another transaction queues behind it.
        first = None
        while not released.is_set():
            async with s.access_db[1].begin() as session:
                waiting = set(
                    (
                        await session.execute(
                            text("SELECT virtualtransaction FROM pg_locks WHERE locktype='advisory' AND NOT granted")
                        )
                    ).scalars()
                )
            if first is None:
                first = waiting or None
            elif waiting != first:
                released.set()
                return
            await asyncio.sleep(0.01)

    async def contended(*args, **kwargs):
        result = await original(*args, **kwargs)
        tasks.append(asyncio.create_task(hold()))
        async with asyncio.timeout(5):
            await held.wait()
        tasks.append(asyncio.create_task(watch()))
        return result

    monkeypatch.setattr(s.subscriptions.bindings, "resolve", contended)
    report = await s.dispatch()
    released.set()
    async with asyncio.timeout(5):
        await asyncio.gather(*tasks)
    assert report.retry == 1 and report.incident == 0
    assert not s.remote.requests
    row = (await s.rows("event_deliveries"))[0]
    # Nothing was settled: the fenced lease waits for expiry instead of recording a failed delivery.
    assert row["status"] == "leased" and row["code"] is None and row["attempts"] == 1
    assert [attempt["outcome"] for attempt in await s.rows("delivery_attempts")] == [None]
    monkeypatch.setattr(s.subscriptions.bindings, "resolve", original)
    await s.due()
    assert (await s.dispatch()).delivered == 1
    assert len(s.remote.requests) == 1
    assert {attempt["outcome"] for attempt in await s.rows("delivery_attempts")} == {"ACK_UNKNOWN", "ACK"}
