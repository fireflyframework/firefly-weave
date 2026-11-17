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

"""Alert and approval mail on real PostgreSQL, through the email service with a recording mail server: who gets mail
and as whom, the budgets, unclear and failed sends, and approval requests once per revision."""

import json
from datetime import timedelta
from email import message_from_bytes
from uuid import uuid4

import pytest
from test_alert_loop_integration import create, draft
from test_alert_settings_api import add_connection
from test_operations_statistics import owner
from test_operations_summary import add_incident
from test_worker_fleet_facts import add_target, clock, principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant
from firefly_weave.access.scheduler import due_alert_scopes
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.alerts import (
    AlertChannelDraft,
    AlertRecipientConsentDraft,
    AlertSettingsUpdate,
    MyAlertAddressUpdate,
)
from firefly_weave.contracts.email import MailProfile
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.service import EmailService, unavailable
from firefly_weave.email.transport import SMTPResult
from firefly_weave.operations.alert_channels import (
    AlertAddressService,
    AlertChannelService,
    AlertRecipientConsentService,
    AlertSettingsService,
    address_hash,
)
from firefly_weave.operations.alert_loop import AlertLoop
from firefly_weave.operations.alert_notifications import AlertNotifier, scan_approvals
from firefly_weave.operations.alert_rules import AlertRuleService
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()
ADDRESSES = [f"person{index}@example.org" for index in range(10)] + ["oncall@vendor.example"]


class MailServer:
    """Records each message; answers with the next queued outcome, then accepts."""

    def __init__(self) -> None:
        self.sent: list[tuple[tuple[str, ...], bytes]] = []
        self.outcomes: list[str] = []

    async def send(self, profile, recipients, raw, credentials):
        self.sent.append((tuple(recipients), raw))
        state = self.outcomes.pop(0) if self.outcomes else "accepted"
        return SMTPResult(state, tuple(recipients) if state == "accepted" else (), ())


@pytest.fixture
async def mail(operations_case, services, provisioned, monkeypatch):
    case = operations_case
    graph = services(case.sessions)
    case.channels = graph.resolve(AlertChannelService)
    case.settings = graph.resolve(AlertSettingsService)
    case.consents = graph.resolve(AlertRecipientConsentService)
    case.addresses = graph.resolve(AlertAddressService)
    case.rules = graph.resolve(AlertRuleService)
    case.connection = await add_connection(case)
    profile = MailProfile(
        host="127.0.0.1",
        port=25,
        tls="local_fixture",
        sender="alerts@example.com",
        allowed_recipients=tuple(ADDRESSES),
        private_networks=("127.0.0.0/8",),
    )
    usable = {case.connection}

    async def connection(self, tx, actor, identifier):
        AuthorizationService().require(actor, tx.scope, "connection.bind")
        if identifier not in usable:
            raise unavailable()
        return profile

    async def credentials(self, scope, actor, identifier):
        return profile, {}

    monkeypatch.setattr(EmailService, "connection", connection)
    monkeypatch.setattr(EmailService, "credentials", credentials)
    case.server = MailServer()
    case.email = EmailService(graph.resolve(ConnectionService), case.server)
    case.notifier = AlertNotifier(UnitOfWork(case.sessions), case.email)
    # Rules on open incidents need incident.read (operator) besides status.read (viewer).
    roles = ("alert_manager", "email_sender", "deployer", "tenant_admin", "viewer", "operator")
    case.owner_a = await principal_with(case, *(Grant(role=role, scope=case.scope) for role in roles))
    case.owner_b = await principal_with(case, *(Grant(role=role, scope=case.scope) for role in roles))
    case.manager = case.owner_a
    case.members = []
    for address in ADDRESSES[:10]:
        member = await principal_with(case, Grant(role="viewer", scope=case.scope))
        await case.addresses.save_mine(
            member, case.scope.tenant_id, MyAlertAddressUpdate(address=address), context=CONTEXT
        )
        case.members.append(member)
    case.now = await clock(case)
    case.uow = UnitOfWork(case.sessions)
    (case.authority,) = await due_alert_scopes(case.uow, case.scope.tenant_id, case.now)
    case.loop = AlertLoop(
        Settings(database_url=case.url.render_as_string(hide_password=False), scheduler_enabled=False),
        case.uow,
        LoopHealth(),
        clock=lambda: case.now,
    )
    return case


async def channel(case, *recipients, actor=None):
    request = AlertChannelDraft.model_validate_json(
        json.dumps(
            {
                "kind": "email",
                "name": "On call",
                "connection_revision_id": str(case.connection),
                "recipients": recipients,
            },
            default=str,
        )
    )
    return await case.channels.create(actor or case.owner_a, case.scope, request, str(uuid4()), context=CONTEXT)


async def deliver(case, seconds: float = 0) -> int:
    return await case.notifier.deliver(case.authority, case.now + timedelta(seconds=seconds), limit=100)


async def notifications(case, **where) -> list[dict]:
    clause = "".join(f" AND {key}=:{key}" for key in where)
    return await owner(
        case,
        f"SELECT kind,state,code,attempts,recipients_count FROM alert_notifications WHERE tenant_id=:tenant{clause} "
        "ORDER BY created_at,id",
        **where,
    )


def text_of(raw: bytes) -> str:
    payload = message_from_bytes(raw).get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode().replace("\r\n", "\n")


def subjects(case) -> list[str]:
    return [message_from_bytes(raw)["Subject"] for _, raw in case.server.sent]


async def test_alert_mail_goes_out_as_the_channel_owner_one_message_per_recipient(mail):
    case = mail
    run = await case.start("alert-mail")
    await add_incident(case, run.id, "first")
    target = await channel(case, {"member": case.members[0].id}, {"member": case.members[1].id})
    await create(case, draft(name="Incidents are open", channel_ids=[str(target.id)]))
    await case.loop.evaluate(case.authority)
    assert await deliver(case) == 1
    assert sorted(recipients for recipients, _ in case.server.sent) == [(ADDRESSES[0],), (ADDRESSES[1],)]
    assert set(subjects(case)) == {"[Critical] Incidents are open is firing in environment"}
    body = text_of(case.server.sent[0][1])
    assert "Rule: Incidents are open\n" in body and "Value: 1 (threshold above 0)\n" in body and "http" not in body
    assert await notifications(case) == [
        {"kind": "firing", "state": "sent", "code": None, "attempts": 1, "recipients_count": 2}
    ]
    senders = await owner(case, "SELECT DISTINCT principal_id FROM email_submissions WHERE tenant_id=:tenant")
    assert senders == [{"principal_id": case.owner_a.id}]
    # Nothing is left to send.
    assert await deliver(case, 120) == 0 and len(case.server.sent) == 2


async def test_a_revoked_opt_in_stops_mail_at_once_and_an_owner_who_lost_email_send_sends_nothing(mail):
    case = mail
    update = AlertSettingsUpdate(recipient_domains=["vendor.example"], approval_channel_ids=[])
    await case.settings.update(case.owner_a, case.scope, update, None, context=CONTEXT)
    consent = await case.consents.create(
        case.owner_a,
        case.scope,
        AlertRecipientConsentDraft(address="oncall@vendor.example", statement="Agreed on the call"),
        "consent",
        context=CONTEXT,
    )
    target = await channel(
        case, {"member": case.members[0].id}, {"address": "oncall@vendor.example"}, actor=case.owner_b
    )
    await case.consents.revoke(case.owner_a, case.scope, consent.id, context=CONTEXT)
    await case.channels.test(case.owner_b, case.scope, target.id, "first", context=CONTEXT)
    await deliver(case)
    assert [recipients for recipients, _ in case.server.sent] == [(ADDRESSES[0],)]
    assert subjects(case) == ["Test message from Weave alerts in environment"]
    (binding,) = await owner(
        case, "SELECT id FROM role_bindings WHERE principal_id=:p AND role='email_sender'", p=case.owner_b.id
    )
    await case.access.revoke_grant(case.admin, case.scope, binding["id"])
    await case.channels.test(case.owner_a, case.scope, target.id, "second", context=CONTEXT)
    await deliver(case)
    assert len(case.server.sent) == 1
    assert [row["code"] for row in await notifications(case)] == [None, "owner_unauthorized"]


async def test_60_messages_an_hour_per_environment_counted_per_recipient(mail):
    case = mail
    targets = [await channel(case, *({"member": member.id} for member in case.members)) for _ in range(4)]
    for index in range(7):
        await case.channels.test(case.owner_a, case.scope, targets[index // 2].id, f"test-{index}", context=CONTEXT)
    await deliver(case)
    states = [row["state"] for row in await notifications(case)]
    assert states == ["sent"] * 6 + ["skipped_limit"] and len(case.server.sent) == 60


async def test_a_mail_whose_outcome_is_unclear_is_never_sent_again_and_a_failed_one_is_retried(mail):
    case = mail
    target = await channel(case, {"member": case.members[0].id})
    case.server.outcomes = ["unknown", "rejected"]
    await case.channels.test(case.owner_a, case.scope, target.id, "unclear", context=CONTEXT)
    await deliver(case)
    await case.channels.test(case.owner_a, case.scope, target.id, "refused", context=CONTEXT)
    await deliver(case)
    rows = await notifications(case)
    assert [(row["state"], row["code"], row["attempts"]) for row in rows] == [
        ("failed", "mail_unknown", 1),
        ("pending", "mail_refused", 1),
    ]
    # Not before a minute has passed, then once more, accepted.
    assert await deliver(case, 30) == 0
    assert await deliver(case, 61) == 1
    rows = await notifications(case)
    assert [(row["state"], row["attempts"]) for row in rows] == [("failed", 1), ("sent", 2)]
    assert len(case.server.sent) == 3


async def test_an_approval_mail_names_the_target_intent_and_expiry_once_per_revision_never_the_reason(mail):
    case = mail
    target_channel = await channel(case, {"member": case.members[0].id})
    update = AlertSettingsUpdate(recipient_domains=[], approval_channel_ids=[target_channel.id])
    await case.settings.update(case.owner_a, case.scope, update, None, context=CONTEXT)
    target = await add_target(case, "local-docker")
    request = uuid4()
    expires = case.now + timedelta(minutes=15)
    await owner(
        case,
        "INSERT INTO deployment_change_requests(id,tenant_id,project_id,environment_id,target_id,revision,payload,"
        "state) VALUES(:id,:tenant,:project,:environment,:target,1,cast(:payload AS jsonb),'awaiting_approval')",
        id=request,
        target=target,
        payload=json.dumps(
            {"intent": "scale_workers", "plan_expires_at": expires.isoformat(), "reason": "Payroll secret 42"}
        ),
    )
    await case.loop.evaluate(case.authority)
    queued = {"kind": "approval", "state": "pending", "code": None, "attempts": 0, "recipients_count": 1}
    assert await notifications(case, kind="approval") == [queued]
    async with case.tx() as tx:
        assert await scan_approvals(tx, case.now) == 0
    await deliver(case)
    assert [(row["state"], row["code"]) for row in await notifications(case, kind="approval")] == [("sent", None)]
    (message,) = [raw for _, raw in case.server.sent]
    parsed = message_from_bytes(message)
    body = text_of(message)
    assert parsed["Subject"] == "A change request awaits approval in environment"
    assert "Target: local-docker\n" in body and "Change: Scale workers\n" in body
    assert f"Expires: {expires:%H:%M} UTC\n" in body
    assert "Payroll" not in body and "secret" not in body and "http" not in body
    await owner(case, "UPDATE deployment_change_requests SET revision=2 WHERE id=:id", id=request)
    async with case.tx() as tx:
        assert await scan_approvals(tx, case.now) == 1
    approvals = await notifications(case, kind="approval")
    assert len(approvals) == 2


async def test_a_send_that_fails_after_the_server_took_the_mail_is_never_sent_again(mail, monkeypatch):
    case = mail
    target = await channel(case, {"member": case.members[0].id})
    original = EmailService.execute

    async def execute(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise ConnectionResetError("the connection dropped after the server took the mail")

    monkeypatch.setattr(EmailService, "execute", execute)
    await case.channels.test(case.owner_a, case.scope, target.id, "dropped", context=CONTEXT)
    await deliver(case)
    for seconds in (61, 400, 1000):
        await deliver(case, seconds)
    assert [recipients for recipients, _ in case.server.sent] == [(ADDRESSES[0],)]
    rows = await notifications(case)
    assert [(row["state"], row["code"], row["attempts"]) for row in rows] == [("failed", "mail_unknown", 1)]


async def test_a_delivery_stopped_after_mailing_finishes_without_mailing_anyone_twice(mail, monkeypatch):
    case = mail
    first, second = case.members[0], case.members[1]
    target = await channel(case, {"member": first.id}, {"member": second.id})
    await case.channels.test(case.owner_a, case.scope, target.id, "stopped", context=CONTEXT)

    async def stopped(self, *args, **kwargs):
        raise RuntimeError("the process stopped before it recorded the outcome")

    with monkeypatch.context() as patched:
        patched.setattr(AlertNotifier, "record", stopped)
        with pytest.raises(RuntimeError):
            await deliver(case)
    assert sorted(recipients for recipients, _ in case.server.sent) == [(ADDRESSES[0],), (ADDRESSES[1],)]
    sent = await owner(case, "SELECT recipient_hash,request_id FROM alert_messages WHERE tenant_id=:tenant")
    # The first recipient leaves the channel before the claim is taken again.
    current = await case.channels.read(case.owner_a, case.scope, target.id, context=CONTEXT)
    only_second = AlertChannelDraft.model_validate_json(
        json.dumps(
            {
                "kind": "email",
                "name": "On call",
                "connection_revision_id": str(case.connection),
                "recipients": [{"member": str(second.id)}],
            }
        )
    )
    await case.channels.update(case.owner_a, case.scope, target.id, only_second, current.revision, context=CONTEXT)
    for seconds in (121, 300, 1300):
        await deliver(case, seconds)
    assert sorted(recipients for recipients, _ in case.server.sent) == [(ADDRESSES[0],), (ADDRESSES[1],)]
    after = await owner(case, "SELECT recipient_hash,request_id FROM alert_messages WHERE tenant_id=:tenant")
    assert sorted(map(str, (row["request_id"] for row in after))) == sorted(
        map(str, (row["request_id"] for row in sent))
    )
    rows = await notifications(case)
    assert [(row["state"], row["code"]) for row in rows] == [("sent", None)]


async def hash_of(case, address: str) -> str:
    async with case.tx() as tx:
        return await address_hash(tx, address)


async def test_a_retry_mails_only_the_recipients_whose_earlier_message_was_refused(mail):
    case = mail
    first, second = case.members[0], case.members[1]
    target = await channel(case, {"member": first.id}, {"member": second.id})
    case.server.outcomes = ["rejected", "rejected"]
    await case.channels.test(case.owner_a, case.scope, target.id, "refused", context=CONTEXT)
    await deliver(case)
    assert [(row["state"], row["attempts"]) for row in await notifications(case)] == [("pending", 1)]
    # The second recipient's first message counts as delivered after all: a retry must not mail them again.
    await owner(
        case,
        "UPDATE alert_messages SET state='accepted' WHERE tenant_id=:tenant AND recipient_hash=:hash",
        hash=await hash_of(case, ADDRESSES[1]),
    )
    await deliver(case, 61)
    assert [recipients for recipients, _ in case.server.sent][2:] == [(ADDRESSES[0],)]
    assert [(row["state"], row["attempts"]) for row in await notifications(case)] == [("sent", 2)]


async def test_an_alert_mail_shows_the_value_its_notification_was_queued_with(mail):
    case = mail
    run = await case.start("alert-value")
    await add_incident(case, run.id, "first")
    target = await channel(case, {"member": case.members[0].id})
    await create(case, draft(name="Incidents are open", channel_ids=[str(target.id)]))
    await case.loop.evaluate(case.authority)
    # The alert's value moves on before the mail goes out; the mail still says what fired.
    second = await case.start("alert-value-second")
    await add_incident(case, second.id, "second")
    case.loop.clock = lambda: case.now + timedelta(seconds=30)
    await case.loop.evaluate(case.authority)
    await deliver(case, 30)
    (message,) = [raw for _, raw in case.server.sent]
    assert "Value: 1 (threshold above 0)\n" in text_of(message)


async def test_test_messages_leave_alert_mail_its_budget(mail):
    case = mail
    target = await channel(case, *({"member": member.id} for member in case.members))
    refused: list[str] = []
    for index in range(6):
        try:
            await case.channels.test(case.owner_a, case.scope, target.id, f"test-{index}", context=CONTEXT)
        except CatalogError as error:
            refused.append(error.code)
    await deliver(case)
    assert refused == ["WV-ALERT-LIMIT"] * 3 and len(case.server.sent) == 30
    run = await case.start("alert-after-tests")
    await add_incident(case, run.id, "first")
    await create(case, draft(name="Incidents are open", channel_ids=[str(target.id)]))
    await case.loop.evaluate(case.authority)
    await deliver(case)
    assert [(row["state"], row["code"]) for row in await notifications(case, kind="firing")] == [("sent", None)]


async def test_test_messages_on_two_channels_leave_alert_mail_the_environment_budget(mail):
    case = mail
    targets = [await channel(case, *({"member": member.id} for member in case.members)) for _ in range(2)]
    for target in targets:
        for index in range(3):
            await case.channels.test(case.owner_a, case.scope, target.id, f"{target.id}-{index}", context=CONTEXT)
    await deliver(case)
    # Six tests to ten members each: the environment's 60 messages of the hour.
    assert len(case.server.sent) == 60
    run = await case.start("alert-after-two-channels-of-tests")
    await add_incident(case, run.id, "first")
    await create(case, draft(name="Incidents are open", channel_ids=[str(targets[0].id)]))
    await case.loop.evaluate(case.authority)
    await deliver(case)
    assert [(row["state"], row["code"]) for row in await notifications(case, kind="firing")] == [("sent", None)]
    assert len(case.server.sent) == 70
