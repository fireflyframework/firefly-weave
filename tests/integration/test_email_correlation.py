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
"""Real scoped token authority and revocation with untrusted threading headers."""

from uuid import UUID, uuid4

import pytest
from test_connections import connections as connections
from test_human_tasks import (
    author as author,
)
from test_human_tasks import (
    headers as headers,
)
from test_human_tasks import (
    human_identity as human_identity,
)
from test_human_tasks import (
    human_run as human_run,
)

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.contracts.email import EmailSendRequest, EmailTokenRequest, MailProfile
from firefly_weave.email.service import EmailService
from firefly_weave.email.source import EmailSourceService
from firefly_weave.email.transport import IMAPTransport, SMTPTransport
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService

pytestmark = pytest.mark.integration


async def test_token_route_rechecks_revocation_before_signal(
    connections, access_db, provisioned, services, monkeypatch, human_run
):
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
        routing_domain="mail.example.com",
    )
    email = EmailService(service, SMTPTransport())

    async def connection(*args):
        return profile

    monkeypatch.setattr(email, "connection", connection)
    graph = services(access_db[0])
    sources = EmailSourceService(email, IMAPTransport(), graph.resolve(RuntimeService), graph.resolve(SignalService))
    source_id = await sources.create(actor, scope, revision.id)
    command = EmailSendRequest(
        request_id=uuid4(), connection_revision_id=revision.id, to=("a@example.com",), subject="Expense", text="review"
    )
    submission = await email.queue(actor, scope, command)
    run_id = UUID(human_run[0])
    async with email.uow.open(scope) as tx:
        db = RuntimeRepository(tx)
        await db.execute(
            f"UPDATE email_conversations SET run_id=:run WHERE {SCOPE} AND id=:id",
            id=submission.conversation_id,
            run=run_id,
        )
        rows = await db.rows(f"SELECT * FROM email_sources WHERE {SCOPE} AND id=:id", id=source_id)
        source = rows[0]
    token = await sources.issue_token(
        actor,
        scope,
        EmailTokenRequest(
            source_id=source_id, conversation_id=submission.conversation_id, run_id=run_id, signal="followup"
        ),
    )
    raw = f"From: a@example.com\r\nTo: {token.reply_address}\r\nMessage-ID: <reply@example.com>\r\n\r\nreply".encode()
    async with email.uow.open(scope) as tx:
        await sources.admit(tx, source, "fixture:1", raw)
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM email_receipts WHERE {SCOPE} AND transport_key='fixture:1'"
        )
        receipt = rows[0]
        assert (
            receipt["state"] == "authorized" and receipt["run_id"] == run_id and receipt["correlation_id"] == token.id
        )
    await sources.revoke_token(actor, scope, token.id)
    assert (await sources.dispatch(actor, scope, receipt["id"]))["state"] == "blocked"
    async with email.uow.open(scope) as tx:
        await sources.admit(tx, source, "fixture:2", raw)
        rows = await RuntimeRepository(tx).rows(
            f"SELECT state FROM email_receipts WHERE {SCOPE} AND transport_key='fixture:2'"
        )
        assert rows[0]["state"] == "pending_correlation"
        assert not await RuntimeRepository(tx).rows(
            f"SELECT id FROM signal_receipts WHERE {SCOPE} AND run_id=:run", run=run_id
        )


async def test_mail_approval_reply_and_followup_single_run(
    connections, access_db, provisioned, services, monkeypatch, author, headers, project_url, env_url
):
    import asyncio
    from types import SimpleNamespace

    import test_human_tasks

    from firefly_weave.email.transport import MailPolicy

    # Reuse the published human-task fixture with a real subsequent signal wait.
    source = test_human_tasks.SOURCE.replace(
        "  output: {ref: /steps/review/output}",
        "    - id: followup\n      kind: signal\n      name: followup\n"
        "      timeoutSeconds: 300\n      payloadSchema: {}\n"
        "  output: {ref: /steps/followup/output}",
    )
    monkeypatch.setattr(test_human_tasks, "SOURCE", source)
    template_run, _ = await test_human_tasks.human_run.__wrapped__(
        author, access_db, provisioned, headers, project_url, env_url, SimpleNamespace(param=False)
    )
    service, _, actor, scope, connection_request = connections
    for role in ("email_manager", "operator"):
        await access_db[2].grant(provisioned[0], actor.id, Grant(role=role, scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "fixture")
    revision = await service.create_revision(actor, scope, connection_request, context=AuditContext())
    accepted = []

    async def smtp(reader, writer):
        writer.write(b"220 local\r\n")
        await writer.drain()
        while line := await reader.readline():
            if line.startswith(b"EHLO") or line.startswith(b"MAIL") or line.startswith(b"RCPT"):
                writer.write(b"250 ok\r\n")
            elif line.startswith(b"DATA"):
                writer.write(b"354 send\r\n")
                await writer.drain()
                parts = []
                while (line := await reader.readline()) != b".\r\n":
                    parts.append(line)
                accepted.append(b"".join(parts))
                writer.write(b"250 accepted\r\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(smtp, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        profile = MailProfile(
            host="127.0.0.1",
            port=port,
            tls="local_fixture",
            sender="s@example.com",
            allowed_recipients=("a@example.com",),
            private_networks=("127.0.0.0/8",),
            routing_domain="mail.example.com",
        )
        email = EmailService(
            service,
            SMTPTransport(
                MailPolicy(private_networks=("127.0.0.0/8",), allowed_ports=(port,), allow_local_fixture=True)
            ),
        )

        async def connection(*args):
            return profile

        async def credentials(*args):
            return profile, {}

        monkeypatch.setattr(email, "connection", connection)
        monkeypatch.setattr(email, "credentials", credentials)
        graph = services(access_db[0])
        sources = EmailSourceService(
            email, IMAPTransport(), graph.resolve(RuntimeService), graph.resolve(SignalService)
        )
        async with email.uow.open(scope, mutation=False) as tx:
            template_rows = await RuntimeRepository(tx).rows(
                f"SELECT activation_id FROM runs WHERE {SCOPE} AND id=:id", id=UUID(template_run)
            )
        source_id = await sources.create(actor, scope, revision.id, activation_id=template_rows[0]["activation_id"])
        initial = (
            b"From: a@example.com\r\nTo: s@example.com\r\n"
            b"Message-ID: <expense@example.com>\r\nSubject: Expense\r\n\r\nReview"
        )
        async with email.uow.open(scope) as tx:
            db = RuntimeRepository(tx)
            source_row = (await db.rows(f"SELECT * FROM email_sources WHERE {SCOPE} AND id=:id", id=source_id))[0]
            await sources.admit(tx, source_row, "expense:1", initial)
            receipt = (await db.rows(f"SELECT * FROM email_receipts WHERE {SCOPE} AND transport_key='expense:1'"))[0]
        dispatched = await sources.dispatch(actor, scope, receipt["id"])
        run_id = UUID(dispatched["run_id"])
        tasks = (await author[0].get(env_url + "/human-tasks", headers=headers)).json()["items"]
        task = next(t for t in tasks if t["run_id"] == str(run_id))
        claimed = await test_human_tasks.mutate(
            author[0], headers, env_url, task, "claim", {"expected_revision": 1}, "email-claim"
        )
        assert claimed.status_code == 200, claimed.text
        completed = await test_human_tasks.mutate(
            author[0],
            headers,
            env_url,
            task,
            "complete",
            {"expected_revision": 2, "decision": "approve", "data": {"note": "Approved"}},
            "email-complete",
        )
        assert completed.status_code == 200, completed.text
        async with email.uow.open(scope, mutation=False) as tx:
            message = (
                await RuntimeRepository(tx).rows(
                    f"SELECT * FROM email_messages WHERE {SCOPE} AND id=:id", id=receipt["message_id"]
                )
            )[0]
            conversation_id = message["conversation_id"]
        from firefly_weave.contracts.email import EmailReplyRequest

        submission = await email.queue(
            actor,
            scope,
            EmailReplyRequest(
                request_id=uuid4(),
                connection_revision_id=revision.id,
                parent_message_id=receipt["message_id"],
                text="Approved",
            ),
            conversation_id,
        )
        assert (await email.execute(actor, scope, submission.id)).state == "accepted"
        from firefly_weave.email.mime import parse_message

        reply = parse_message(accepted[0])
        assert reply.in_reply_to == "<expense@example.com>"
        token = await sources.issue_token(
            actor,
            scope,
            EmailTokenRequest(source_id=source_id, conversation_id=conversation_id, run_id=run_id, signal="followup"),
        )
        followup = (
            f"From: a@example.com\r\nTo: {token.reply_address}\r\n"
            f"Message-ID: <followup@example.com>\r\nIn-Reply-To: {reply.message_id}\r\n"
            f"References: <expense@example.com> {reply.message_id}\r\n\r\nThanks"
        ).encode()
        async with email.uow.open(scope) as tx:
            await sources.admit(tx, source_row, "expense:2", followup)
            followup_receipt = (
                await RuntimeRepository(tx).rows(
                    f"SELECT * FROM email_receipts WHERE {SCOPE} AND transport_key='expense:2'"
                )
            )[0]
        assert (await sources.dispatch(actor, scope, followup_receipt["id"]))["run_id"] == str(run_id)
        assert (await sources.dispatch(actor, scope, followup_receipt["id"]))["state"] == "dispatched"
        view = (await author[0].get(f"{env_url}/runs/{run_id}", headers=headers)).json()
        assert view["state"]["status"] == "succeeded"
        async with email.uow.open(scope, mutation=False) as tx:
            rows = await RuntimeRepository(tx).rows(
                f"SELECT id FROM signal_receipts WHERE {SCOPE} AND run_id=:run", run=run_id
            )
            assert len(rows) == 1
        assert len(accepted) == 1
