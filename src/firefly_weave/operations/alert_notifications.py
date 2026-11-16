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

"""Alert and approval mail: what it says, who it reaches, how much may go out, and never twice.

Mail is sent outside any evaluation transaction, as the channel's owner and through the owner's email connection,
one message per recipient so no recipient sees another's address. At every send the owner's access, each member's
grant and saved address, and each outside address's domain and opt-in are checked again. Messages are counted per
recipient against the budgets: one alert message per rule and channel every 15 minutes, 40 per rule and 60 per
environment an hour, and 20 per recipient an hour. A message whose outcome is unclear is never sent again; a send that
reached nobody is tried again after 1, 5 and 15 minutes, and only to the recipients it didn't reach. Each recipient's
request is keyed by their address, so a delivery that stopped after sending reads what went out instead of sending it
again. The text is fixed: no link, no user text but the rule name, and the value the notification was queued with.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.contracts.alerts import AlertChannelDraft, AlertUnit
from firefly_weave.contracts.email import EmailSendRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.service import EmailService
from firefly_weave.operations.alert_channels import address_hash, principal_here, resolve_recipients, stored_settings
from firefly_weave.operations.alert_rules import SCOPED, AlertRuleRepository
from firefly_weave.operations.loop_health import raise_if_cancelled
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.scheduler import failure_cause

ALERT_KINDS = frozenset({"firing", "resolved", "repeat"})
RULE_CHANNEL_MINUTES = 15
RULE_HOUR = 40
ENVIRONMENT_HOUR = 60
RECIPIENT_HOUR = 20
# A send that reached nobody is tried again after these many minutes; then it has failed.
RETRY_MINUTES = (1, 5, 15)
# A claimed first attempt that never finished (the process stopped) is claimed again after this long.
CLAIM_MINUTES = 2
SUBJECT_NAME = 80

ALERT_SIGNAL_NAMES: Mapping[str, str] = {
    "run.failure_rate": "Run failure rate",
    "run.failures": "Failed runs",
    "run.duration_p95": "Run duration (95th percentile)",
    "queue.oldest_ready_seconds": "Oldest waiting task",
    "queue.ready_tasks": "Waiting tasks",
    "outbox.pending": "Event deliveries waiting",
    "provider.pending": "Provider messages waiting",
    "incidents.active": "Open incidents",
    "workers.online": "Workers online",
    "workers.free_slots": "Free worker slots",
    "targets.runner_offline": "Targets whose runner is offline",
    "deployments.reconciliation_required": "Deployments that need reconciling",
    "health.degraded": "Degraded platform components",
}
_SEVERITY = {"info": "Info", "warning": "Warning", "critical": "Critical"}
_COMPARATOR = {"gt": "above", "gte": "at or above", "lt": "below", "lte": "at or below"}
_INTENT = {
    "deploy": "Deploy",
    "update": "Update",
    "scale_workers": "Scale workers",
    "restart_workers": "Restart workers",
}
# Why a notification sent nothing, by the first recipient state that stopped it.
_RECIPIENT_CODES = {
    "not_member": "recipient_not_member",
    "without_address": "recipient_without_address",
    "domain_not_allowed": "recipient_not_allowed",
    "without_consent": "recipient_without_consent",
}

type Kind = Literal["firing", "resolved", "repeat", "test", "approval", "approval_moved"]
type Outcome = tuple[Literal["sent", "failed", "pending"], str | None]


def figure(value: float, unit: AlertUnit) -> str:
    if unit == "ratio":
        return f"{value * 100:.1f}".rstrip("0").rstrip(".") + " %"
    return f"{value:g}" + (" s" if unit == "seconds" else "")


def _time(at: datetime) -> str:
    return f"{at.astimezone(UTC):%H:%M} UTC"


def _footer(environment: str, thing: str) -> str:
    return (
        f"You get this because an alert {thing} in {environment} sends to this address. Ask an administrator of "
        f"{environment} to change the {thing}'s channels."
        if thing == "rule"
        else f"You get this because an alert channel in {environment} sends to this address. Ask an administrator of "
        f"{environment} to change the channel."
    )


def alert_email(
    alert: Mapping[str, Any], environment: str, kind: Literal["firing", "resolved", "repeat"]
) -> tuple[str, str]:
    """The subject and text of an alert message; ``alert`` holds the rule's name and the alert's figures."""
    name, severity = alert["rule_name"], _SEVERITY[alert["severity"]]
    short = name[:SUBJECT_NAME]
    subject = {
        "firing": f"[{severity}] {short} is firing in {environment}",
        "repeat": f"[{severity}] {short} is still firing in {environment}",
        "resolved": f"Resolved: {short} in {environment}",
    }[kind]
    state = (
        f"Resolved at {_time(alert['resolved_at'])}"
        if kind == "resolved" and alert["resolved_at"] is not None
        else f"Firing since {_time(alert['started_at'])}"
    )
    unit = alert["unit"]
    value = "no data" if alert["value"] is None else figure(alert["value"], unit)
    threshold = f"{_COMPARATOR[alert['comparator']]} {figure(alert['threshold'], unit)}"
    lines = [
        f"Rule: {name}",
        f"State: {state}",
        f"Signal: {ALERT_SIGNAL_NAMES.get(alert['signal'], alert['signal'])}",
        f"Value: {value} (threshold {threshold})",
        f"Environment: {environment}",
        "",
        _footer(environment, "rule"),
    ]
    return subject, "\n".join(lines) + "\n"


def approval_email(target: str, intent: str, expires_at: datetime | None, environment: str) -> tuple[str, str]:
    """An approval request: the target, what changes and when it expires; no action, no link, never the reason."""
    lines = [f"Target: {target}", f"Change: {_INTENT.get(intent, intent)}"]
    if expires_at is not None:
        lines.append(f"Expires: {_time(expires_at)}")
    lines += ["", "Open Studio › Operate › Clusters › Approvals to review it."]
    return f"A change request awaits approval in {environment}", "\n".join(lines) + "\n"


def channel_test_email(environment: str) -> tuple[str, str]:
    body = f"This test message shows that alert mail from {environment} reaches this address.\n\n"
    return f"Test message from Weave alerts in {environment}", body + _footer(environment, "channel") + "\n"


def moved_email(environment: str) -> tuple[str, str]:
    body = (
        f"Approval notifications for {environment} now go to another channel.\n\n"
        f"If you didn't expect this, ask an administrator of {environment}.\n"
    )
    return f"Approval notifications moved in {environment}", body


@dataclass(frozen=True)
class Usage:
    """Messages already counted against the budgets this message would join."""

    # A message of this rule went to this channel within 15 minutes.
    rule_channel_recent: bool
    rule_hour: int
    environment_hour: int
    # By the keyed hash of each recipient's address.
    recipients_hour: Mapping[str, int]


def admit(kind: Kind, recipients: Sequence[str], usage: Usage) -> list[str] | None:
    """The recipients (address hashes) that may get this message now, or None when a budget skips it."""
    alert = kind in ALERT_KINDS
    if alert and usage.rule_channel_recent:
        return None
    allowed = [recipient for recipient in recipients if usage.recipients_hour.get(recipient, 0) < RECIPIENT_HOUR]
    if not allowed or usage.environment_hour + len(allowed) > ENVIRONMENT_HOUR:
        return None
    if alert and usage.rule_hour + len(allowed) > RULE_HOUR:
        return None
    return allowed


def outcome(states: Sequence[str], attempts: int, *, final: str | None = None) -> Outcome:
    """A notification's state after ``attempts`` sends, from its messages' states.

    Anything accepted makes it sent. Any state but accepted or rejected ends it as unclear, because the mail may have
    arrived: an unknown result, another deliverer mid-send, a send that never settled. A send that reached nobody is
    tried again until three retries are spent, unless ``final`` names a refusal no retry changes.
    """
    if any(state == "accepted" for state in states):
        return "sent", None if all(state == "accepted" for state in states) else "mail_refused"
    if any(state != "rejected" for state in states):
        return "failed", "mail_unknown"
    if final is not None:
        return "failed", final
    if attempts <= len(RETRY_MINUTES):
        return "pending", "mail_refused"
    return "failed", "mail_refused"


def due(attempts: int, attempted_at: datetime | None, now: datetime) -> bool:
    """Whether a pending notification may be tried now."""
    if attempted_at is None:
        return True
    wait = CLAIM_MINUTES if attempts == 0 else RETRY_MINUTES[min(attempts, len(RETRY_MINUTES)) - 1]
    return attempted_at <= now - timedelta(minutes=wait)


def request_id(notification_id: UUID, recipient_hash: str, attempt: int) -> UUID:
    """One email request per notification, recipient and attempt, keyed by the recipient's address hash: a retried
    delivery never queues one twice, and a recipient leaving the channel never moves another's request."""
    return uuid5(NAMESPACE_URL, f"weave-alert-notification:{notification_id}:{recipient_hash}:{attempt}")


def settled(state: str) -> str:
    """A message's state as stored: what the server answered, or unknown for anything that may still arrive."""
    return state if state in {"accepted", "rejected"} else "unknown"


def _refused(error: Exception) -> tuple[str, str | None]:
    """A send that surely went nowhere: rejected, with the refusal no retry changes when there is one."""
    raise_if_cancelled(error)
    if isinstance(error, AccessDenied | AuthenticationFailed):
        return "rejected", "owner_unauthorized"
    if isinstance(error, CatalogError):
        if error.code == "WV-EMAIL-RECIPIENT":
            # The connection's own recipient list refuses this address.
            return "rejected", "recipient_not_allowed"
        if error.code in {"WV-EMAIL-UNAVAILABLE", "WV-FORBIDDEN"}:
            return "rejected", "owner_unauthorized"
        logging.getLogger(__name__).warning("Alert mail was refused (%s)", error.code)
        return "rejected", None
    # The mail server or the database couldn't be reached before anything was sent: it is tried again.
    logging.getLogger(__name__).warning("Alert mail could not be sent (%s)", failure_cause(error))
    return "rejected", None


# Pending notifications that may be tried now, as ``due`` decides.
_DUE = (
    "state='pending' AND (attempted_at IS NULL OR attempted_at<=cast(:now AS timestamptz)-make_interval(mins=>"
    f"CASE attempts WHEN 0 THEN {CLAIM_MINUTES} WHEN 1 THEN {RETRY_MINUTES[0]} WHEN 2 THEN {RETRY_MINUTES[1]} "
    f"ELSE {RETRY_MINUTES[2]} END))"
)


@dataclass(frozen=True)
class Message:
    """One recipient's message; ``existing`` when an earlier delivery of the same attempt already wrote it."""

    id: UUID
    request: UUID
    address: str
    existing: bool = False


@dataclass
class Prepared:
    """A notification ready to send, or its final state when nothing goes out."""

    notification: Mapping[str, Any]
    owner: UUID | None = None
    connection: UUID | None = None
    subject: str = ""
    body: str = ""
    messages: tuple[Message, ...] = ()
    final: tuple[str, str | None] | None = None


class AlertNotifier:
    """Delivers one environment's pending alert and approval notifications."""

    def __init__(self, uow: UnitOfWork, email: EmailService) -> None:
        self.uow, self.email = uow, email
        self.authorization = AuthorizationService()

    async def deliver(self, authority: _SchedulerScope, now: datetime, *, limit: int = 10) -> int:
        """Claim up to ``limit`` notifications that may be tried now and send them; returns how many were tried."""
        async with self.uow.open(authority.scope, admission=False) as tx:
            await authority.verify(tx)
            claimed = await AlertRuleRepository(tx).rows(
                f"UPDATE alert_notifications SET attempted_at=:now WHERE {SCOPED} AND id IN (SELECT id FROM "
                f"alert_notifications WHERE {SCOPED} AND {_DUE} ORDER BY created_at,id LIMIT :limit FOR UPDATE SKIP "
                "LOCKED) RETURNING id,alert_id,channel_id,kind,attempts,change_request_id,change_request_revision,"
                "value",
                now=now,
                limit=limit,
            )
        for notification in claimed:
            await self._one(authority, notification, now)
        return len(claimed)

    async def _one(self, authority: _SchedulerScope, notification: Mapping[str, Any], now: datetime) -> None:
        async with self.uow.open(authority.scope, admission=False) as tx:
            await authority.verify(tx)
            prepared = await self.prepare(tx, notification, now)
        states: list[str] = []
        final: str | None = None
        for message in prepared.messages:
            if final == "owner_unauthorized":
                states.append("rejected")
                continue
            state, refusal = await self._send(authority, prepared, message)
            states.append(state)
            final = refusal or final
        async with self.uow.open(authority.scope, admission=False) as tx:
            await authority.verify(tx)
            await self.record(tx, prepared, states, final, now)

    async def _send(self, authority: _SchedulerScope, prepared: Prepared, message: Message) -> tuple[str, str | None]:
        """One message as the channel's owner: its state, and a refusal no retry changes.

        A message an earlier delivery already wrote is never queued again: what that delivery queued is read, and
        sent only if it is still waiting. Once a submission is queued, a failure counts as nothing sent only while
        the submission is still queued; otherwise the mail may have gone out, and the outcome is unknown.
        """
        assert prepared.owner is not None and prepared.connection is not None
        owner = Principal(id=prepared.owner, kind="human")
        try:
            submission = None
            if message.existing:
                submission = await self.email.submitted(owner, authority.scope, message.request)
            if submission is None:
                submission = await self.email.queue(
                    owner,
                    authority.scope,
                    EmailSendRequest(
                        request_id=message.request,
                        connection_revision_id=prepared.connection,
                        to=(message.address,),
                        subject=prepared.subject,
                        text=prepared.body,
                    ),
                    automated=True,
                )
            elif submission.state != "queued":
                return settled(submission.state), None
        except Exception as error:
            return _refused(error)
        try:
            result = await self.email.execute(owner, authority.scope, submission.id)
        except Exception as error:
            raise_if_cancelled(error)
            if await self._still_queued(owner, authority, message.request):
                return _refused(error)
            logging.getLogger(__name__).warning("Alert mail may have been sent (%s)", failure_cause(error))
            return "unknown", None
        return settled(result.state), None

    async def _still_queued(self, owner: Principal, authority: _SchedulerScope, request: UUID) -> bool:
        """Whether the submission is surely unsent; a read that fails can't tell, so it is not."""
        try:
            submission = await self.email.submitted(owner, authority.scope, request)
        except Exception as error:
            raise_if_cancelled(error)
            return False
        return submission is not None and submission.state == "queued"

    async def prepare(self, tx: Transaction, notification: Mapping[str, Any], now: datetime) -> Prepared:
        """Check the channel, its owner, its recipients and the budgets again, and write the messages to send."""
        repository = AlertRuleRepository(tx)
        prepared = Prepared(notification)
        found = await repository.rows(
            f"SELECT name,kind,connection_revision_id,recipients,owner_id FROM alert_channels WHERE {SCOPED} "
            "AND id=:id",
            id=notification["channel_id"],
        )
        if not found:
            prepared.final = ("failed", "channel_deleted")
            return prepared
        channel = found[0]
        owner = await principal_here(tx, channel["owner_id"])
        if owner is None or not all(
            self.authorization.allows(owner, tx.scope, capability) for capability in ("email.send", "connection.bind")
        ):
            prepared.final = ("failed", "owner_unauthorized")
            return prepared
        draft = AlertChannelDraft.model_validate_json(
            json.dumps(
                {
                    "kind": channel["kind"],
                    "name": channel["name"],
                    "connection_revision_id": str(channel["connection_revision_id"]),
                    "recipients": channel["recipients"],
                }
            )
        )
        resolved = await resolve_recipients(tx, draft)
        addresses = list(dict.fromkeys(item.address for item in resolved if item.address is not None))
        if not addresses:
            first = next((item.state for item in resolved if item.state != "ok"), "not_member")
            prepared.final = ("failed", _RECIPIENT_CODES.get(first, "recipient_not_member"))
            return prepared
        content = await self._content(tx, notification)
        if content is None:
            prepared.final = ("failed", "approval_ended")
            return prepared
        rule_id, prepared.subject, prepared.body = content
        attempt = int(notification["attempts"])
        # A retry goes only to the recipients every earlier attempt was refused for.
        reached = {
            row["recipient_hash"]
            for row in await repository.rows(
                f"SELECT recipient_hash FROM alert_messages WHERE {SCOPED} AND notification_id=:notification "
                "AND attempt<:attempt AND state<>'rejected'",
                notification=notification["id"],
                attempt=attempt,
            )
        }
        hashes = {address: await address_hash(tx, address) for address in addresses}
        hashes = {address: value for address, value in hashes.items() if value not in reached}
        if not hashes:
            prepared.final = ("sent", None)
            return prepared
        allowed = admit(notification["kind"], list(hashes.values()), await self._usage(tx, notification, rule_id, now))
        if allowed is None:
            prepared.final = ("skipped_limit", "skipped_limit")
            return prepared
        messages = []
        for address, recipient in hashes.items():
            if recipient not in allowed:
                continue
            request = request_id(notification["id"], recipient, attempt)
            written = await repository.rows(
                "INSERT INTO alert_messages(tenant_id,project_id,environment_id,id,notification_id,rule_id,channel_id,"
                "recipient_hash,request_id,attempt,state,sent_at) VALUES(:tenant,:project,:environment,:id,"
                ":notification,:rule,:channel,:hash,:request,:attempt,'queued',:now) ON CONFLICT DO NOTHING "
                "RETURNING id",
                id=uuid4(),
                notification=notification["id"],
                rule=rule_id,
                channel=notification["channel_id"],
                hash=recipient,
                request=request,
                attempt=attempt,
                now=now,
            )
            if written:
                messages.append(Message(written[0]["id"], request, address))
                continue
            # A delivery of this attempt that stopped before recording its outcome already wrote this message.
            (earlier,) = await repository.rows(
                f"SELECT id FROM alert_messages WHERE {SCOPED} AND request_id=:request", request=request
            )
            messages.append(Message(earlier["id"], request, address, existing=True))
        prepared.messages = tuple(messages)
        prepared.owner, prepared.connection = channel["owner_id"], channel["connection_revision_id"]
        return prepared

    async def _content(self, tx: Transaction, notification: Mapping[str, Any]) -> tuple[UUID | None, str, str] | None:
        """The rule the message counts against, its subject and its text; None when an approval no longer waits."""
        repository = AlertRuleRepository(tx)
        environment = (await repository.rows("SELECT name FROM environments WHERE id=:environment"))[0]["name"]
        kind = notification["kind"]
        if kind in ALERT_KINDS:
            # Only what never changes once the notification is queued, so every delivery of it says the same.
            (alert,) = await repository.rows(
                "SELECT rule_id,rule_name,severity,signal,comparator,threshold,unit,started_at,resolved_at "
                f"FROM alerts WHERE {SCOPED} AND id=:id",
                id=notification["alert_id"],
            )
            return alert["rule_id"], *alert_email(
                {**alert, "value": notification["value"]},
                environment,
                cast(Literal["firing", "resolved", "repeat"], kind),
            )
        if kind == "test":
            return None, *channel_test_email(environment)
        if kind == "approval_moved":
            return None, *moved_email(environment)
        found = await repository.rows(
            "SELECT c.payload,c.revision,c.state,t.payload->>'name' AS target FROM deployment_change_requests c "
            "JOIN deployment_targets t ON t.tenant_id=c.tenant_id AND t.project_id=c.project_id AND "
            "t.environment_id=c.environment_id AND t.id=c.target_id WHERE c.tenant_id=:tenant AND "
            "c.project_id=:project AND c.environment_id=:environment AND c.id=:id",
            id=notification["change_request_id"],
        )
        if (
            not found
            or found[0]["state"] != "awaiting_approval"
            or found[0]["revision"] != notification["change_request_revision"]
        ):
            return None
        payload = found[0]["payload"]
        expires = payload.get("plan_expires_at")
        subject, body = approval_email(
            found[0]["target"],
            payload["intent"],
            None if expires is None else datetime.fromisoformat(expires),
            environment,
        )
        return None, subject, body

    async def _usage(
        self, tx: Transaction, notification: Mapping[str, Any], rule_id: UUID | None, now: datetime
    ) -> Usage:
        counted = "state<>'rejected' AND notification_id<>:notification"
        repository = AlertRuleRepository(tx)
        hour = now - timedelta(hours=1)
        environment = await repository.rows(
            f"SELECT count(*) AS n FROM alert_messages WHERE {SCOPED} AND sent_at>:hour AND {counted}",
            hour=hour,
            notification=notification["id"],
        )
        recipients = await repository.rows(
            f"SELECT recipient_hash,count(*) AS n FROM alert_messages WHERE {SCOPED} AND sent_at>:hour AND {counted} "
            "GROUP BY recipient_hash",
            hour=hour,
            notification=notification["id"],
        )
        rule_hour, recent = 0, False
        if rule_id is not None:
            rule = await repository.rows(
                f"SELECT count(*) AS n,count(*) FILTER (WHERE channel_id=:channel AND sent_at>:recent) AS recent "
                f"FROM alert_messages WHERE {SCOPED} AND rule_id=:rule AND sent_at>:hour AND {counted}",
                rule=rule_id,
                channel=notification["channel_id"],
                recent=now - timedelta(minutes=RULE_CHANNEL_MINUTES),
                hour=hour,
                notification=notification["id"],
            )
            rule_hour, recent = int(rule[0]["n"]), int(rule[0]["recent"]) > 0
        return Usage(
            rule_channel_recent=recent,
            rule_hour=rule_hour,
            environment_hour=int(environment[0]["n"]),
            recipients_hour={row["recipient_hash"]: int(row["n"]) for row in recipients},
        )

    async def record(
        self, tx: Transaction, prepared: Prepared, states: Sequence[str], final: str | None, now: datetime
    ) -> None:
        repository = AlertRuleRepository(tx)
        for message, state in zip(prepared.messages, states, strict=True):
            await repository.execute(
                f"UPDATE alert_messages SET state=:state WHERE {SCOPED} AND id=:id", id=message.id, state=settled(state)
            )
        attempts = int(prepared.notification["attempts"]) + 1
        if prepared.final is not None:
            state, code = prepared.final
        else:
            state, code = outcome(states, attempts, final=final)
        await repository.execute(
            "UPDATE alert_notifications SET state=:state,code=:code,attempts=:attempts,attempted_at=:now,"
            f"recipients_count=:sent WHERE {SCOPED} AND id=:id",
            state=state,
            code=code,
            attempts=attempts,
            now=now,
            sent=sum(1 for item in states if item != "rejected"),
            id=prepared.notification["id"],
        )


async def scan_approvals(tx: Transaction, now: datetime, *, page: int = 20) -> int:
    """Queue one approval message per approval channel for each change request awaiting approval, once per revision.

    It walks the requests in pages from the environment's scan position, wrapping at the end, so every request is
    seen again: a request that returns to awaiting approval with a new revision is mailed again.
    """
    channels = (await stored_settings(tx)).approval_channel_ids
    if not channels:
        return 0
    repository = AlertRuleRepository(tx)
    position = await repository.rows(
        f"SELECT approval_after_at,approval_after_id FROM alert_environments WHERE {SCOPED} FOR UPDATE"
    )
    after_at = position[0]["approval_after_at"] if position else None
    after_id = position[0]["approval_after_id"] if position else None
    resume = "" if after_at is None else " AND (created_at,id)>(cast(:after_at AS timestamptz),cast(:after_id AS uuid))"
    requests = await repository.rows(
        f"SELECT id,revision,created_at FROM deployment_change_requests WHERE {SCOPED} AND state='awaiting_approval'"
        f"{resume} ORDER BY created_at,id LIMIT :page",
        after_at=after_at,
        after_id=after_id,
        page=page,
    )
    queued = 0
    for request in requests:
        inserted = await repository.rows(
            "INSERT INTO alert_notifications(tenant_id,project_id,environment_id,id,channel_id,kind,sequence,"
            "recipients_count,state,created_at,change_request_id,change_request_revision) SELECT tenant_id,"
            "project_id,environment_id,gen_random_uuid(),id,'approval',0,jsonb_array_length(recipients),'pending',"
            f":now,:request,:revision FROM alert_channels WHERE {SCOPED} AND id=ANY(:channels) ON CONFLICT DO NOTHING "
            "RETURNING id",
            now=now,
            request=request["id"],
            revision=request["revision"],
            channels=list(channels),
        )
        queued += len(inserted)
    last = requests[-1] if len(requests) == page else None
    if position:
        await repository.execute(
            f"UPDATE alert_environments SET approval_after_at=:at,approval_after_id=:id WHERE {SCOPED}",
            at=None if last is None else last["created_at"],
            id=None if last is None else last["id"],
        )
    return queued
