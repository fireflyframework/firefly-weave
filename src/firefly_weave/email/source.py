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
"""Leased IMAP admission, conservative header grouping and authenticated routing."""

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.email import EmailCorrelationToken, EmailReceipt, EmailTokenRequest, NormalizedEmail
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.mime import parse_message
from firefly_weave.email.service import EmailService, unavailable
from firefly_weave.email.transport import IMAPTransport
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService


def correlation_candidates(mail: NormalizedEmail) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*mail.references, *((mail.in_reply_to,) if mail.in_reply_to else ()))))


@service
class EmailSourceService:
    def __init__(
        self, email: EmailService, imap: IMAPTransport, runtime: RuntimeService, signals: SignalService
    ) -> None:
        self.email, self.imap, self.runtime, self.signals = email, imap, runtime, signals

    async def create(
        self, actor: Principal, scope: Scope, connection_revision_id: UUID, *, activation_id: UUID | None = None
    ) -> UUID:
        async with self.email.uow.open(scope) as tx:
            actor = await self.email.checked(tx, actor, "email.manage")
            self.runtime.require(actor, scope, "trigger.manage", AuditContext())
            profile = await self.email.connection(tx, actor, connection_revision_id)
            identifier = uuid4()
            await RuntimeRepository(tx).execute(
                "INSERT INTO "
                "email_sources(id,tenant_id,project_id,environment_id,principal_id,connection_revision"
                "_id,activation_id,mailbox,policy) "
                "VALUES(:id,:tenant,:project,:environment,:actor,:connection,:activation,:mailbox,'{}'"
                "::jsonb)",
                id=identifier,
                actor=actor.id,
                connection=connection_revision_id,
                activation=activation_id,
                mailbox=profile.folder,
            )
            return identifier

    async def checked(
        self, tx: Transaction, identifier: UUID, *, lock: bool = False
    ) -> tuple[dict[str, Any], Principal]:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM email_sources WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""), id=identifier
        )
        if not rows:
            raise unavailable()
        row = rows[0]
        actor = await load_principal(tx.session, row["principal_id"])
        await self.email.checked(tx, actor, "email.manage")
        self.runtime.require(actor, tx.scope, "trigger.manage", AuditContext())
        await self.email.connection(tx, actor, row["connection_revision_id"])
        return row, actor

    async def poll(self, actor: Principal, scope: Scope, identifier: UUID) -> dict[str, Any]:
        async with self.email.uow.open(scope) as tx:
            await self.email.checked(tx, actor, "email.manage")
            row, owner = await self.checked(tx, identifier, lock=True)
            db = RuntimeRepository(tx)
            now = await db.now()
            profile = await self.email.connection(tx, owner, row["connection_revision_id"])
            await db.execute(
                "UPDATE email_receipts r SET state='held',reason='correlation_window_elapsed' FROM "
                "email_messages m WHERE r.tenant_id=:tenant AND r.project_id=:project AND "
                "r.environment_id=:environment AND r.source_id=:source AND r.message_id=m.id AND "
                "r.state='pending_correlation' AND m.accepted_at<:cutoff",
                source=identifier,
                cutoff=now - timedelta(seconds=profile.correlation_retention_seconds),
            )
            if row["state"] != "active":
                return {"state": row["state"]}
            if row["lease_until"] is not None and row["lease_until"] > now:
                return {"state": "leased"}
            generation = row["generation"] + 1
            await db.execute(
                f"UPDATE email_sources SET "
                f"generation=:generation,lease_until=clock_timestamp()+interval '65 seconds' WHERE "
                f"{SCOPE} AND id=:id",
                id=identifier,
                generation=generation,
            )
        profile, credentials = await self.email.credentials(scope, owner, row["connection_revision_id"])
        if profile.folder != row["mailbox"]:
            raise unavailable()
        batch = await self.imap.poll(profile, credentials, row["last_uid"] if row["uidvalidity"] is not None else None)
        async with self.email.uow.open(scope) as tx:
            current, _ = await self.checked(tx, identifier, lock=True)
            db = RuntimeRepository(tx)
            if current["generation"] != generation or current["lease_until"] <= await db.now():
                raise unavailable()
            if current["uidvalidity"] is not None and current["uidvalidity"] != batch.uidvalidity:
                await db.execute(
                    f"UPDATE email_sources SET state='resync_required',lease_until=NULL WHERE {SCOPE} AND id=:id",
                    id=identifier,
                )
                return {"state": "resync_required"}
            if current["uidvalidity"] is None:
                await db.execute(
                    f"UPDATE email_sources SET "
                    f"uidvalidity=:validity,last_uid=:uid,lease_until=clock_timestamp()+interval '30 "
                    f"seconds' WHERE {SCOPE} AND id=:id",
                    id=identifier,
                    validity=batch.uidvalidity,
                    uid=batch.highest_uid,
                )
                return {"state": "baseline_recorded", "uid": batch.highest_uid}
            last_uid = current["last_uid"]
            for uid, raw in batch.messages:
                key = f"{identifier}:{row['mailbox']}:{batch.uidvalidity}:{uid}"
                prior = await db.rows(
                    f"SELECT id FROM email_receipts WHERE {SCOPE} AND source_id=:source AND transport_key=:key",
                    source=identifier,
                    key=key,
                )
                if not prior:
                    await self.admit(tx, current, key, raw)
                last_uid = max(last_uid, uid)
            await db.execute(
                f"UPDATE email_sources SET last_uid=:uid,lease_until=clock_timestamp()+interval '30 "
                f"seconds' WHERE {SCOPE} AND id=:id",
                id=identifier,
                uid=last_uid,
            )
            return {"state": "active", "admitted": len(batch.messages), "uid": last_uid}

    async def rebaseline(self, actor: Principal, scope: Scope, identifier: UUID) -> None:
        async with self.email.uow.open(scope) as tx:
            await self.email.checked(tx, actor, "email.manage")
            row, _ = await self.checked(tx, identifier, lock=True)
            if row["state"] != "resync_required":
                raise unavailable()
            # Explicit command permits a fresh new-message-only baseline; old receipts remain immutable.
            await RuntimeRepository(tx).execute(
                f"UPDATE email_sources SET "
                f"state='active',uidvalidity=NULL,last_uid=0,generation=generation+1,lease_until=NULL "
                f"WHERE {SCOPE} AND id=:id",
                id=identifier,
            )

    async def admit(self, tx: Transaction, source: dict[str, Any], key: str, raw: bytes | None) -> None:
        db = RuntimeRepository(tx)
        receipt_id = uuid4()
        try:
            if raw is None:
                raise ValueError("Oversized message")
            mail = parse_message(raw)
        except (ValueError, LookupError):
            await db.execute(
                "INSERT INTO "
                "email_receipts(id,tenant_id,project_id,environment_id,source_id,transport_key,state,r"
                "eason) "
                "VALUES(:id,:tenant,:project,:environment,:source,:key,'rejected','malformed_or_oversi"
                "zed')",
                id=receipt_id,
                source=source["id"],
                key=key,
            )
            return
        parents = correlation_candidates(mail)
        related = (
            await db.rows(
                f"SELECT DISTINCT conversation_id FROM email_messages WHERE {SCOPE} AND "
                f"connection_revision_id=:connection AND payload->>'message_id'=ANY(:parents) AND "
                f"conversation_id IS NOT NULL",
                connection=source["connection_revision_id"],
                parents=list(parents),
            )
            if parents
            else []
        )
        conversation_id = related[0]["conversation_id"] if len(related) == 1 else None
        state = "pending_correlation" if parents else "pending"
        # Header grouping conveys display continuity only; it never dispatches a signal.
        if len(related) > 1:
            state = "conflict"
        run_id, signal, correlation_id = None, None, None
        profile = await self.email.connection(
            tx, await load_principal(tx.session, source["principal_id"]), source["connection_revision_id"]
        )
        aliases = [
            v
            for v in (*mail.to, *mail.cc)
            if profile.routing_domain and v.endswith("@" + profile.routing_domain) and v.startswith("weave-")
        ]
        tokens = (
            await db.rows(
                f"SELECT * FROM email_correlations WHERE {SCOPE} AND source_id=:source AND NOT revoked "
                f"AND expires_at>clock_timestamp() AND token_hash=ANY(:hashes)",
                source=source["id"],
                hashes=[hashlib.sha256(v.encode()).hexdigest() for v in aliases],
            )
            if aliases
            else []
        )
        if aliases:
            state = "pending_correlation"
            if (
                len(tokens) == 1
                and len(related) <= 1
                and (not related or related[0]["conversation_id"] == tokens[0]["conversation_id"])
            ):
                token = tokens[0]
                correlation_id = token["id"]
                conversation_id = token["conversation_id"]
                state = "authorized"
                run_id, signal = token["run_id"], token["signal"]
            elif len(tokens) > 1 or tokens and related:
                state = "conflict"
        if not parents and not aliases:
            conversation_id = uuid4()
            await db.execute(
                "INSERT INTO "
                "email_conversations(id,tenant_id,project_id,environment_id,connection_revision_id,sub"
                "ject) VALUES(:id,:tenant,:project,:environment,:connection,:subject)",
                id=conversation_id,
                connection=source["connection_revision_id"],
                subject=mail.subject,
            )
        if mail.automated or mail.sender == profile.sender:
            state = "suppressed"
        local = (
            await db.rows(
                f"SELECT id FROM email_messages WHERE {SCOPE} AND direction='outbound' AND "
                f"connection_revision_id=:connection AND payload->>'message_id'=:mid",
                connection=source["connection_revision_id"],
                mid=mail.message_id,
            )
            if mail.message_id
            else []
        )
        if local:
            state = "suppressed"
        message_id = uuid4()
        await db.execute(
            "INSERT INTO "
            "email_messages(id,tenant_id,project_id,environment_id,conversation_id,connection_revi"
            "sion_id,direction,state,payload,transport_key) "
            "VALUES(:id,:tenant,:project,:environment,:conversation,:connection,'inbound',:state,c"
            "ast(:payload AS jsonb),:key)",
            id=message_id,
            conversation=conversation_id,
            connection=source["connection_revision_id"],
            state=state,
            payload=mail.model_dump_json(),
            key=key,
        )
        await db.execute(
            "INSERT INTO "
            "email_receipts(id,tenant_id,project_id,environment_id,source_id,transport_key,message"
            "_id,state,run_id,reason,correlation_id) "
            "VALUES(:id,:tenant,:project,:environment,:source,:key,:message,:state,:run,:signal,:correlation)",
            id=receipt_id,
            source=source["id"],
            key=key,
            message=message_id,
            state=state,
            run=run_id,
            signal=signal,
            correlation=correlation_id,
        )

        if mail.message_id and conversation_id is not None:
            # Late parent arrival only updates grouping; retained receipts cannot redispatch.
            await self.email.conversation(tx, conversation_id, lock_row=True)
            pending = await db.rows(
                f"SELECT id,payload FROM email_messages WHERE {SCOPE} AND "
                f"connection_revision_id=:connection AND conversation_id IS NULL AND "
                f"state='pending_correlation' ORDER BY id LIMIT 100",
                connection=source["connection_revision_id"],
            )
            for item in pending:
                pending_mail = NormalizedEmail.model_validate_json(json.dumps(item["payload"]))
                parents = correlation_candidates(pending_mail)
                if mail.message_id not in parents:
                    continue
                resolved = await db.rows(
                    f"SELECT DISTINCT conversation_id FROM email_messages WHERE {SCOPE} AND "
                    f"connection_revision_id=:connection AND payload->>'message_id'=ANY(:parents) AND "
                    f"conversation_id IS NOT NULL",
                    connection=source["connection_revision_id"],
                    parents=list(parents),
                )
                if len(resolved) == 1 and resolved[0]["conversation_id"] == conversation_id:
                    await db.execute(
                        f"UPDATE email_messages SET conversation_id=:conversation WHERE {SCOPE} AND id=:id",
                        id=item["id"],
                        conversation=conversation_id,
                    )

    async def inbox(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None
    ) -> dict[str, object]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded inbox page required")
        async with self.email.uow.open(scope, mutation=False) as tx:
            await self.email.checked(tx, actor, "email.read")
            db = RuntimeRepository(tx)
            rows = await db.rows(
                f"SELECT * FROM email_receipts WHERE {SCOPE}"
                + (" AND id>:cursor" if cursor else "")
                + " ORDER BY id LIMIT :limit",
                cursor=cursor,
                limit=limit + 1,
            )
            result = []
            for row in rows[:limit]:
                message = None
                if row["message_id"]:
                    messages = await db.rows(
                        f"SELECT * FROM email_messages WHERE {SCOPE} AND id=:id", id=row["message_id"]
                    )
                    if messages:
                        message = self.email.message(messages[0])
                result.append(
                    EmailReceipt(
                        **{key: row[key] for key in EmailReceipt.model_fields if key != "message"}, message=message
                    ).model_dump(mode="json")
                )
            return {"items": result, "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    async def issue_token(self, actor: Principal, scope: Scope, request: EmailTokenRequest) -> EmailCorrelationToken:
        async with self.email.uow.open(scope) as tx:
            await self.email.checked(tx, actor, "email.manage", request.conversation_id)
            self.runtime.require(actor, scope, "run.signal", AuditContext())
            source, owner = await self.checked(tx, request.source_id, lock=True)
            self.runtime.require(owner, scope, "run.signal", AuditContext())
            profile = await self.email.connection(tx, owner, source["connection_revision_id"])
            conversation = await self.email.conversation(tx, request.conversation_id, lock_row=True)
            db = RuntimeRepository(tx)
            await db.run(request.run_id)
            if (
                not profile.routing_domain
                or source["connection_revision_id"] != conversation.connection_revision_id
                or conversation.run_id != request.run_id
            ):
                raise unavailable()
            identifier = uuid4()
            reply_address = "weave-" + secrets.token_hex(24) + "@" + profile.routing_domain
            expires_at = await db.now() + timedelta(seconds=request.validity_seconds)
            await db.execute(
                "INSERT INTO "
                "email_correlations(id,tenant_id,project_id,environment_id,source_id,conversation_id,r"
                "un_id,signal,token_hash,expires_at) "
                "VALUES(:id,:tenant,:project,:environment,:source,:conversation,:run,:signal,:hash,:ex"
                "piry)",
                id=identifier,
                source=source["id"],
                conversation=conversation.id,
                run=request.run_id,
                signal=request.signal,
                hash=hashlib.sha256(reply_address.encode()).hexdigest(),
                expiry=expires_at,
            )
            return EmailCorrelationToken(id=identifier, reply_address=reply_address, expires_at=expires_at)

    async def revoke_token(self, actor: Principal, scope: Scope, identifier: UUID) -> None:
        async with self.email.uow.open(scope) as tx:
            await self.email.checked(tx, actor, "email.manage")
            await RuntimeRepository(tx).execute(
                f"UPDATE email_correlations SET revoked=true WHERE {SCOPE} AND id=:id", id=identifier
            )

    async def correlate(
        self, actor: Principal, scope: Scope, receipt_id: UUID, conversation_id: UUID, *, run_id: UUID, signal: str
    ) -> None:
        # An authenticated manager binding supplies authority independent of hostile mail headers.
        async with self.email.uow.open(scope) as tx:
            await self.email.checked(tx, actor, "email.manage", conversation_id)
            await self.email.checked(tx, actor, "email.read", conversation_id)
            self.runtime.require(actor, scope, "run.signal", AuditContext())
            conversation = await self.email.conversation(tx, conversation_id, lock_row=True)
            db = RuntimeRepository(tx)
            receipt = (
                await db.rows(f"SELECT * FROM email_receipts WHERE {SCOPE} AND id=:id FOR UPDATE", id=receipt_id)
            )[0]
            source, _ = await self.checked(tx, receipt["source_id"])
            if source["connection_revision_id"] != conversation.connection_revision_id or receipt["state"] not in (
                "pending_correlation",
                "conflict",
                "pending",
            ):
                raise unavailable()
            if conversation.run_id is not None and conversation.run_id != run_id:
                raise unavailable()
            await db.run(run_id)
            await db.execute(
                f"UPDATE email_messages SET conversation_id=:conversation WHERE {SCOPE} AND id=:id",
                id=receipt["message_id"],
                conversation=conversation_id,
            )
            await db.execute(
                f"UPDATE email_receipts SET state='authorized',run_id=:run,reason=:signal WHERE {SCOPE} AND id=:id",
                id=receipt_id,
                run=run_id,
                signal=signal,
            )

    async def dispatch(self, actor: Principal, scope: Scope, receipt_id: UUID) -> dict[str, Any]:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            await self.email.checked(tx, actor, "email.manage")
            db = RuntimeRepository(tx)
            initial = (await db.rows(f"SELECT * FROM email_receipts WHERE {SCOPE} AND id=:id", id=receipt_id))[0]
            source, owner = await self.checked(tx, initial["source_id"], lock=True)
            receipt = (
                await db.rows(f"SELECT * FROM email_receipts WHERE {SCOPE} AND id=:id FOR UPDATE", id=receipt_id)
            )[0]
            if receipt["state"] not in ("pending", "authorized"):
                return {"state": receipt["state"]}
            message = (
                await db.rows(f"SELECT * FROM email_messages WHERE {SCOPE} AND id=:id", id=receipt["message_id"])
            )[0]
            if message["conversation_id"] is None:
                raise unavailable()
            conversation = await self.email.conversation(tx, message["conversation_id"], lock_row=True)
            key = f"email:{source['id']}:{receipt_id}"
            try:
                async with tx.session.begin_nested():
                    if receipt["state"] == "authorized":
                        if receipt.get("correlation_id") is not None:
                            token = await db.rows(
                                f"SELECT id FROM email_correlations WHERE {SCOPE} AND id=:id AND source_id=:source AND "
                                f"conversation_id=:conversation AND run_id=:run AND signal=:signal AND NOT revoked AND "
                                f"expires_at>clock_timestamp()",
                                id=receipt["correlation_id"],
                                source=source["id"],
                                conversation=conversation.id,
                                run=receipt["run_id"],
                                signal=receipt["reason"],
                            )
                            if not token:
                                await db.execute(
                                    "UPDATE email_receipts SET state='blocked',"
                                    "reason='correlation_revoked_or_expired' "
                                    f"WHERE {SCOPE} AND id=:id",
                                    id=receipt_id,
                                )
                                return {"state": "blocked", "reason": "correlation_revoked_or_expired"}
                        outcome = await self.signals.deliver(
                            tx,
                            receipt["run_id"],
                            key,
                            receipt["reason"],
                            message["payload"],
                            actor=owner,
                            scope=scope,
                            context=AuditContext(),
                        )
                        run_id = receipt["run_id"]
                        signal_id = outcome.id
                    elif source["activation_id"] is not None and conversation.run_id is None:
                        outcome_run = await self.runtime.start(
                            owner,
                            scope,
                            StartRunRequest(activation_id=source["activation_id"], input=message["payload"]),
                            key,
                            context=AuditContext(),
                            tx=tx,
                        )
                        run_id, signal_id = outcome_run.id, None
                        await db.execute(
                            f"UPDATE email_conversations SET run_id=:run WHERE {SCOPE} AND id=:id",
                            id=conversation.id,
                            run=run_id,
                        )
                    else:
                        await db.execute(
                            f"UPDATE email_receipts SET state='retained',reason='no_initial_target' WHERE {SCOPE} "
                            f"AND id=:id",
                            id=receipt_id,
                        )
                        return {"state": "retained"}
            except CatalogError as error:
                state = "retained" if "TERMINAL" in error.code else "blocked"
                await db.execute(
                    f"UPDATE email_receipts SET state=:state,reason=:reason WHERE {SCOPE} AND id=:id",
                    id=receipt_id,
                    state=state,
                    reason=error.code,
                )
                return {"state": state, "reason": error.code}
            await db.execute(
                f"UPDATE email_receipts SET state='dispatched',run_id=:run,signal_id=:signal WHERE {SCOPE} AND id=:id",
                id=receipt_id,
                run=run_id,
                signal=signal_id,
            )
            return {"state": "dispatched", "run_id": str(run_id)}

    async def scan(self, authority: "_SchedulerScope", limit: int = 10) -> int:
        if not 1 <= limit <= 10:
            raise ValueError("Bounded email turn required")
        async with self.email.uow.open(authority.scope, mutation=False) as tx:
            await authority.verify(tx)
            db = RuntimeRepository(tx)
            rows = await db.rows(
                f"SELECT id,principal_id FROM email_sources WHERE {SCOPE} AND state='active' AND "
                f"(lease_until IS NULL OR lease_until<=clock_timestamp()) ORDER BY lease_until NULLS "
                f"FIRST,id LIMIT :limit",
                limit=limit,
            )
        for row in rows:
            try:
                async with self.email.uow.open(authority.scope, mutation=False) as tx:
                    owner = await load_principal(tx.session, row["principal_id"])
                await self.poll(owner, authority.scope, row["id"])
            except (CatalogError, AccessDenied, OSError, TimeoutError, ValueError):
                continue
        async with self.email.uow.open(authority.scope, mutation=False) as tx:
            await authority.verify(tx)
            pending = await RuntimeRepository(tx).rows(
                "SELECT r.id,s.principal_id FROM email_receipts r JOIN email_sources s ON "
                "s.id=r.source_id WHERE r.tenant_id=:tenant AND r.project_id=:project AND "
                "r.environment_id=:environment AND r.state IN ('pending','authorized') ORDER BY r.id "
                "LIMIT :limit",
                limit=limit,
            )
        for row in pending:
            try:
                async with self.email.uow.open(authority.scope, mutation=False) as tx:
                    owner = await load_principal(tx.session, row["principal_id"])
                await self.dispatch(owner, authority.scope, row["id"])
            except (CatalogError, AccessDenied, ValueError):
                continue
        return len(rows) + len(pending)
