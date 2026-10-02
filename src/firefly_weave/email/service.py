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
"""One durable submission boundary for UI and worker mail actions."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.email import (
    EmailConversation,
    EmailConversationDetail,
    EmailMessage,
    EmailReplyRequest,
    EmailSendRequest,
    EmailSubmission,
    MailProfile,
    NormalizedEmail,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.mime import build_message, parse_message
from firefly_weave.email.transport import SMTPTransport
from firefly_weave.persistence.idempotency import lock
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository


def unavailable() -> CatalogError:
    return CatalogError(409, "WV-EMAIL-UNAVAILABLE", "Email resource unavailable")


@service
class EmailService:
    def __init__(self, connections: ConnectionService, transport: SMTPTransport) -> None:
        self.connections, self.transport = connections, transport
        self.uow = connections.uow

    async def checked(
        self, tx: Transaction, actor: Principal, capability: str, resource: UUID | None = None
    ) -> Principal:
        actor = await load_principal(tx.session, actor.id)
        self.connections.definitions.authorization.require(
            actor, tx.scope, capability, resource=str(resource) if resource else None, context=AuditContext()
        )
        return actor

    async def connection(self, tx: Transaction, actor: Principal, identifier: UUID) -> MailProfile:
        revision = await ConnectionRepository(tx).revision(identifier)
        for capability in ("connection.bind",):
            self.connections.require(actor, tx.scope, capability, AuditContext())
            await self.connections._ready(actor, tx.scope, revision, capability, AuditContext(), tx)
        if revision.connector != "weave-email":
            raise unavailable()
        profile = MailProfile.model_validate_json(json.dumps(revision.config))
        target = f"https://{profile.host}:{profile.port}"
        if target not in revision.allowed_destinations:
            raise unavailable()
        return profile

    async def credentials(self, scope: Scope, actor: Principal, identifier: UUID) -> tuple[MailProfile, dict[str, str]]:
        async def checked() -> tuple[MailProfile, ConnectionRevision]:
            async with self.uow.open(scope, mutation=False) as tx:
                current = await self.checked(tx, actor, "email.send")
                profile = await self.connection(tx, current, identifier)
                revision = await ConnectionRepository(tx).revision(identifier)
                grants = await RuntimeRepository(tx).rows(
                    f"SELECT handle FROM connection_grants WHERE {SCOPE} AND revision_id=:id", id=identifier
                )
                if not set(revision.secret_refs.values()).issubset({row["handle"] for row in grants}):
                    raise unavailable()
                return profile, revision

        before, revision = await checked()
        async with asyncio.timeout(5):
            values = {
                slot: (await resolve_secret(partial(self.connections.secrets.resolve, scope, handle))).value
                for slot, handle in revision.secret_refs.items()
            }
        after, current = await checked()
        if before != after or revision != current:
            raise unavailable()
        return before, values

    async def conversation(self, tx: Transaction, identifier: UUID, *, lock_row: bool = False) -> EmailConversation:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM email_conversations WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock_row else ""),
            id=identifier,
        )
        if not rows:
            raise unavailable()
        return EmailConversation(**{key: rows[0][key] for key in EmailConversation.model_fields})

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, *, cursor: UUID | None = None, limit: int = 50
    ) -> EmailConversationDetail:
        async with self.uow.open(scope, mutation=False) as tx:
            await self.checked(tx, actor, "email.read", identifier)
            conversation = await self.conversation(tx, identifier)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM email_messages WHERE {SCOPE} AND conversation_id=:id"
                + (" AND id>:cursor" if cursor else "")
                + " ORDER BY id LIMIT :limit",
                id=identifier,
                cursor=cursor,
                limit=limit + 1,
            )
            return EmailConversationDetail(
                conversation=conversation,
                messages=tuple(self.message(row) for row in rows[:limit]),
                next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            )

    @staticmethod
    def message(row: dict[str, Any]) -> EmailMessage:
        return EmailMessage(
            id=row["id"],
            conversation_id=row["conversation_id"],
            direction=row["direction"],
            state=row["state"],
            accepted_at=row["accepted_at"],
            mail=NormalizedEmail.model_validate_json(json.dumps(row["payload"])),
        )

    @staticmethod
    def submission(row: dict[str, Any]) -> EmailSubmission:
        return EmailSubmission(
            id=row["id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            state=row["state"],
            accepted_recipients=tuple(row["result"].get("accepted_recipients", ())),
            rejected_recipients=tuple(row["result"].get("rejected_recipients", ())),
        )

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None
    ) -> dict[str, object]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid page size")
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            # Filter resources before pagination: restricted grants never reveal hidden subjects.
            permitted = [
                g for g in current.grants if "email.read" in ROLE_CAPABILITIES[g.role] and contains(g.scope, scope)
            ]
            if not current.active or current.kind == "worker" or not permitted:
                await self.checked(tx, current, "email.read")
            resources = tuple({v for g in permitted for v in g.resources})
            restricted = all(g.resources for g in permitted)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM email_conversations WHERE {SCOPE}"
                + (" AND cast(id AS text)=ANY(:resources)" if restricted else "")
                + (" AND id>:cursor" if cursor else "")
                + " ORDER BY id LIMIT :limit",
                resources=list(resources),
                limit=limit + 1,
                cursor=cursor,
            )
            return {
                "items": [
                    EmailConversation(**{key: row[key] for key in EmailConversation.model_fields}).model_dump(
                        mode="json"
                    )
                    for row in rows[:limit]
                ],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def queue(
        self,
        actor: Principal,
        scope: Scope,
        request: EmailSendRequest | EmailReplyRequest,
        conversation_id: UUID | None = None,
        *,
        automated: bool = False,
    ) -> EmailSubmission:
        async with self.uow.open(scope) as tx:
            actor = await self.checked(tx, actor, "email.send", conversation_id)
            profile = await self.connection(tx, actor, request.connection_revision_id)
            db = RuntimeRepository(tx)
            await lock(tx, f"email-command:{scope}:{actor.id}:{request.request_id}")
            fingerprint = canonical_digest(
                {"conversation": str(conversation_id), "request": request.model_dump(mode="json")}
            )
            prior = await db.rows(
                f"SELECT * FROM email_submissions WHERE {SCOPE} AND principal_id=:actor AND request_id=:request",
                actor=actor.id,
                request=request.request_id,
            )
            if prior:
                if prior[0]["fingerprint"] != fingerprint:
                    raise CatalogError(409, "WV-IDEMPOTENCY-CONFLICT", "Email command changed")
                return self.submission(prior[0])
            parent = None
            if isinstance(request, EmailReplyRequest):
                if conversation_id is None:
                    raise unavailable()
                await self.checked(tx, actor, "email.read", conversation_id)
                conversation = await self.conversation(tx, conversation_id, lock_row=True)
                if conversation.connection_revision_id != request.connection_revision_id:
                    raise unavailable()
                rows = await db.rows(
                    f"SELECT * FROM email_messages WHERE {SCOPE} AND id=:id AND "
                    f"conversation_id=:conversation AND connection_revision_id=:connection",
                    id=request.parent_message_id,
                    conversation=conversation_id,
                    connection=request.connection_revision_id,
                )
                if not rows:
                    raise unavailable()
                parent = NormalizedEmail.model_validate_json(json.dumps(rows[0]["payload"]))
                if parent.automated:
                    raise CatalogError(409, "WV-EMAIL-AUTOMATED", "Automated mail cannot be replied to automatically")
                recipient = (
                    (parent.reply_to or parent.sender)
                    if rows[0]["direction"] == "inbound"
                    else (parent.to[0] if parent.to else parent.sender)
                )
                extra = (*parent.to, *parent.cc) if request.reply_all else ()
                recipients = tuple(dict.fromkeys(v for v in (recipient, *extra) if v != profile.sender))
                request_send = EmailSendRequest(
                    request_id=request.request_id,
                    connection_revision_id=request.connection_revision_id,
                    to=recipients,
                    subject="Re: " + re.sub(r"^(?:re:\s*)+", "", parent.subject, flags=re.I),
                    text=request.text,
                    html=request.html,
                    attachments=request.attachments,
                )
            else:
                if conversation_id is not None:
                    raise unavailable()
                request_send = request
                conversation_id = uuid4()
                await db.execute(
                    "INSERT INTO "
                    "email_conversations(id,tenant_id,project_id,environment_id,connection_revision_id,sub"
                    "ject) VALUES(:id,:tenant,:project,:environment,:connection,:subject)",
                    id=conversation_id,
                    connection=request.connection_revision_id,
                    subject=request.subject,
                )
            if automated:
                counts = await db.rows(
                    f"SELECT count(*) AS count FROM email_submissions WHERE {SCOPE} AND "
                    f"conversation_id=:conversation AND request->>'automated'='true'",
                    conversation=conversation_id,
                )
                if counts[0]["count"] >= profile.automated_response_limit:
                    raise CatalogError(409, "WV-EMAIL-RESPONSE-BUDGET", "Automated response budget exhausted")
            recipients = tuple(dict.fromkeys((*request_send.to, *request_send.cc, *request_send.bcc)))
            if not set(recipients).issubset(profile.allowed_recipients):
                raise CatalogError(422, "WV-EMAIL-RECIPIENT", "Recipient policy denied")
            submission_id, message_id = uuid4(), uuid4()
            stable_id = f"<{submission_id}@{profile.sender.split('@')[1]}>"
            raw = build_message(request_send, profile.sender, stable_id, parent=parent, automated=automated)
            mail = parse_message(raw)
            await db.execute(
                "INSERT INTO "
                "email_messages(id,tenant_id,project_id,environment_id,conversation_id,connection_revi"
                "sion_id,direction,state,payload) "
                "VALUES(:id,:tenant,:project,:environment,:conversation,:connection,'outbound','queued"
                "',cast(:payload AS jsonb))",
                id=message_id,
                conversation=conversation_id,
                connection=request.connection_revision_id,
                payload=mail.model_dump_json(),
            )
            await db.execute(
                "INSERT INTO "
                "email_submissions(id,tenant_id,project_id,environment_id,principal_id,request_id,fing"
                "erprint,connection_revision_id,conversation_id,message_id,state,request) "
                "VALUES(:id,:tenant,:project,:environment,:actor,:request,:fingerprint,:connection,:co"
                "nversation,:message,'queued',cast(:payload AS jsonb))",
                id=submission_id,
                actor=actor.id,
                request=request.request_id,
                fingerprint=fingerprint,
                connection=request.connection_revision_id,
                conversation=conversation_id,
                message=message_id,
                payload=json.dumps({"raw": raw.decode("ascii"), "recipients": recipients, "automated": automated}),
            )
            await audit(
                tx.session,
                actor,
                "email.queue",
                str(submission_id),
                scope=scope,
                capability="email.send",
                context=AuditContext(),
            )
            return EmailSubmission(
                id=submission_id, conversation_id=conversation_id, message_id=message_id, state="queued"
            )

    async def status(self, actor: Principal, scope: Scope, identifier: UUID) -> EmailSubmission:
        async with self.uow.open(scope, mutation=False) as tx:
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM email_submissions WHERE {SCOPE} AND id=:id", id=identifier
            )
            if not rows:
                raise unavailable()
            await self.checked(tx, actor, "email.read", rows[0]["conversation_id"])
            return self.submission(rows[0])

    async def execute(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> EmailSubmission:
        # The committed attempting fence precedes all network writes. Expired attempts never resend.
        async with self.uow.open(scope) as tx:
            db = RuntimeRepository(tx)
            rows = await db.rows(f"SELECT * FROM email_submissions WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier)
            if not rows:
                raise unavailable()
            row = rows[0]
            owner = await load_principal(tx.session, row["principal_id"])
            await self.checked(tx, actor, "email.send", row["conversation_id"])
            await self.checked(tx, owner, "email.send", row["conversation_id"])
            if row["state"] == "attempting":
                if row["lease_until"] <= await db.now():
                    await db.execute(
                        f"""UPDATE email_submissions SET state='unknown' WHERE {SCOPE} AND id=:id""", id=identifier
                    )
                    await db.execute(
                        f"UPDATE email_messages SET state='unknown' WHERE {SCOPE} AND id=:id", id=row["message_id"]
                    )
                    row["state"] = "unknown"
                return self.submission(row)
            if row["state"] != "queued":
                return self.submission(row)
            await self.connection(tx, owner, row["connection_revision_id"])
            generation = row["generation"] + 1
            await db.execute(
                f"UPDATE email_submissions SET "
                f"state='attempting',generation=:generation,lease_until=clock_timestamp()+interval '65 "
                f"seconds' WHERE {SCOPE} AND id=:id",
                id=identifier,
                generation=generation,
            )
            await db.execute(
                f"UPDATE email_messages SET state='attempting' WHERE {SCOPE} AND id=:id", id=row["message_id"]
            )
        profile, credentials = await self.credentials(scope, owner, row["connection_revision_id"])
        if authorize is not None:
            await authorize()
        result = await self.transport.send(
            profile, tuple(row["request"]["recipients"]), row["request"]["raw"].encode("ascii"), credentials
        )
        async with self.uow.open(scope) as tx:
            db = RuntimeRepository(tx)
            current = (
                await db.rows(f"SELECT * FROM email_submissions WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier)
            )[0]
            if current["state"] != "attempting" or current["generation"] != generation:
                return self.submission(current)
            await db.execute(
                f"""UPDATE email_submissions SET state=:state,result=cast(:result AS jsonb) WHERE {SCOPE} AND id=:id""",
                id=identifier,
                state=result.state,
                result=json.dumps(
                    {
                        "accepted_recipients": result.accepted_recipients,
                        "rejected_recipients": result.rejected_recipients,
                    }
                ),
            )
            await db.execute(
                f"""UPDATE email_messages SET state=:state WHERE {SCOPE} AND id=:id""",
                id=row["message_id"],
                state=result.state,
            )
            current.update(
                state=result.state,
                result={
                    "accepted_recipients": result.accepted_recipients,
                    "rejected_recipients": result.rejected_recipients,
                },
            )
            return self.submission(current)
