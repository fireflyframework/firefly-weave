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
"""Real forward migration verification against task-owned PostgreSQL."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_connections import connections as connections
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration


async def test_email_migration_rls_and_fencing_schema(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value())
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0023_email")
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0023_email"
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_class WHERE relname LIKE 'email_%' "
                        "AND relkind='r' AND relrowsecurity AND relforcerowsecurity"
                    )
                )
                == 6
            )
            assert (
                await connection.scalar(text("SELECT has_table_privilege('weave_app','email_submissions','DELETE')"))
                is False
            )
    finally:
        await engine.dispose()


async def test_submission_exact_retry_and_crashed_attempt_never_resend(
    connections, access_db, provisioned, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.email import EmailSendRequest, MailProfile
    from firefly_weave.email.service import EmailService
    from firefly_weave.email.transport import SMTPResult

    service, _, actor, scope, request = connections
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="email_sender", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "fixture")
    revision = await service.create_revision(
        actor,
        scope,
        request,
        context=__import__("firefly_weave.access.audit", fromlist=["AuditContext"]).AuditContext(),
    )
    profile = MailProfile(
        host="127.0.0.1",
        port=25,
        tls="local_fixture",
        sender="s@example.com",
        allowed_recipients=("a@example.com",),
        private_networks=("127.0.0.0/8",),
    )

    class Transport:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            return SMTPResult("accepted", ("a@example.com",), ())

    transport = Transport()
    email = EmailService(service, transport)

    async def connection(*args):
        return profile

    async def credentials(*args):
        return profile, {}

    monkeypatch.setattr(email, "connection", connection)
    monkeypatch.setattr(email, "credentials", credentials)
    command = EmailSendRequest(
        request_id=uuid4(), connection_revision_id=revision.id, to=("a@example.com",), subject="hello", text="world"
    )
    first = await email.queue(actor, scope, command)
    assert (await email.queue(actor, scope, command)).id == first.id
    result = await email.execute(actor, scope, first.id)
    assert result.state == "accepted" and transport.calls == 1
    assert (await email.execute(actor, scope, first.id)).state == "accepted" and transport.calls == 1
    command = command.model_copy(update={"request_id": uuid4()})
    second = await email.queue(actor, scope, command)
    async with email.uow.open(scope) as tx:
        from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

        await RuntimeRepository(tx).execute(
            f"UPDATE email_submissions SET state='attempting',generation=1,lease_until=:expiry "
            f"WHERE {SCOPE} AND id=:id",
            id=second.id,
            expiry=datetime.now(UTC) - timedelta(seconds=1),
        )
    assert (await email.execute(actor, scope, second.id)).state == "unknown"
    assert transport.calls == 1


async def test_source_cursor_rejections_and_uidvalidity_reset(
    connections, access_db, provisioned, services, monkeypatch
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.email import MailProfile
    from firefly_weave.email.service import EmailService
    from firefly_weave.email.source import EmailSourceService
    from firefly_weave.email.transport import IMAPBatch, SMTPTransport
    from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    service, _, actor, scope, request = connections
    for role in ("email_manager", "operator"):
        await access_db[2].grant(provisioned[0], actor.id, Grant(role=role, scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "fixture")
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    profile = MailProfile(
        host="127.0.0.1",
        port=25,
        tls="local_fixture",
        sender="s@example.com",
        allowed_recipients=("a@example.com",),
        private_networks=("127.0.0.0/8",),
    )
    email = EmailService(service, SMTPTransport())

    async def connection(*args):
        return profile

    async def credentials(*args):
        return profile, {}

    monkeypatch.setattr(email, "connection", connection)
    monkeypatch.setattr(email, "credentials", credentials)
    raw = b"From: a@example.com\r\nTo: s@example.com\r\nMessage-ID: <initial@example.com>\r\n\r\ninitial"
    reply = (
        b"From: a@example.com\r\nTo: s@example.com\r\n"
        b"Message-ID: <reply@example.com>\r\nIn-Reply-To: <initial@example.com>\r\n"
        b"References: <initial@example.com>\r\n\r\nreply"
    )

    class IMAP:
        batch = IMAPBatch(7, 10, ())

        async def poll(self, *args):
            return self.batch

    imap = IMAP()
    graph = services(access_db[0])
    sources = EmailSourceService(email, imap, graph.resolve(RuntimeService), graph.resolve(SignalService))
    identifier = await sources.create(actor, scope, revision.id)
    assert (await sources.poll(actor, scope, identifier))["state"] == "baseline_recorded"

    async def release():
        async with email.uow.open(scope) as tx:
            await RuntimeRepository(tx).execute(
                f"UPDATE email_sources SET lease_until=NULL WHERE {SCOPE} AND id=:id", id=identifier
            )

    await release()
    imap.batch = IMAPBatch(7, 13, ((11, raw), (12, reply), (13, None)))
    assert (await sources.poll(actor, scope, identifier))["uid"] == 13
    async with email.uow.open(scope, mutation=False) as tx:
        db = RuntimeRepository(tx)
        rows = await db.rows(f"SELECT state FROM email_receipts WHERE {SCOPE} AND source_id=:source", source=identifier)
        assert {row["state"] for row in rows} == {"pending", "pending_correlation", "rejected"}
        messages = await db.rows(f"SELECT conversation_id FROM email_messages WHERE {SCOPE}")
        assert len(messages) == 2 and messages[0]["conversation_id"] == messages[1]["conversation_id"]
    await release()
    imap.batch = IMAPBatch(8, 100, ())
    assert (await sources.poll(actor, scope, identifier))["state"] == "resync_required"
    assert (await sources.poll(actor, scope, identifier))["state"] == "resync_required"
    await sources.rebaseline(actor, scope, identifier)
    assert (await sources.poll(actor, scope, identifier))["state"] == "baseline_recorded"
    # A second poller cannot enter transport, and a stale generation cannot commit admission.
    import asyncio

    from firefly_weave.definitions.models import CatalogError

    entered, resume = asyncio.Event(), asyncio.Event()

    async def blocked_poll(*args):
        entered.set()
        await resume.wait()
        return IMAPBatch(8, 101, ((101, raw),))

    imap.poll = blocked_poll
    await release()
    polling = asyncio.create_task(sources.poll(actor, scope, identifier))
    await entered.wait()
    assert (await sources.poll(actor, scope, identifier))["state"] == "leased"
    async with email.uow.open(scope) as tx:
        await RuntimeRepository(tx).execute(
            f"UPDATE email_sources SET generation=generation+1 WHERE {SCOPE} AND id=:id", id=identifier
        )
    resume.set()
    with pytest.raises(CatalogError):
        await polling
    async with email.uow.open(scope, mutation=False) as tx:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT last_uid FROM email_sources WHERE {SCOPE} AND id=:id", id=identifier
        )
        assert rows[0]["last_uid"] == 100
