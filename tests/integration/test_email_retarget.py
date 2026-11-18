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

"""An email source moves to a new version whose input accepts every message; it then starts runs as the mover."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_connections import connections as connections
from test_webhook_retarget import activate_version, person, publish_version, refused

pytestmark = pytest.mark.integration
MAIL = (
    b"From: a@example.com\r\nTo: s@example.com\r\nSubject: Order\r\nMessage-ID: <order-1@example.com>\r\n\r\n"
    b"Two boxes, please."
)


@pytest.fixture
async def mail(connections, worker_setup, worker_runtime_fixture, access_db, provisioned, services, monkeypatch):  # noqa: F811
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.email import MailProfile
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.email.service import EmailService
    from firefly_weave.email.source import EmailSourceService
    from firefly_weave.email.transport import IMAPTransport, SMTPTransport
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    service, _, admin_actor, scope, request = connections
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "fixture")
    revision = await service.create_revision(admin_actor, scope, request, context=AuditContext())
    _, _, _, creator, _, first, _ = worker_setup
    sessions, owner, access, _ = access_db
    admin, _ = provisioned
    await access.grant(admin, creator.id, Grant(role="email_manager", scope=scope))
    creator = await access.load_principal(creator.id)
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

    # The mail profile stands in for an email connection; using it still needs connection.bind, as it does for real.
    async def connection(tx, actor, identifier):
        email.connections.require(actor, tx.scope, "connection.bind", AuditContext())
        return profile

    monkeypatch.setattr(email, "connection", connection)
    graph = services(sessions)
    sources = EmailSourceService(email, IMAPTransport(), graph.resolve(RuntimeService), graph.resolve(SignalService))
    source_id = await sources.create(creator, scope, revision.id, activation_id=first.id)
    definitions = graph.resolve(DefinitionService)
    source = worker_runtime_fixture["source"]
    second = await activate_version(
        definitions,
        creator,
        scope,
        first,
        await publish_version(definitions, creator, scope, source, version="1.1.0"),
        expected=1,
    )
    mover = await person(access, admin, scope, "deployer", "operator", "viewer", "email_manager")
    return SimpleNamespace(
        email=email,
        profile=profile,
        sources=sources,
        source_id=source_id,
        connection_id=revision.id,
        definitions=definitions,
        source=source,
        scope=scope,
        creator=creator,
        mover=mover,
        first=first,
        second=second,
        owner=owner,
        access=access,
        admin=admin,
    )


async def retarget(env, activation_id, revision, *, actor=None):
    from firefly_weave.access.audit import AuditContext

    return await env.sources.retarget(
        actor or env.mover, env.scope, env.source_id, activation_id, revision, context=AuditContext()
    )


async def stored(env, sql, **values):
    async with env.owner() as session:
        return (await session.execute(text(sql), {"id": env.source_id, **values})).mappings().all()


async def change(env, sql, **values):
    async with env.owner() as session, session.begin():
        await session.execute(text(sql), {"id": env.source_id, **values})


async def test_a_retarget_repins_and_the_next_message_starts_the_new_version_as_the_mover(mail):
    env = mail
    from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

    await change(env, "UPDATE email_sources SET generation=4,lease_until=now()+interval '60 seconds' WHERE id=:id")
    moved = await retarget(env, env.second.id, 1)
    assert moved.model_dump() == {
        "id": env.source_id,
        "connection_revision_id": env.connection_id,
        "activation_id": env.second.id,
        "mailbox": "INBOX",
        "revision": 2,
        "disabled": False,
    }
    [row] = await stored(env, "SELECT principal_id,generation,lease_until FROM email_sources WHERE id=:id")
    assert (row["principal_id"], row["generation"], row["lease_until"]) == (env.mover.id, 5, None)
    async with env.email.uow.open(env.scope) as tx:
        [source] = await RuntimeRepository(tx).rows(
            f"SELECT * FROM email_sources WHERE {SCOPE} AND id=:id", id=env.source_id
        )
        await env.sources.admit(tx, source, "fixture:1", MAIL)
        [receipt] = await RuntimeRepository(tx).rows(
            f"SELECT id FROM email_receipts WHERE {SCOPE} AND transport_key='fixture:1'"
        )
    assert (await env.sources.dispatch(env.mover, env.scope, receipt["id"]))["state"] == "dispatched"
    [run] = await stored(env, "SELECT activation_id,principal_id FROM runs")
    assert (run["activation_id"], run["principal_id"]) == (env.second.id, env.mover.id)


async def test_a_poll_in_flight_when_the_source_moves_admits_nothing(mail, monkeypatch):
    env = mail
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.email.transport import IMAPBatch

    await change(env, "UPDATE email_sources SET uidvalidity=7,last_uid=1 WHERE id=:id")

    async def credentials(scope, actor, identifier):
        return env.profile, {}

    async def poll(profile, credentials, after_uid, **limits):
        # The source moves while the poll reads the mailbox.
        await retarget(env, env.second.id, 1)
        return IMAPBatch(uidvalidity=7, highest_uid=2, messages=((2, MAIL),))

    monkeypatch.setattr(env.email, "credentials", credentials)
    monkeypatch.setattr(env.sources.imap, "poll", poll)
    with pytest.raises(CatalogError):
        await env.sources.poll(env.creator, env.scope, env.source_id)
    assert await stored(env, "SELECT id FROM email_receipts") == []
    [row] = await stored(env, "SELECT last_uid,revision FROM email_sources WHERE id=:id")
    assert (row["last_uid"], row["revision"]) == (1, 2)


async def test_the_audit_names_activations_and_principals_only(mail):
    env = mail
    await retarget(env, env.second.id, 1)
    [audit] = await stored(env, "SELECT target,event FROM access_audit WHERE action='email.source.retarget'")
    assert audit["target"] == str(env.source_id)
    assert audit["event"]["details"] == {
        "kind": "email",
        "from_activation_id": str(env.first.id),
        "to_activation_id": str(env.second.id),
        "from_principal_id": str(env.creator.id),
        "to_principal_id": str(env.mover.id),
    }


async def test_an_input_a_message_doesnt_satisfy_is_refused(mail):
    env = mail
    order = {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}
    changed = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="2.0.0", input_schema=order),
        expected=2,
    )
    assert await refused(retarget(env, changed.id, 1)) == (409, "WV-TRIGGER-SCHEMA-CHANGED")
    fitting = {
        "type": "object",
        "required": ["sender", "subject"],
        "properties": {"sender": {"type": "string"}, "subject": {"type": "string"}},
    }
    accepting = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(
            env.definitions, env.creator, env.scope, env.source, version="2.1.0", input_schema=fitting
        ),
        expected=3,
    )
    assert (await retarget(env, accepting.id, 1)).activation_id == accepting.id


async def test_a_stale_or_missing_revision_another_workflow_and_a_resync_are_refused(mail):
    env = mail
    assert await refused(retarget(env, env.second.id, None)) == (422, "WV-ETAG")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-REVISION")
    other = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="1.0.0", name="other-flow"),
    )
    assert await refused(retarget(env, other.id, 1)) == (409, "WV-TRIGGER-WORKFLOW")
    assert await refused(retarget(env, uuid4(), 1)) == (404, "WV-NOT-FOUND")
    await change(env, "UPDATE email_sources SET state='resync_required' WHERE id=:id")
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-TRIGGER-DISABLED")


async def test_the_mover_needs_what_creating_an_email_source_needs_and_may_start_runs(mail):
    env = mail
    from firefly_weave.access.authorization import AccessDenied

    for roles in (
        ("deployer", "operator", "viewer"),
        ("operator", "viewer", "email_manager"),
        ("deployer", "viewer", "email_manager"),
    ):
        lacking = await person(env.access, env.admin, env.scope, *roles)
        with pytest.raises(AccessDenied):
            await retarget(env, env.second.id, 1, actor=lacking)
    [row] = await stored(env, "SELECT revision,principal_id FROM email_sources WHERE id=:id")
    assert (row["revision"], row["principal_id"]) == (1, env.creator.id)


async def test_a_source_of_a_test_activation_never_moves(mail):
    env = mail
    from test_webhook_retarget import marked_test

    test_activation = await marked_test(env.owner, env.second)
    await change(env, "UPDATE email_sources SET activation_id=:a WHERE id=:id", a=test_activation)
    assert await refused(retarget(env, env.second.id, 1)) == (404, "WV-NOT-FOUND")
    [row] = await stored(env, "SELECT revision,activation_id FROM email_sources WHERE id=:id")
    assert (row["revision"], row["activation_id"]) == (1, test_activation)
