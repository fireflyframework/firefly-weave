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

"""Alert email: channels, who they may reach, outside recipients' opt-ins, each person's own alert address, and the
environment's alert settings.

Recipients are project members by default, reached at the address each member saved for themselves; nobody types a
member's address. An outside address needs a domain an administrator allowed and an opt-in an administrator recorded,
and the person who recorded an opt-in can't add that address to a channel unless they are the environment's only
administrator. All of it is checked when a channel is saved and again at every send. Audit records keep channel IDs,
names, counts, domains and a keyed hash of each address, never an address, and replays store only IDs.
"""

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import (
    AddressRecipient,
    AlertChannel,
    AlertChannelDraft,
    AlertNotificationView,
    AlertRecipient,
    AlertRecipientConsent,
    AlertRecipientConsentDraft,
    AlertRecipientState,
    AlertSettings,
    AlertSettingsUpdate,
    MemberRecipient,
    MyAlertAddress,
    MyAlertAddressUpdate,
)
from firefly_weave.contracts.public import Revoked
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.service import EmailService
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork

CHANNEL_LIMIT = 20
SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
# Roles that machines hold; a principal with only these is no person and receives no alert mail.
SERVICE_ROLES = frozenset({"worker", "deployment_runner"})
ADMINISTRATOR_ROLES = tuple(sorted(role for role, held in ROLE_CAPABILITIES.items() if "environment.manage" in held))
_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

type RecipientState = Literal["ok", "not_member", "without_address", "domain_not_allowed", "without_consent"]


def normalize_domain(value: str) -> str:
    """A domain as stored and compared: IDNA A-labels, lowercase, no trailing dot, at least two labels."""
    stripped = value.strip().rstrip(".")
    try:
        ascii_value = stripped.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Use a domain such as vendor.example") from None
    if len(ascii_value) > 253 or not _DOMAIN.fullmatch(ascii_value):
        raise ValueError("Use a domain such as vendor.example")
    return ascii_value


def normalize_address(value: str) -> str:
    """An address with its domain normalized; the local part keeps its case."""
    local, _, domain = value.rpartition("@")
    return f"{local}@{normalize_domain(domain)}"


def recipient_allowed(address: str, domains: Sequence[str]) -> bool:
    """Whether the address's domain is one of ``domains`` (normalized) or a subdomain of one."""
    try:
        domain = normalize_domain(address.rpartition("@")[2])
    except ValueError:
        return False
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in domains)


@dataclass(frozen=True)
class Audience:
    """What decides who a channel reaches, read in its environment."""

    # Principals with a grant in the project.
    members: frozenset[UUID]
    # The addresses members saved for themselves.
    addresses: Mapping[UUID, str]
    # The environment's allowed recipient domains, normalized.
    domains: tuple[str, ...]
    # Active opt-ins: the normalized address and who recorded it.
    consents: Mapping[str, UUID]


@dataclass(frozen=True)
class ResolvedRecipient:
    index: int
    # Where mail goes; None when it goes nowhere.
    address: str | None
    state: RecipientState


def _resolve_one(index: int, recipient: AlertRecipient, audience: Audience) -> ResolvedRecipient:
    if isinstance(recipient, MemberRecipient):
        if recipient.member not in audience.members:
            return ResolvedRecipient(index, None, "not_member")
        saved = audience.addresses.get(recipient.member)
        return ResolvedRecipient(index, saved, "ok" if saved else "without_address")
    address = normalize_address(recipient.address)
    if not recipient_allowed(address, audience.domains):
        return ResolvedRecipient(index, None, "domain_not_allowed")
    if address not in audience.consents:
        return ResolvedRecipient(index, None, "without_consent")
    return ResolvedRecipient(index, address, "ok")


def resolve(channel: AlertChannelDraft, audience: Audience) -> list[ResolvedRecipient]:
    """Where each recipient's mail goes now, and why it goes nowhere when it doesn't."""
    return [_resolve_one(index, recipient, audience) for index, recipient in enumerate(channel.recipients)]


def refusals(
    channel: AlertChannelDraft,
    audience: Audience,
    *,
    actor: UUID,
    sole_administrator: bool,
    kept: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Why each recipient a channel may not be saved with is refused: indexes and reasons, never addresses.

    ``kept`` are the normalized outside addresses already on the channel: the person who recorded an opt-in may keep
    an address someone else added, but not add it.
    """
    refused: list[dict[str, Any]] = []
    for index, recipient in enumerate(channel.recipients):
        reason = None
        if isinstance(recipient, MemberRecipient):
            if recipient.member not in audience.members:
                reason = "not_member"
        else:
            address = normalize_address(recipient.address)
            if not recipient_allowed(address, audience.domains):
                reason = "domain_not_allowed"
            elif address not in audience.consents:
                reason = "no_consent"
            elif audience.consents[address] == actor and not sole_administrator and address not in kept:
                reason = "consent_by_creator"
        if reason is not None:
            refused.append({"index": index, "reason": reason})
    return refused


def _keys(tx: Transaction) -> dict[str, Any]:
    return {"tenant": tx.scope.tenant_id, "project": tx.scope.project_id, "environment": tx.scope.environment_id}


async def _rows(tx: Transaction, sql: str, **values: Any) -> list[dict[str, Any]]:
    result = await tx.session.execute(text(sql), {**_keys(tx), **values})
    return [dict(row) for row in result.mappings()]


async def _execute(tx: Transaction, sql: str, **values: Any) -> None:
    await tx.session.execute(text(sql), {**_keys(tx), **values})


async def _now(tx: Transaction) -> datetime:
    value: datetime = (await _rows(tx, "SELECT clock_timestamp() AS now"))[0]["now"]
    return value


async def stored_settings(tx: Transaction, *, lock_row: bool = False) -> AlertSettings:
    found = await _rows(
        tx,
        f"SELECT recipient_domains,approval_channel_ids,revision FROM alert_settings WHERE {SCOPED}"
        + (" FOR UPDATE" if lock_row else ""),
    )
    if not found:
        return AlertSettings(recipient_domains=[], approval_channel_ids=[], revision=0)
    row = found[0]
    return AlertSettings(
        recipient_domains=list(row["recipient_domains"]),
        approval_channel_ids=list(row["approval_channel_ids"]),
        revision=row["revision"],
    )


async def load_audience(tx: Transaction, recipients: Iterable[AlertRecipient]) -> Audience:
    """Members, their saved addresses, the allowed domains and the active opt-ins the recipients need, as of now."""
    wanted = list(recipients)
    member_ids = [recipient.member for recipient in wanted if isinstance(recipient, MemberRecipient)]
    outside = [normalize_address(r.address) for r in wanted if isinstance(r, AddressRecipient)]
    members: frozenset[UUID] = frozenset()
    addresses: dict[UUID, str] = {}
    if member_ids:
        members = frozenset(
            row["id"]
            for row in await _rows(
                tx,
                "SELECT p.id FROM principals p WHERE p.id=ANY(:ids) AND p.active AND p.kind<>'worker' AND EXISTS("
                "SELECT 1 FROM role_bindings b WHERE b.principal_id=p.id AND b.tenant_id=:tenant AND "
                "(b.project_id IS NULL OR b.project_id=:project) AND NOT (b.role=ANY(:service)))",
                ids=member_ids,
                service=sorted(SERVICE_ROLES),
            )
        )
        if members:
            addresses = {
                row["principal_id"]: row["address"]
                for row in await _rows(
                    tx,
                    "SELECT principal_id,address FROM alert_member_addresses WHERE tenant_id=:tenant "
                    "AND principal_id=ANY(:ids)",
                    ids=sorted(members),
                )
            }
    consents: dict[str, UUID] = {}
    if outside:
        consents = {
            row["address"]: row["created_by"]
            for row in await _rows(
                tx,
                f"SELECT address,created_by FROM alert_recipient_consents WHERE {SCOPED} AND revoked_at IS NULL "
                "AND address=ANY(:addresses)",
                addresses=outside,
            )
        }
    settings = await stored_settings(tx)
    return Audience(members, addresses, tuple(settings.recipient_domains), consents)


async def resolve_recipients(tx: Transaction, channel: AlertChannelDraft) -> list[ResolvedRecipient]:
    """Where a channel's mail goes at this moment; the sender calls it before every message."""
    return resolve(channel, await load_audience(tx, channel.recipients))


async def address_hash(tx: Transaction, address: str) -> str:
    """A keyed hash of the address with the tenant's own random key, so audit exports can't be matched to a list."""
    await _execute(
        tx,
        "INSERT INTO alert_hash_keys(tenant_id,key,created_at) VALUES(:tenant,:key,clock_timestamp()) "
        "ON CONFLICT DO NOTHING",
        key=secrets.token_bytes(32),
    )
    key = (await _rows(tx, "SELECT key FROM alert_hash_keys WHERE tenant_id=:tenant"))[0]["key"]
    return hmac.new(bytes(key), normalize_address(address).encode(), hashlib.sha256).hexdigest()


async def _described(tx: Transaction, address: str) -> dict[str, str]:
    normalized = normalize_address(address)
    return {"domain": normalized.rpartition("@")[2], "address_hash": await address_hash(tx, normalized)}


async def principal_here(tx: Transaction, principal_id: UUID) -> Principal | None:
    """A principal with its grants in this tenant, read without acting as that principal."""
    found = await _rows(tx, "SELECT id,kind,active FROM principals WHERE id=:id", id=principal_id)
    if not found:
        return None
    grants = tuple(
        Grant(
            role=row["role"],
            scope=Scope(tenant_id=row["tenant_id"], project_id=row["project_id"], environment_id=row["environment_id"]),
            resources=tuple(row["resources"]),
        )
        for row in await _rows(
            tx,
            "SELECT role,tenant_id,project_id,environment_id,resources FROM role_bindings "
            "WHERE tenant_id=:tenant AND principal_id=:id",
            id=principal_id,
        )
    )
    return Principal(**found[0], grants=grants)


def _changed(record: str) -> CatalogError:
    return CatalogError(409, "WV-REVISION", f"The {record} changed; read it again before saving")


def _revision(expected: int | None) -> int:
    if expected is None:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision")
    return expected


def _environment(scope: Scope) -> None:
    if scope.environment_id is None:
        raise CatalogError(422, "WV-SCOPE", "Alerts require an environment")


_CHANNEL = "id,kind,name,connection_revision_id,recipients,owner_id,revision"


def _draft(row: Mapping[str, Any]) -> AlertChannelDraft:
    return AlertChannelDraft.model_validate_json(
        json.dumps(
            {
                "kind": row["kind"],
                "name": row["name"],
                "connection_revision_id": str(row["connection_revision_id"]),
                "recipients": row["recipients"],
            }
        )
    )


@service
class AlertChannelService:
    """Email channels of one environment, sent through as their owner: the person who last saved them."""

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService, email: EmailService) -> None:
        self.uow, self.authorization, self.email = uow, authorization, email

    def _require(
        self, principal: Principal, scope: Scope, capabilities: tuple[str, ...], context: AuditContext
    ) -> None:
        _environment(scope)
        for capability in capabilities:
            self.authorization.require(principal, scope, capability, context=context)

    async def _current(
        self, tx: Transaction, actor: Principal, capabilities: tuple[str, ...], context: AuditContext
    ) -> Principal:
        current = await load_principal(tx.session, actor.id)
        self._require(current, tx.scope, capabilities, context)
        return current

    async def view(self, tx: Transaction, row: Mapping[str, Any]) -> AlertChannel:
        draft = _draft(row)
        owner = await principal_here(tx, row["owner_id"])
        sendable = owner is not None and all(
            self.authorization.allows(owner, tx.scope, capability) for capability in ("email.send", "connection.bind")
        )
        return AlertChannel(
            **{name: getattr(draft, name) for name in AlertChannelDraft.model_fields},
            id=row["id"],
            owner_id=row["owner_id"],
            revision=row["revision"],
            recipient_states=[
                AlertRecipientState(index=item.index, state=item.state) for item in await resolve_recipients(tx, draft)
            ],
            status="ok" if sendable else "owner_unauthorized",
        )

    async def _row(self, tx: Transaction, channel_id: UUID, *, lock_row: bool = False) -> dict[str, Any]:
        found = await _rows(
            tx,
            f"SELECT {_CHANNEL} FROM alert_channels WHERE {SCOPED} AND id=:id" + (" FOR UPDATE" if lock_row else ""),
            id=channel_id,
        )
        if not found:
            raise CatalogError(404, "WV-NOT-FOUND", "Alert channel not found")
        return found[0]

    async def _sendable(self, tx: Transaction, current: Principal, revision_id: UUID) -> None:
        """The person saving the channel must be able to send mail with its connection, as the sender will."""
        try:
            await self.email.connection(tx, current, revision_id)
        except CatalogError:
            raise CatalogError(
                422,
                "WV-ALERT-CHANNEL",
                "This connection can't send email here. Pick an email connection you may use.",
                result={"connection_revision_id": str(revision_id)},
            ) from None

    async def _admitted(
        self, tx: Transaction, actor: Principal, draft: AlertChannelDraft, kept: frozenset[str]
    ) -> dict[str, Any]:
        """Refuse recipients the channel may not reach; what the audit records about the recipients."""
        audience = await load_audience(tx, draft.recipients)
        administrators = await _rows(
            tx,
            "SELECT count(DISTINCT b.principal_id) AS n FROM role_bindings b JOIN principals p ON p.id=b.principal_id "
            "WHERE b.tenant_id=:tenant AND (b.project_id IS NULL OR b.project_id=:project) AND "
            "(b.environment_id IS NULL OR b.environment_id=:environment) AND b.role=ANY(:roles) AND p.active "
            "AND p.kind<>'worker'",
            roles=list(ADMINISTRATOR_ROLES),
        )
        sole = int(administrators[0]["n"]) <= 1
        refused = refusals(draft, audience, actor=actor.id, sole_administrator=sole, kept=kept)
        if refused:
            raise CatalogError(
                422,
                "WV-ALERT-RECIPIENT",
                "Some recipients can't get alert mail from this environment.",
                result={"recipients": refused},
            )
        outside = [normalize_address(r.address) for r in draft.recipients if isinstance(r, AddressRecipient)]
        details: dict[str, Any] = {
            "recipients": len(draft.recipients),
            "members": len(draft.recipients) - len(outside),
            "outside": [await _described(tx, address) for address in outside],
        }
        if sole and any(audience.consents[address] == actor.id and address not in kept for address in outside):
            # The one administrator recorded the opt-in and added the address: allowed, and recorded.
            details["sole_administrator"] = True
        return details

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int, cursor: UUID | None, context: AuditContext
    ) -> dict[str, Any]:
        self._require(actor, scope, ("alert.read",), context)
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            after = "" if cursor is None else " AND id>:after"
            rows = await _rows(
                tx,
                f"SELECT {_CHANNEL} FROM alert_channels WHERE {SCOPED}{after} ORDER BY id LIMIT :limit",
                after=cursor,
                limit=limit + 1,
            )
            return {
                "items": [(await self.view(tx, row)).model_dump(mode="json") for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def read(self, actor: Principal, scope: Scope, channel_id: UUID, *, context: AuditContext) -> AlertChannel:
        self._require(actor, scope, ("alert.read",), context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            return await self.view(tx, await self._row(tx, channel_id))

    async def create(
        self, actor: Principal, scope: Scope, draft: AlertChannelDraft, idempotency_key: str, *, context: AuditContext
    ) -> AlertChannel:
        needed = ("alert.manage", "email.send")
        self._require(actor, scope, needed, context)
        async with self.uow.open(scope) as tx:
            current = await self._current(tx, actor, needed, context)
            replay = Idempotency(
                tx,
                actor.id,
                f"alert_channels.create:{scope.environment_id}",
                idempotency_key,
                draft.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                # Replays keep only the ID, so no address is stored outside the channel itself.
                return await self.view(tx, await self._row(tx, UUID(prior["id"])))
            await lock(tx, f"alert-channels:{scope.environment_id}")
            await self._sendable(tx, current, draft.connection_revision_id)
            count = await _rows(tx, f"SELECT count(*) AS n FROM alert_channels WHERE {SCOPED}")
            if int(count[0]["n"]) >= CHANNEL_LIMIT:
                raise CatalogError(
                    409, "WV-ALERT-LIMIT", f"An environment holds at most {CHANNEL_LIMIT} alert channels."
                )
            details = await self._admitted(tx, actor, draft, frozenset())
            channel_id, now = uuid4(), await _now(tx)
            await _execute(
                tx,
                "INSERT INTO alert_channels(tenant_id,project_id,environment_id,id,kind,name,connection_revision_id,"
                "recipients,owner_id,revision,created_at,updated_at) VALUES(:tenant,:project,:environment,:id,"
                "'email',:name,:connection,cast(:recipients AS jsonb),:owner,1,:now,:now)",
                id=channel_id,
                name=draft.name,
                connection=draft.connection_revision_id,
                recipients=json.dumps([r.model_dump(mode="json") for r in draft.recipients]),
                owner=actor.id,
                now=now,
            )
            await audit(
                tx.session,
                actor,
                "alert_channel.create",
                str(channel_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={"name": draft.name, "connection_revision_id": str(draft.connection_revision_id), **details},
            )
            await replay.save({"id": str(channel_id)})
            return await self.view(tx, await self._row(tx, channel_id))

    async def update(
        self,
        actor: Principal,
        scope: Scope,
        channel_id: UUID,
        draft: AlertChannelDraft,
        expected_revision: int | None,
        *,
        context: AuditContext,
    ) -> AlertChannel:
        """Save the channel; whoever saves it becomes the person its mail is sent as."""
        needed = ("alert.manage", "email.send")
        self._require(actor, scope, needed, context)
        expected = _revision(expected_revision)
        async with self.uow.open(scope) as tx:
            current = await self._current(tx, actor, needed, context)
            await lock(tx, f"alert-channels:{scope.environment_id}")
            before = await self._row(tx, channel_id, lock_row=True)
            if before["revision"] != expected:
                raise _changed("alert channel")
            await self._sendable(tx, current, draft.connection_revision_id)
            kept = frozenset(
                normalize_address(r.address) for r in _draft(before).recipients if isinstance(r, AddressRecipient)
            )
            details = await self._admitted(tx, actor, draft, kept)
            await _execute(
                tx,
                "UPDATE alert_channels SET name=:name,connection_revision_id=:connection,"
                "recipients=cast(:recipients AS jsonb),owner_id=:owner,revision=revision+1,"
                f"updated_at=clock_timestamp() WHERE {SCOPED} AND id=:id",
                id=channel_id,
                name=draft.name,
                connection=draft.connection_revision_id,
                recipients=json.dumps([r.model_dump(mode="json") for r in draft.recipients]),
                owner=actor.id,
            )
            await audit(
                tx.session,
                actor,
                "alert_channel.update",
                str(channel_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={
                    "name": draft.name,
                    "connection_revision_id": str(draft.connection_revision_id),
                    "previous_owner": str(before["owner_id"]),
                    "revision": expected + 1,
                    **details,
                },
            )
            return await self.view(tx, await self._row(tx, channel_id))

    async def delete(
        self, actor: Principal, scope: Scope, channel_id: UUID, expected_revision: int | None, *, context: AuditContext
    ) -> Revoked:
        self._require(actor, scope, ("alert.manage",), context)
        expected = _revision(expected_revision)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, ("alert.manage",), context)
            await lock(tx, f"alert-channels:{scope.environment_id}")
            before = await self._row(tx, channel_id, lock_row=True)
            if before["revision"] != expected:
                raise _changed("alert channel")
            rules = [
                str(row["id"])
                for row in await _rows(
                    tx, f"SELECT id FROM alert_rules WHERE {SCOPED} AND :id=ANY(channel_ids) ORDER BY id", id=channel_id
                )
            ]
            approval = channel_id in (await stored_settings(tx)).approval_channel_ids
            if rules or approval:
                raise CatalogError(
                    409,
                    "WV-ALERT-CHANNEL",
                    "Rules or approval notifications still send to this channel; remove it from them first.",
                    result={"rule_ids": rules, "approval": approval},
                )
            await _execute(tx, f"DELETE FROM alert_channels WHERE {SCOPED} AND id=:id", id=channel_id)
            await audit(
                tx.session,
                actor,
                "alert_channel.delete",
                str(channel_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={"name": before["name"], "revision": expected},
            )
            return Revoked()

    async def test(
        self, actor: Principal, scope: Scope, channel_id: UUID, idempotency_key: str, *, context: AuditContext
    ) -> AlertNotificationView:
        """Queue one test message; the next delivery sends it, and it counts toward the mail budgets."""
        self._require(actor, scope, ("alert.manage",), context)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, ("alert.manage",), context)
            replay = Idempotency(tx, actor.id, f"alert_channels.test:{channel_id}", idempotency_key, {})
            prior = await replay.replay()
            if prior is not None:
                return AlertNotificationView.model_validate_json(json.dumps(prior))
            row = await self._row(tx, channel_id)
            now = await _now(tx)
            await _execute(
                tx,
                "INSERT INTO alert_notifications(tenant_id,project_id,environment_id,id,channel_id,kind,sequence,"
                "recipients_count,state,created_at) VALUES(:tenant,:project,:environment,:id,:channel,'test',0,"
                ":count,'pending',:now)",
                id=uuid4(),
                channel=channel_id,
                count=len(row["recipients"]),
                now=now,
            )
            await audit(
                tx.session,
                actor,
                "alert_channel.test",
                str(channel_id),
                scope=scope,
                capability="alert.manage",
                context=context,
            )
            queued = AlertNotificationView(channel_id=channel_id, kind="test", state="pending", at=now)
            await replay.save(queued.model_dump(mode="json"))
            return queued


@service
class AlertSettingsService:
    """Allowed recipient domains and the channels approval notifications go to."""

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    def _require(
        self, principal: Principal, scope: Scope, capabilities: tuple[str, ...], context: AuditContext
    ) -> None:
        _environment(scope)
        for capability in capabilities:
            self.authorization.require(principal, scope, capability, context=context)

    async def read(self, actor: Principal, scope: Scope, *, context: AuditContext) -> AlertSettings:
        self._require(actor, scope, ("alert.read",), context)
        async with self.uow.open(scope, mutation=False) as tx:
            self._require(await load_principal(tx.session, actor.id), scope, ("alert.read",), context)
            return await stored_settings(tx)

    async def update(
        self,
        actor: Principal,
        scope: Scope,
        update: AlertSettingsUpdate,
        expected_revision: int | None,
        *,
        context: AuditContext,
    ) -> AlertSettings:
        """Save the settings; without If-Match the environment must have none saved yet (revision 0)."""
        needed = ("alert.manage", "environment.manage")
        self._require(actor, scope, needed, context)
        domains: list[str] = []
        for index, value in enumerate(update.recipient_domains):
            try:
                domain = normalize_domain(value)
            except ValueError:
                raise CatalogError(
                    422,
                    "WV-VALIDATION",
                    "Use a domain such as vendor.example.",
                    result={"field": "recipient_domains", "index": index},
                ) from None
            if domain not in domains:
                domains.append(domain)
        expected = expected_revision or 0
        async with self.uow.open(scope) as tx:
            self._require(await load_principal(tx.session, actor.id), scope, needed, context)
            before = await stored_settings(tx, lock_row=True)
            if before.revision != expected:
                raise _changed("alert settings")
            wanted = list(update.approval_channel_ids)
            found = {
                row["id"]
                for row in await _rows(tx, f"SELECT id FROM alert_channels WHERE {SCOPED} AND id=ANY(:ids)", ids=wanted)
            }
            missing = [str(channel) for channel in wanted if channel not in found]
            if missing:
                raise CatalogError(
                    422, "WV-ALERT-CHANNEL", "Pick email channels of this environment.", result={"channel_ids": missing}
                )
            values = {"domains": domains, "channels": wanted, "actor": actor.id, "expected": expected}
            if expected == 0:
                statement = (
                    "INSERT INTO alert_settings(tenant_id,project_id,environment_id,recipient_domains,"
                    "approval_channel_ids,revision,updated_at,updated_by) VALUES(:tenant,:project,:environment,"
                    "cast(:domains AS text[]),cast(:channels AS uuid[]),1,clock_timestamp(),:actor) "
                    "ON CONFLICT DO NOTHING RETURNING revision"
                )
            else:
                statement = (
                    "UPDATE alert_settings SET recipient_domains=cast(:domains AS text[]),"
                    "approval_channel_ids=cast(:channels AS uuid[]),revision=revision+1,updated_at=clock_timestamp(),"
                    f"updated_by=:actor WHERE {SCOPED} AND revision=:expected RETURNING revision"
                )
            saved = await _rows(tx, statement, **values)
            if not saved:
                raise _changed("alert settings")
            if before.approval_channel_ids and set(before.approval_channel_ids) != set(wanted):
                # Every channel that received approval mail hears once that approvals now go elsewhere too, so a
                # redirect is never silent.
                now = await _now(tx)
                await _execute(
                    tx,
                    "INSERT INTO alert_notifications(tenant_id,project_id,environment_id,id,channel_id,kind,sequence,"
                    "recipients_count,state,created_at) SELECT tenant_id,project_id,environment_id,gen_random_uuid(),"
                    f"id,'approval_moved',0,jsonb_array_length(recipients),'pending',:now FROM alert_channels WHERE "
                    f"{SCOPED} AND id=ANY(:previous)",
                    previous=list(before.approval_channel_ids),
                    now=now,
                )
            await audit(
                tx.session,
                actor,
                "alert_settings.update",
                str(scope.environment_id),
                scope=scope,
                capability="environment.manage",
                context=context,
                details={
                    "old": {
                        "recipient_domains": before.recipient_domains,
                        "approval_channel_ids": [str(channel) for channel in before.approval_channel_ids],
                    },
                    "new": {"recipient_domains": domains, "approval_channel_ids": [str(channel) for channel in wanted]},
                    "revision": saved[0]["revision"],
                },
            )
            return AlertSettings(recipient_domains=domains, approval_channel_ids=wanted, revision=saved[0]["revision"])


_CONSENT = "id,domain,address,method,statement,created_by,created_at,revoked_at,revoked_by"


def _consent(row: Mapping[str, Any]) -> AlertRecipientConsent:
    return AlertRecipientConsent(**{name: row[name] for name in AlertRecipientConsent.model_fields})


@service
class AlertRecipientConsentService:
    """Opt-ins of outside addresses, recorded by an administrator with how the recipient agreed."""

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    def _require(
        self, principal: Principal, scope: Scope, capabilities: tuple[str, ...], context: AuditContext
    ) -> None:
        _environment(scope)
        for capability in capabilities:
            self.authorization.require(principal, scope, capability, context=context)

    async def _current(
        self, tx: Transaction, actor: Principal, capabilities: tuple[str, ...], context: AuditContext
    ) -> Principal:
        current = await load_principal(tx.session, actor.id)
        self._require(current, tx.scope, capabilities, context)
        return current

    async def _row(self, tx: Transaction, consent_id: UUID, *, lock_row: bool = False) -> dict[str, Any]:
        found = await _rows(
            tx,
            f"SELECT {_CONSENT} FROM alert_recipient_consents WHERE {SCOPED} AND id=:id"
            + (" FOR UPDATE" if lock_row else ""),
            id=consent_id,
        )
        if not found:
            raise CatalogError(404, "WV-NOT-FOUND", "Opt-in not found")
        return found[0]

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int, cursor: UUID | None, context: AuditContext
    ) -> dict[str, Any]:
        self._require(actor, scope, ("alert.read",), context)
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            after = "" if cursor is None else " AND id>:after"
            rows = await _rows(
                tx,
                f"SELECT {_CONSENT} FROM alert_recipient_consents WHERE {SCOPED}{after} ORDER BY id LIMIT :limit",
                after=cursor,
                limit=limit + 1,
            )
            return {
                "items": [_consent(row).model_dump(mode="json") for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def create(
        self,
        actor: Principal,
        scope: Scope,
        draft: AlertRecipientConsentDraft,
        idempotency_key: str,
        *,
        context: AuditContext,
    ) -> AlertRecipientConsent:
        needed = ("alert.manage", "environment.manage")
        self._require(actor, scope, needed, context)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, needed, context)
            replay = Idempotency(
                tx,
                actor.id,
                f"alert_recipient_consents.create:{scope.environment_id}",
                idempotency_key,
                {"address_hash": await address_hash(tx, draft.address), "statement": draft.statement},
            )
            prior = await replay.replay()
            if prior is not None:
                return _consent(await self._row(tx, UUID(prior["id"])))
            await lock(tx, f"alert-consents:{scope.environment_id}")
            address = normalize_address(draft.address)
            if not recipient_allowed(address, (await stored_settings(tx)).recipient_domains):
                raise CatalogError(
                    422,
                    "WV-ALERT-RECIPIENT",
                    "That address's domain isn't an allowed email domain here.",
                    result={"reason": "domain_not_allowed"},
                )
            active = await _rows(
                tx,
                f"SELECT id FROM alert_recipient_consents WHERE {SCOPED} AND address=:address AND revoked_at IS NULL",
                address=address,
            )
            if active:
                raise CatalogError(
                    409,
                    "WV-ALERT-RECIPIENT",
                    "That address already has an opt-in here.",
                    result={"reason": "consent_exists", "id": str(active[0]["id"])},
                )
            consent_id = uuid4()
            await _execute(
                tx,
                "INSERT INTO alert_recipient_consents(tenant_id,project_id,environment_id,id,address,domain,method,"
                "statement,created_by,created_at) VALUES(:tenant,:project,:environment,:id,:address,:domain,"
                "'administrator_attested',:statement,:actor,clock_timestamp())",
                id=consent_id,
                address=address,
                domain=address.rpartition("@")[2],
                statement=draft.statement,
                actor=actor.id,
            )
            await audit(
                tx.session,
                actor,
                "alert_recipient_consent.create",
                str(consent_id),
                scope=scope,
                capability="environment.manage",
                context=context,
                details={**await _described(tx, address), "method": "administrator_attested"},
            )
            await replay.save({"id": str(consent_id)})
            return _consent(await self._row(tx, consent_id))

    async def revoke(
        self, actor: Principal, scope: Scope, consent_id: UUID, *, context: AuditContext
    ) -> AlertRecipientConsent:
        """Revoke an opt-in; mail to that address stops at the next send. Revoking twice changes nothing."""
        needed = ("alert.manage", "environment.manage")
        self._require(actor, scope, needed, context)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, needed, context)
            row = await self._row(tx, consent_id, lock_row=True)
            if row["revoked_at"] is not None:
                return _consent(row)
            await _execute(
                tx,
                "UPDATE alert_recipient_consents SET revoked_at=clock_timestamp(),revoked_by=:actor "
                f"WHERE {SCOPED} AND id=:id",
                id=consent_id,
                actor=actor.id,
            )
            await audit(
                tx.session,
                actor,
                "alert_recipient_consent.revoke",
                str(consent_id),
                scope=scope,
                capability="environment.manage",
                context=context,
                details=await _described(tx, row["address"]),
            )
            return _consent(await self._row(tx, consent_id))


def is_person(principal: Principal, tenant: UUID) -> bool:
    """An active principal that isn't a worker, with a grant in the tenant other than a service role."""
    return (
        principal.active
        and principal.kind != "worker"
        and any(
            grant.scope is not None and grant.scope.tenant_id == tenant and grant.role not in SERVICE_ROLES
            for grant in principal.grants
        )
    )


@service
class AlertAddressService:
    """Each person's own alert address. The person is always the caller: no operation names anyone else."""

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def _current(self, tx: Transaction, actor: Principal, tenant: UUID) -> None:
        if not is_person(await load_principal(tx.session, actor.id), tenant):
            raise AccessDenied()

    async def read_mine(self, actor: Principal, tenant: UUID, *, context: AuditContext) -> MyAlertAddress:
        if not is_person(actor, tenant):
            raise AccessDenied()
        async with self.uow.open(Scope(tenant_id=tenant), mutation=False) as tx:
            await self._current(tx, actor, tenant)
            found = await _rows(
                tx,
                "SELECT address,updated_at FROM alert_member_addresses WHERE tenant_id=:tenant AND principal_id=:me",
                me=actor.id,
            )
        return MyAlertAddress(**found[0]) if found else MyAlertAddress()

    async def save_mine(
        self, actor: Principal, tenant: UUID, update: MyAlertAddressUpdate, *, context: AuditContext
    ) -> MyAlertAddress:
        """Set or remove the caller's own address; audited with its domain and a keyed hash."""
        if not is_person(actor, tenant):
            raise AccessDenied()
        scope = Scope(tenant_id=tenant)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, tenant)
            if update.address is None:
                await _execute(
                    tx, "DELETE FROM alert_member_addresses WHERE tenant_id=:tenant AND principal_id=:me", me=actor.id
                )
                details: dict[str, Any] = {"removed": True}
                saved = MyAlertAddress()
            else:
                address = normalize_address(update.address)
                rows = await _rows(
                    tx,
                    "INSERT INTO alert_member_addresses(tenant_id,principal_id,address,updated_at) "
                    "VALUES(:tenant,:me,:address,clock_timestamp()) ON CONFLICT(tenant_id,principal_id) DO UPDATE "
                    "SET address=excluded.address,updated_at=excluded.updated_at RETURNING address,updated_at",
                    me=actor.id,
                    address=address,
                )
                details = await _described(tx, address)
                saved = MyAlertAddress(**rows[0])
            await audit(
                tx.session,
                actor,
                "alert_address.save",
                str(actor.id),
                scope=scope,
                capability="authenticated identity",
                context=context,
                details=details,
            )
            return saved
