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

"""Signed raw-byte ingress and atomic PostgreSQL receipt acceptance."""

import pytest

pytestmark = pytest.mark.integration


async def test_bad_signature_creates_no_run(client, access_db):
    from uuid import uuid4

    from sqlalchemy import text

    async with access_db[1]() as session:
        before = await session.scalar(text("SELECT count(*) FROM runs"))
    response = await client.post(
        f"/webhooks/{uuid4()}", content=b'{"customerId":"123"}', headers={"X-Weave-Signature": "invalid"}
    )
    assert response.status_code in {401, 403}
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == before


def test_signature_binds_exact_bytes_timestamp_and_event():
    import hashlib
    import hmac
    import time

    from firefly_weave.triggers.webhooks import authenticate

    stamp = str(int(time.time()))
    raw = b'{"eventId":"event-1","payload":{"value":1}}'
    headers = {
        "x-weave-timestamp": stamp,
        "x-weave-event-id": "event-1",
        "x-weave-signature": hmac.new(b"secret", stamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
    }
    assert authenticate(b"secret", raw, headers, tolerance=300) == "event-1"
    from firefly_weave.definitions.models import CatalogError

    for invalid in (b'{ "value":1}', b'{"value":2}'):
        with pytest.raises(CatalogError):
            authenticate(b"secret", invalid, headers, tolerance=300)
    with pytest.raises(CatalogError):
        authenticate(b"secret", raw, headers | {"x-weave-timestamp": "1"}, tolerance=300)
    assert (
        authenticate(b"secret", raw, {k: v for k, v in headers.items() if k != "x-weave-event-id"}, tolerance=300)
        == "event-1"
    )


@pytest.fixture
async def webhook_setup(worker_setup, access_db, services, monkeypatch):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.triggers.models import TriggerRequest
    from firefly_weave.triggers.service import WebhookService

    _, _, _, actor, scope, activation, _ = worker_setup
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_WEBHOOK", "webhook-canary")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "webhook", "env", "WEAVE_CONNECTION_SECRET_WEBHOOK"),)
    )
    service = services(access_db[0], secrets=secrets).resolve(WebhookService)
    trigger = await service.create(
        actor,
        scope,
        TriggerRequest(
            name="start",
            kind="run",
            activation_id=activation.id,
            secret_ref="webhook",
            payload_schema={"type": "integer"},
        ),
        context=AuditContext(),
    )
    return service, trigger, scope, actor


def signed(raw, event="event-1", stamp=None):
    import hashlib
    import hmac
    import time

    stamp = stamp or str(int(time.time()))
    return {
        "x-weave-timestamp": stamp,
        "x-weave-event-id": event,
        "x-weave-signature": hmac.new(b"webhook-canary", stamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
    }


async def test_atomic_receipt_duplicate_and_conflict(webhook_setup, access_db):
    import asyncio

    from sqlalchemy import text

    from firefly_weave.definitions.models import CatalogError

    service, trigger, _, _ = webhook_setup
    raw = b'{"eventId":"event-1","payload":3}'
    first, duplicate = await asyncio.gather(*(service.receive(trigger.id, raw, signed(raw)) for _ in range(2)))
    assert first == duplicate
    assert (await service.receive(trigger.id, raw, signed(raw))) == first  # lost response after commit
    with pytest.raises(CatalogError) as error:
        await service.receive(
            trigger.id, b'{"eventId":"event-1","payload":4}', signed(b'{"eventId":"event-1","payload":4}')
        )
    assert error.value.status == 409
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trigger_receipts")) == 1
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1


async def test_invalid_webhook_never_creates_receipt(webhook_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.definitions.models import CatalogError

    service, trigger, _, _ = webhook_setup
    for raw, headers in [
        (b"3", {}),
        (b"4", signed(b"3")),
        (b"3", signed(b"3", stamp="1")),
        (b'{"eventId":"event-1","payload":{}}', signed(b'{"eventId":"event-1","payload":{}}')),
        (b" " * 1048577, signed(b" " * 1048577)),
    ]:
        with pytest.raises(CatalogError):
            await service.receive(trigger.id, raw, headers)
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trigger_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


async def test_event_identity_is_signed_and_distinct_events_remain_distinct(webhook_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.definitions.models import CatalogError

    service, trigger, _, _ = webhook_setup
    raw = b'{"eventId":"event-1","payload":3}'
    first = await service.receive(trigger.id, raw, signed(raw))
    with pytest.raises(CatalogError):
        await service.receive(trigger.id, raw, signed(raw) | {"x-weave-event-id": "event-2"})
    second_raw = b'{"eventId":"event-2","payload":3}'
    second = await service.receive(trigger.id, second_raw, signed(second_raw, event="event-2"))
    assert first.run_id != second.run_id
    for invalid in [
        b'{"eventId":"event-3","eventId":"event-3","payload":3}',
        b'{"eventId":"event-3","payload":3,"extra":1}',
        b'{"eventId":"event-3","payload":NaN}',
        b'{"eventId":null,"payload":3}',
        b'{"eventId":"","payload":3}',
        b'{"eventId":"event-3","payload":' + b"[" * 35 + b"0" + b"]" * 35 + b"}",
    ]:
        with pytest.raises(CatalogError):
            await service.receive(trigger.id, invalid, signed(invalid, event="event-3"))
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 2


async def test_native_ingress_route_preserves_identity_and_rejects_duplicate_headers(
    webhook_setup, access_db, scheduler_url
):
    from httpx import ASGITransport, AsyncClient

    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    service, trigger, _, _ = webhook_setup
    app = make_app(
        Settings(
            scheduler_enabled=False,
            database_url=access_db[3].render_as_string(hide_password=False),
            scheduler_database_url=scheduler_url,
        ),
        secrets=service.secrets,
    )
    raw = b'{"eventId":"event-1","payload":3}'
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://local") as client,
    ):
        response = await client.post(
            f"/webhooks/{trigger.id}",
            content=raw,
            headers={k: v for k, v in signed(raw).items() if k != "x-weave-event-id"}
            | {"X-Forwarded-Host": "untrusted", "X-Weave-Tenant": "other", "X-Forwarded-For": "169.254.169.254"},
        )
        assert response.status_code == 202, response.text
        repeated = await client.post(f"/webhooks/{trigger.id}", content=raw, headers=signed(raw))
        assert repeated.json() == response.json()
        duplicate = list(signed(raw).items()) + [("X-Weave-Signature", "invalid")]
        assert (await client.post(f"/webhooks/{trigger.id}", content=raw, headers=duplicate)).status_code == 401
        duplicate_event = list(signed(raw).items()) + [("X-Weave-Event-ID", "event-1")]
        assert (await client.post(f"/webhooks/{trigger.id}", content=raw, headers=duplicate_event)).status_code == 401
        assert (await client.get(f"/webhooks/{trigger.id}")).status_code == 401
        assert (await client.post(f"/webhooks/{trigger.id}/disable")).status_code == 401


async def test_trigger_disable_and_principal_revocation_are_current(webhook_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    service, trigger, scope, actor = webhook_setup
    raw = b'{"eventId":"event-1","payload":3}'
    receipt = await service.receive(trigger.id, raw, signed(raw))
    assert receipt.run_id
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": actor.id})
    with pytest.raises((CatalogError, AccessDenied)):
        await service.receive(trigger.id, raw, signed(raw))
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE principals SET active=true WHERE id=:id"), {"id": actor.id})
    await service.disable(actor, scope, trigger.id, context=AuditContext())
    with pytest.raises(CatalogError):
        await service.receive(trigger.id, raw, signed(raw))


async def test_trigger_lookup_role_has_only_minimal_cross_scope_access(webhook_setup, access_db):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    _, trigger, _, _ = webhook_setup
    async with access_db[0]() as session:
        assert (await session.execute(text("SELECT id FROM trigger_routes"))).all() == []
        row = (
            (await session.execute(text("SELECT * FROM weave_trigger_route(:id)"), {"id": trigger.id})).mappings().one()
        )
        assert set(row) == {
            "id",
            "tenant_id",
            "project_id",
            "environment_id",
            "secret_ref",
            "tolerance_seconds",
            "max_body_bytes",
        }
        with pytest.raises(DBAPIError):
            await session.execute(text("SET ROLE weave_trigger_reader"))
    async with access_db[1]() as session:
        owner = (
            await session.execute(
                text(
                    "SELECT r.rolsuper,r.rolbypassrls,r.rolcanlogin FROM pg_proc p "
                    "JOIN pg_roles r ON r.oid=p.proowner WHERE p.proname='weave_trigger_route'"
                )
            )
        ).one()
        assert tuple(owner) == (False, False, False)


async def test_signal_trigger_commits_receipt_with_signal(webhook_setup, access_db):
    import json

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.triggers.models import TriggerRequest

    service, _, scope, actor = webhook_setup
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "signal-trigger", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "kind": "signal",
                    "id": "wait",
                    "name": "approved",
                    "timeoutSeconds": 30,
                    "payloadSchema": {"type": "boolean"},
                }
            ],
            "output": {"ref": "/steps/wait/output"},
        },
    }
    version = await service.runtime.definitions.publish(
        actor, scope, "Workflow", json.dumps(document), "json", "signal-flow", context=AuditContext()
    )
    activation = await service.runtime.definitions.activate(
        actor,
        scope,
        ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=scope),
        "signal-activation",
        context=AuditContext(),
    )
    run = await service.runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), "signal-run", context=AuditContext()
    )
    trigger = await service.create(
        actor,
        scope,
        TriggerRequest(
            name="signal",
            kind="signal",
            run_id=run.id,
            signal="approved",
            secret_ref="webhook",
            payload_schema={"type": "boolean"},
        ),
        context=AuditContext(),
    )
    raw = b'{"eventId":"event-1","payload":true}'
    receipt = await service.receive(trigger.id, raw, signed(raw))
    assert receipt.run_id == run.id and receipt.signal_id is not None
    assert await service.receive(trigger.id, raw, signed(raw)) == receipt
    final = await service.runtime.read(actor, scope, run.id, context=AuditContext())
    assert final.state.status == "succeeded" and final.state.output is True
