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

"""Telegram native durable inbox integration; PostgreSQL runs only in an assigned slot."""

import asyncio
import json
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.providers import ProviderSourceRequest, provider_schema_digest
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.providers.service import ProviderIngressService

IDENTITY = "firefly-weave:weave-telegram:firefly_weave.connectors.telegram:package"
SECRET = "e5_native_fixture_webhook"


def test_telegram_provider_uses_native_verifier_port():
    from firefly_weave.providers.ports import ProviderSourceValidator, ProviderVerifier
    from firefly_weave.providers.telegram import TelegramVerifier

    verifier = TelegramVerifier(None)
    assert isinstance(verifier, ProviderVerifier)
    assert isinstance(verifier, ProviderSourceValidator)


def update(identifier=73, *, bot=False, chat=-987):
    return {
        "update_id": identifier,
        "message": {
            "message_id": 9,
            "date": 1790800000,
            "chat": {"id": chat, "type": "supergroup"},
            "from": {"id": 42, "is_bot": bot},
            "text": "hello",
        },
    }


@pytest.fixture
async def telegram_inbox(worker_setup, access_db, provisioned, monkeypatch, scheduler_url):
    from firefly_weave.access.models import Grant
    from firefly_weave.app import make_app
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.telegram import package
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.settings import Settings

    _, _, _, actor, scope, activation, _ = worker_setup
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_E5_WEBHOOK", SECRET)
    # Bot-token grant exists, but its unavailable provider value must not block ingress.
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()},
        (
            SecretGrant(scope, "e5-webhook", "env", "WEAVE_CONNECTION_SECRET_E5_WEBHOOK"),
            SecretGrant(scope, "e5-token", "env", "WEAVE_CONNECTION_SECRET_E5_UNSET_TOKEN"),
        ),
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
        context = apps[0].state.pyfly.context
        published = await context.get_bean(DefinitionService).publish(
            actor,
            scope,
            "Connector",
            json.dumps(package.metadata.model.manifest.model_dump(by_alias=True)),
            "json",
            "telegram-native",
            context=AuditContext(),
        )
        connection = await context.get_bean(ConnectionService).create_revision(
            actor,
            scope,
            ConnectionRequest(
                name="telegram-native",
                connector_version_id=published.id,
                config={"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987"]},
                secretRef={"botToken": "e5-token", "webhookSecret": "e5-webhook"},
                allowed_destinations=("https://api.telegram.org",),
            ),
            context=AuditContext(),
        )
        service = context.get_bean(ProviderIngressService)
        request = ProviderSourceRequest(
            name="telegram-native",
            provider="telegram",
            package="firefly-weave",
            package_version=package.metadata.model.distribution_version,
            schema_digest=provider_schema_digest(
                package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds
            ),
            connection_revision_id=connection.id,
            policy=connection.config,
            kind="run",
            activation_id=activation.id,
        )
        source = await service.create(actor, scope, request, context=AuditContext())
        yield {
            "apps": apps,
            "clients": clients,
            "service": service,
            "source": source,
            "request": request,
            "scope": scope,
            "actor": actor,
            "connection": connection,
            "dispatchers": [app.state.pyfly.context.get_bean(ProviderDispatcher) for app in apps],
        }


async def deliver(inbox, document=None, *, client=0, headers=None, raw=None):
    return await inbox["clients"][client].post(
        f"/provider-ingress/{inbox['source'].id}",
        content=raw if raw is not None else json.dumps(document or update()).encode(),
        headers=headers if headers is not None else {"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )


async def rows(inbox, access_db, table="provider_receipts"):
    from firefly_weave.persistence.uow import UnitOfWork

    assert table in {"provider_receipts", "runs"}
    async with UnitOfWork(access_db[0]).open(inbox["scope"]) as tx:
        return list((await tx.session.execute(text(f"SELECT * FROM {table}"))).mappings())


@pytest.mark.integration
async def test_native_two_apps_deduplicate_one_update_and_dispatch(telegram_inbox, access_db):
    a, b = await asyncio.gather(deliver(telegram_inbox), deliver(telegram_inbox, client=1))
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    receipts = await rows(telegram_inbox, access_db)
    assert len(receipts) == 1
    dispatched = await asyncio.gather(
        *(
            dispatcher.dispatch_one(telegram_inbox["scope"], receipts[0]["id"])
            for dispatcher in telegram_inbox["dispatchers"]
        )
    )
    assert dispatched[0] == dispatched[1] and dispatched[0].state == "dispatched"
    assert len(await rows(telegram_inbox, access_db, "runs")) == 1


@pytest.mark.integration
async def test_native_security_and_ignored_receipts(telegram_inbox, access_db):
    for headers, raw, status in [
        ({}, b"bad-json", 401),
        ([("X-Telegram-Bot-Api-Secret-Token", SECRET), ("x-telegram-bot-api-secret-token", SECRET)], b"{}", 401),
        ({"X-Telegram-Bot-Api-Secret-Token": SECRET}, b"{}", 422),
        ({"X-Telegram-Bot-Api-Secret-Token": SECRET}, b"x" * 1048577, 413),
    ]:
        response = await deliver(telegram_inbox, headers=headers, raw=raw)
        assert response.status_code == status
    assert (await deliver(telegram_inbox, update(chat=-999))).status_code == 401
    assert await rows(telegram_inbox, access_db) == []
    assert (await deliver(telegram_inbox, update(bot=True))).status_code == 200
    receipts = await rows(telegram_inbox, access_db)
    assert len(receipts) == 1 and receipts[0]["state"] == "ignored"
    assert (
        await telegram_inbox["dispatchers"][0].dispatch_one(telegram_inbox["scope"], receipts[0]["id"])
    ).state == "ignored"
    assert await rows(telegram_inbox, access_db, "runs") == []


@pytest.mark.integration
async def test_native_conflict_out_of_order_edits_and_replacement_sources(telegram_inbox, access_db):
    assert (await deliver(telegram_inbox)).status_code == 200
    changed = update()
    changed["message"]["text"] = "changed"
    assert (await deliver(telegram_inbox, changed)).status_code == 409
    assert (await deliver(telegram_inbox, update(72))).status_code == 200
    assert (await deliver(telegram_inbox, {"update_id": 74, "edited_message": update()["message"]})).status_code == 200
    replacement = await telegram_inbox["service"].create(
        telegram_inbox["actor"],
        telegram_inbox["scope"],
        telegram_inbox["request"].model_copy(update={"name": "telegram-replacement"}),
        context=AuditContext(),
    )
    assert (await deliver({**telegram_inbox, "source": replacement})).status_code == 200
    receipts = await rows(telegram_inbox, access_db)
    assert len(receipts) == 4 and sum(row["state"] == "ignored" for row in receipts) == 1


@pytest.mark.integration
@pytest.mark.parametrize("revocation", ["source", "principal", "binding"])
async def test_native_current_authority_blocks_after_ack(telegram_inbox, access_db, revocation):
    assert (await deliver(telegram_inbox)).status_code == 200
    receipt = (await rows(telegram_inbox, access_db))[0]
    if revocation == "source":
        await telegram_inbox["service"].disable(
            telegram_inbox["actor"], telegram_inbox["scope"], telegram_inbox["source"].id, context=AuditContext()
        )
    else:
        sql, identifier = (
            ("UPDATE principals SET active=false WHERE id=:id", telegram_inbox["actor"].id)
            if revocation == "principal"
            else (
                "UPDATE connection_source_bindings SET revoked=true WHERE id=:id",
                telegram_inbox["source"].binding_id,
            )
        )
        async with access_db[1]() as session, session.begin():
            await session.execute(text(sql), {"id": identifier})
    result = await telegram_inbox["dispatchers"][0].dispatch_one(telegram_inbox["scope"], receipt["id"])
    assert result.state == "blocked" and await rows(telegram_inbox, access_db, "runs") == []


@pytest.mark.integration
async def test_native_commit_failure_has_no_ack_or_partial_receipt(telegram_inbox, access_db):
    def fail(session):
        if any(
            "INSERT INTO provider_receipts" in str(statement) for statement in session.info.get("e5_statements", [])
        ):
            raise RuntimeError("e5 controlled commit failure")

    def capture(state):
        state.session.info.setdefault("e5_statements", []).append(state.statement)

    event.listen(AsyncSession.sync_session_class, "do_orm_execute", capture)
    event.listen(AsyncSession.sync_session_class, "before_commit", fail)
    try:
        assert (await deliver(telegram_inbox)).status_code >= 500
    finally:
        event.remove(AsyncSession.sync_session_class, "before_commit", fail)
        event.remove(AsyncSession.sync_session_class, "do_orm_execute", capture)
    assert await rows(telegram_inbox, access_db) == []


@pytest.mark.integration
async def test_native_exact_source_policy_and_scope(telegram_inbox, access_db):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    for policy in [
        {**telegram_inbox["request"].policy, "account_id": "999"},
        {**telegram_inbox["request"].policy, "allowed_chat_ids": []},
    ]:
        with pytest.raises(CatalogError):
            await telegram_inbox["service"].create(
                telegram_inbox["actor"],
                telegram_inbox["scope"],
                telegram_inbox["request"].model_copy(update={"policy": policy}),
                context=AuditContext(),
            )
    assert (await deliver(telegram_inbox)).status_code == 200
    receipt = (await rows(telegram_inbox, access_db))[0]
    with pytest.raises(AccessDenied):
        await telegram_inbox["service"].read(
            telegram_inbox["actor"],
            telegram_inbox["scope"].model_copy(update={"environment_id": uuid4()}),
            receipt["id"],
            receipt=True,
            context=AuditContext(),
        )
