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
"""Real PostgreSQL proves atomic WhatsApp admission, history, authority and scoped reads."""

import asyncio
import copy
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
from firefly_weave.contracts.providers import ProviderSourceRequest, provider_schema_digest
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.providers.service import ProviderIngressService
from firefly_weave.providers.whatsapp_status import WhatsAppStatusService

pytestmark = pytest.mark.integration
IDENTITY = "firefly-weave:weave-whatsapp:firefly_weave.connectors.whatsapp:package"


def body(*statuses, message_id="wamid.out", message=False):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "300"},
        "statuses": [
            {"id": message_id, "recipient_id": "15550000001", "status": state, "timestamp": str(1700000000 + index)}
            for index, state in enumerate(statuses)
        ],
    }
    if message:
        value["messages"] = [
            {
                "id": "wamid.in",
                "from": "15550000001",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "Hello"},
            }
        ]
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "200", "changes": [{"field": "messages", "value": value}]}],
    }


@pytest.fixture
async def whatsapp(worker_setup, access_db, provisioned, scheduler_url):
    from firefly_weave.access.models import Grant
    from firefly_weave.app import make_app
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.whatsapp import package
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.settings import Settings

    _, _, _, actor, scope, activation, _ = worker_setup
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    actor = await access_db[2].load_principal(actor.id)

    class Provider:
        def __init__(self):
            self.calls = []
            self.unavailable = set()

        def resolve(self, locator):
            self.calls.append(locator)
            if locator in self.unavailable:
                raise ValueError("secret unavailable")
            return ResolvedSecret(
                value={"verify": "verify-token", "app": "app-secret", "access": "access-token"}[locator]
            )

    provider = Provider()
    secrets = ScopedSecrets(
        {"fixture": provider}, tuple(SecretGrant(scope, slot, "fixture", slot) for slot in ("verify", "app", "access"))
    )
    settings = Settings(
        scheduler_enabled=False,
        scheduler_database_url=scheduler_url,
        connector_packages=(IDENTITY,),
        database_url=access_db[3].render_as_string(hide_password=False),
    )
    apps = [make_app(settings, secrets=secrets), make_app(settings, secrets=secrets)]
    async with AsyncExitStack() as stack:
        clients = []
        for app in apps:
            await stack.enter_async_context(app.router.lifespan_context(app))
            assert app.state.compatibility.ready
            clients.append(
                await stack.enter_async_context(
                    AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://local")
                )
            )
        native = apps[0].state.pyfly.context
        ingress = native.get_bean(ProviderIngressService)
        version = await ingress.runtime.definitions.publish(
            actor,
            scope,
            "Connector",
            package.metadata.model.manifest.model_dump_json(by_alias=True),
            "json",
            "whatsapp",
            context=AuditContext(),
        )
        connection = await native.get_bean(ConnectionService).create_revision(
            actor,
            scope,
            ConnectionRequest(
                name="whatsapp",
                connector_version_id=version.id,
                config={
                    "app_id": "100",
                    "account_id": "200",
                    "phone_number_id": "300",
                    "business_phone_number": "15550000000",
                    "graph_version": "v26.0",
                    "recipient_allowlist": ["15550000001"],
                    "approved_templates": [{"name": "appointment", "locale": "en_US", "body_parameters": 1}],
                },
                secretRef={"accessToken": "access", "appSecret": "app", "verifyToken": "verify"},
                allowed_destinations=("https://graph.facebook.com",),
            ),
            context=AuditContext(),
        )
        request = ProviderSourceRequest(
            name="whatsapp",
            provider="whatsapp",
            package="firefly-weave",
            package_version="0.1.0a4",
            schema_digest=provider_schema_digest(
                package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds
            ),
            connection_revision_id=connection.id,
            policy=connection.config,
            kind="run",
            activation_id=activation.id,
        )
        source = await ingress.create(actor, scope, request, context=AuditContext())
        yield dict(
            apps=apps,
            clients=clients,
            ingress=ingress,
            source=source,
            scope=scope,
            actor=actor,
            provider=provider,
            statuses=native.get_bean(WhatsAppStatusService),
            request=request,
            connection=connection,
            settings=settings,
            secrets=secrets,
        )


async def deliver(wa, value, *, client=0, source=None):
    raw = json.dumps(value).encode()
    return await wa["clients"][client].post(
        f"/provider-ingress/{(source or wa['source']).id}",
        content=raw,
        headers={"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()},
    )


async def rows(wa, access_db, table):
    async with UnitOfWork(access_db[0]).open(wa["scope"]) as tx:
        return list((await tx.session.execute(text(f"SELECT * FROM {table}"))).mappings())


async def read(wa, message_id="wamid.out"):
    return await wa["statuses"].read(wa["actor"], wa["scope"], wa["source"].id, message_id, context=AuditContext())


async def test_out_of_order_status_facts_do_not_collapse_or_rewrite(whatsapp, access_db):
    wa = whatsapp
    assert (await deliver(wa, body("read", "sent", "delivered", "failed", "deleted"))).status_code == 200
    receipts = await rows(wa, access_db, "provider_receipts")
    assert len(receipts) == 5 and len({r["event_id"] for r in receipts}) == 5
    state = await read(wa)
    assert state.progress == "read" and state.failed_seen and state.deleted_seen and state.fact_count == 5
    facts = await wa["statuses"].facts(wa["actor"], wa["scope"], wa["source"].id, state.id, context=AuditContext())
    assert {v["status"] for v in facts["items"]} == {"read", "sent", "delivered", "failed", "deleted"}


async def test_concurrent_duplicate_and_cross_source_facts_are_one_history(whatsapp, access_db):
    wa = whatsapp
    replacement = await wa["ingress"].create(wa["actor"], wa["scope"], wa["request"], context=AuditContext())
    value = body("read", "sent")
    deliveries = ({}, {"client": 1}, {"client": 1, "source": replacement})
    responses = await asyncio.gather(*(deliver(wa, value, **options) for options in deliveries))
    for response, options in zip(responses, deliveries, strict=True):
        if response.status_code == 429:
            assert response.json()["code"] == "WV-OPERATION-CAPACITY"
            # Explicit redelivery after competing requests finish preserves the exact event identities.
            response = await deliver(wa, value, **options)
        assert response.status_code == 200, response.text
    assert len(await rows(wa, access_db, "provider_receipts")) == 4
    assert len(await rows(wa, access_db, "whatsapp_status_facts")) == 2
    assert len(await rows(wa, access_db, "whatsapp_status_observations")) == 4
    assert (await read(wa)).fact_count == 2


async def test_conflicting_fact_in_other_source_rolls_back_entire_mixed_batch(whatsapp, access_db):
    wa = whatsapp
    assert (await deliver(wa, body("read"))).status_code == 200
    replacement = await wa["ingress"].create(wa["actor"], wa["scope"], wa["request"], context=AuditContext())
    value = body("read", message=True)
    value["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [{"code": 123}]
    assert (await deliver(wa, value, source=replacement)).status_code == 409
    assert len(await rows(wa, access_db, "provider_receipts")) == 1
    assert len(await rows(wa, access_db, "whatsapp_status_observations")) == 1
    assert (await read(wa)).fact_count == 1


async def test_mixed_waba_and_late_bad_signature_leave_no_state(whatsapp, access_db):
    wa = whatsapp
    value = body("sent", message=True)
    value["entry"].append(copy.deepcopy(value["entry"][0]))
    value["entry"][1]["id"] = "201"
    assert (await deliver(wa, value)).status_code == 401
    raw = json.dumps(body("read")).encode()
    response = await wa["clients"][0].post(
        f"/provider-ingress/{wa['source'].id}",
        content=raw + b" ",
        headers={"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()},
    )
    assert response.status_code == 401
    assert await rows(wa, access_db, "provider_receipts") == []
    assert await rows(wa, access_db, "whatsapp_message_states") == []


async def test_get_post_resolve_only_required_slot_and_recheck_revoke(whatsapp, access_db):
    wa = whatsapp
    wa["provider"].calls.clear()
    wa["provider"].unavailable.update({"access", "app"})
    path = f"/provider-ingress/{wa['source'].id}"
    query = {"hub.mode": "subscribe", "hub.verify_token": "verify-token", "hub.challenge": "123"}
    response = await wa["clients"][0].get(path, params=query)
    assert response.status_code == 200 and response.text == "123"
    assert wa["provider"].calls == ["verify"]
    assert await rows(wa, access_db, "provider_receipts") == []
    wa["provider"].unavailable = {"access", "verify"}
    wa["provider"].calls.clear()
    assert (await deliver(wa, body("read"))).status_code == 200
    assert wa["provider"].calls == ["app"]
    await wa["ingress"].bindings.revoke(wa["actor"], wa["scope"], wa["source"].binding_id, context=AuditContext())
    assert (await deliver(wa, body("sent"))).status_code >= 400
    assert len(await rows(wa, access_db, "provider_receipts")) == 1


async def test_storage_failure_rolls_back_projection_before_ack(whatsapp, access_db, monkeypatch):
    wa = whatsapp
    persist = wa["statuses"].persist

    async def fail(tx, source, events):
        await persist(tx, source, events)
        raise RuntimeError("after projection fixture failure")

    monkeypatch.setattr(wa["statuses"], "persist", fail)
    assert (await deliver(wa, body("read", message=True))).status_code >= 500
    for table in (
        "provider_receipts",
        "provider_intents",
        "whatsapp_message_states",
        "whatsapp_status_facts",
        "whatsapp_status_observations",
    ):
        assert await rows(wa, access_db, table) == []


async def test_commit_failure_never_returns_ack_or_keeps_projection(whatsapp, access_db, monkeypatch):
    wa = whatsapp
    persist = wa["statuses"].persist

    async def flag(tx, source, events):
        await persist(tx, source, events)
        tx.session.info["e4_fail"] = True

    def fail(session):
        if session.info.get("e4_fail"):
            raise RuntimeError("commit fixture failure")

    monkeypatch.setattr(wa["statuses"], "persist", flag)
    event.listen(AsyncSession.sync_session_class, "before_commit", fail)
    try:
        assert (await deliver(wa, body("read"))).status_code >= 500
        assert await rows(wa, access_db, "whatsapp_status_facts") == []
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", fail)


async def test_opposing_batch_order_serializes_across_sources(whatsapp, access_db):
    wa = whatsapp
    replacement = await wa["ingress"].create(wa["actor"], wa["scope"], wa["request"], context=AuditContext())
    first = body("sent", message_id="a")
    first["entry"].extend(body("sent", message_id="z")["entry"])
    second = copy.deepcopy(first)
    second["entry"].reverse()
    responses = await asyncio.wait_for(
        asyncio.gather(deliver(wa, first), deliver(wa, second, client=1, source=replacement)), 10
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert len(await rows(wa, access_db, "whatsapp_status_facts")) == 2


async def test_status_rls_and_append_only_privileges(whatsapp, access_db):
    wa = whatsapp
    assert (await deliver(wa, body("read"))).status_code == 200
    uow = UnitOfWork(access_db[0])
    for scope in (
        wa["scope"].model_copy(update={"environment_id": None}),
        wa["scope"].model_copy(update={"tenant_id": uuid4()}),
        wa["scope"].model_copy(update={"project_id": uuid4()}),
    ):
        async with uow.open(scope) as tx:
            for table in ("whatsapp_message_states", "whatsapp_status_facts", "whatsapp_status_observations"):
                assert await tx.session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    async with uow.open(wa["scope"]) as tx:
        assert not await tx.session.scalar(
            text("SELECT has_table_privilege(current_user,'whatsapp_status_facts','UPDATE')")
        )
        assert not await tx.session.scalar(
            text("SELECT has_table_privilege(current_user,'whatsapp_status_facts','DELETE')")
        )
        assert not await tx.session.scalar(
            text("SELECT has_column_privilege(current_user,'whatsapp_message_states','recipient','UPDATE')")
        )
    assert (await read(wa)).progress == "read"


async def test_disabled_source_retains_readable_history(whatsapp):
    wa = whatsapp
    assert (await deliver(wa, body("read"))).status_code == 200
    await wa["ingress"].disable(wa["actor"], wa["scope"], wa["source"].id, context=AuditContext())
    assert (await read(wa)).progress == "read"
    assert (await deliver(wa, body("sent"))).status_code >= 400


async def test_authenticated_sdk_pages_history_and_enforces_scope_and_package(
    whatsapp, authenticated_client, headers, other_headers
):
    from firefly_weave.app import make_app
    from firefly_weave.sdk.client import WeaveClient

    wa = whatsapp
    assert (await deliver(wa, body("read", "sent", "delivered"))).status_code == 200
    auth_app = authenticated_client[0]._transport.app
    selected = make_app(wa["settings"], secrets=wa["secrets"], verifiers=auth_app.state.restart_verifiers)
    token = headers["Authorization"].removeprefix("Bearer ")
    async with selected.router.lifespan_context(selected):
        async with WeaveClient(
            "http://localhost", lambda: token, wa["scope"], transport=ASGITransport(selected)
        ) as sdk:
            state = await sdk.whatsapp_delivery_state(wa["source"].id, "wamid.out")
            assert state.progress == "read" and state.fact_count == 3
            first = await sdk.list_whatsapp_status_facts(wa["source"].id, state.id, limit=1)
            second = await sdk.list_whatsapp_status_facts(wa["source"].id, state.id, limit=1, cursor=first.next_cursor)
            assert first.items[0].id != second.items[0].id
        scope = wa["scope"]
        path = (
            f"/api/v1/tenants/{scope.tenant_id}/projects/{scope.project_id}"
            f"/environments/{scope.environment_id}/provider-sources/{wa['source'].id}/whatsapp-statuses"
        )
        async with AsyncClient(transport=ASGITransport(selected), base_url="http://localhost") as client:
            assert (
                await client.get(path, params={"message_id": "wamid.out"}, headers=other_headers)
            ).status_code == 403
            assert (
                await client.get(path, params=[("message_id", "one"), ("message_id", "two")], headers=headers)
            ).status_code == 422
            mismatch = await client.get(
                path + f"/{uuid4()}/facts", params={"cursor": first.next_cursor}, headers=headers
            )
            assert mismatch.status_code == 422
        unavailable = await authenticated_client[0].get(path, params={"message_id": "wamid.out"}, headers=headers)
        assert unavailable.status_code in (409, 422)


async def test_source_revision_rotation_retains_history_and_target_mapping_guards(whatsapp):
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.models import CatalogError

    wa = whatsapp
    assert (await deliver(wa, body("read"))).status_code == 200
    old = wa["connection"]
    request = ConnectionRequest.model_validate(
        old.model_dump(by_alias=True, include=set(ConnectionRequest.model_fields))
    )
    connections = wa["apps"][0].state.pyfly.context.get_bean(ConnectionService)
    updated = await connections.create_revision(wa["actor"], wa["scope"], request, context=AuditContext())
    replacement = await wa["ingress"].create(
        wa["actor"],
        wa["scope"],
        wa["request"].model_copy(update={"connection_revision_id": updated.id}),
        context=AuditContext(),
    )
    assert (await deliver(wa, body("read"), source=replacement)).status_code == 200
    state = await wa["statuses"].read(wa["actor"], wa["scope"], replacement.id, "wamid.out", context=AuditContext())
    assert state.fact_count == 1 and state.progress == "read"
    bad = wa["request"].model_copy(update={"policy": wa["request"].policy | {"account_id": "999"}})
    with pytest.raises(CatalogError):
        await wa["ingress"].create(wa["actor"], wa["scope"], bad, context=AuditContext())


async def test_native_duplicate_signature_and_query_and_oversized_body_are_denied(whatsapp, access_db):
    wa = whatsapp
    path = f"/provider-ingress/{wa['source'].id}"
    raw = json.dumps(body("read")).encode()
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    response = await wa["clients"][0].post(
        path, content=raw, headers=[("x-hub-signature-256", signature), ("X-Hub-Signature-256", signature)]
    )
    assert response.status_code == 401
    response = await wa["clients"][0].get(
        path,
        params=[
            ("hub.mode", "subscribe"),
            ("hub.verify_token", "verify-token"),
            ("hub.verify_token", "verify-token"),
            ("hub.challenge", "a"),
        ],
    )
    assert response.status_code == 401
    response = await wa["clients"][0].post(path, content=b" " * 1048577)
    assert response.status_code == 413
    assert await rows(wa, access_db, "provider_receipts") == []


async def test_revocation_during_required_secret_resolution_cannot_admit(whatsapp, access_db, monkeypatch):
    import threading

    wa = whatsapp
    entered, release = threading.Event(), threading.Event()
    original = wa["provider"].resolve

    def held(locator):
        assert locator == "app"
        entered.set()
        if not release.wait(5):
            raise TimeoutError("fixture timed out")
        return original(locator)

    monkeypatch.setattr(wa["provider"], "resolve", held)
    task = asyncio.create_task(deliver(wa, body("read")))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        await wa["ingress"].bindings.revoke(wa["actor"], wa["scope"], wa["source"].binding_id, context=AuditContext())
    finally:
        release.set()
    response = await task
    assert response.status_code in (401, 403, 409)
    assert await rows(wa, access_db, "provider_receipts") == []
    assert await rows(wa, access_db, "whatsapp_status_facts") == []


async def test_missing_required_secret_and_disabled_owner_fail_before_admission(whatsapp, access_db):
    wa = whatsapp
    wa["provider"].unavailable = {"app"}
    assert (await deliver(wa, body("read"))).status_code == 401
    wa["provider"].unavailable = set()
    async with access_db[1]() as session, session.begin():
        await session.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": wa["actor"].id})
    assert (await deliver(wa, body("read"))).status_code >= 400
    assert await rows(wa, access_db, "provider_receipts") == []
