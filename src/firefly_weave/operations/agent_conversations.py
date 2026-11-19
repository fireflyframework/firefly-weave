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

"""Agent conversations in an environment: listing, reading and deleting them, and the lifetime sweep.

Reading needs ``run.read`` and deleting ``run.purge``, checked on the token's principal before any unit opens and on
the stored principal inside it. A conversation is named by its workflow and key hash, never by its key, so no answer
and no audit row ever holds a key. An expired conversation is never listed or read, even before the sweep deletes it.
"""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_conversations import (
    LOADED_BY_LIMIT,
    PURGE_BATCH,
    SWEEP_BATCH,
    AgentConversation,
    AgentConversationDeletion,
    AgentConversationQuery,
    AgentConversationSummary,
    ConversationKeyHash,
    ConversationPurge,
    ConversationPurgeRequest,
    ConversationWorkflow,
)
from firefly_weave.contracts.public import Page
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction, UnitOfWork

_SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


async def sweep(tx: Transaction, now: datetime, *, batch: int = SWEEP_BATCH) -> int:
    """Delete one batch of the environment's expired conversations, the longest expired first; returns how many went.

    Rows another transaction holds (an append in flight) are skipped. Each chosen row is locked as it is chosen, and
    PostgreSQL checks the expiry again on the row's newest version before locking it, so a conversation an append
    refreshed meanwhile is never taken. An expired conversation is never loaded, listed or read, so a sweep that falls
    behind leaves nothing visible.
    """
    # The batch is materialized once: as an IN subquery its LIMIT and SKIP LOCKED could run again per row and
    # delete more than one batch.
    deleted = await tx.session.scalar(
        text(
            "WITH batch AS MATERIALIZED (SELECT workflow,key_hash FROM agent_conversations "
            f"WHERE {_SCOPE} AND expires_at<=:now ORDER BY expires_at LIMIT :batch FOR UPDATE SKIP LOCKED), "
            "gone AS (DELETE FROM agent_conversations c USING batch b "
            f"WHERE c.{_SCOPE.replace(' AND ', ' AND c.')} AND c.workflow=b.workflow AND c.key_hash=b.key_hash "
            "RETURNING 1) SELECT count(*) FROM gone"
        ),
        {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
            "now": now,
            "batch": batch,
        },
    )
    return int(deleted or 0)


_KEY_HASH: TypeAdapter[str] = TypeAdapter(ConversationKeyHash)
_WORKFLOW: TypeAdapter[str] = TypeAdapter(ConversationWorkflow)
_SUMMARY = "workflow,key_hash,jsonb_array_length(messages) AS message_count,version,created_at,updated_at,expires_at"
NOT_FOUND = "No conversation is stored under this workflow and key hash, or it has expired"


def conversation_name(workflow: str, key_hash: str) -> tuple[str, str]:
    """The workflow and key hash a request names, refused with 422 before anything is read when either is malformed."""
    try:
        return _WORKFLOW.validate_python(workflow), _KEY_HASH.validate_python(key_hash)
    except ValidationError:
        raise CatalogError(
            422, "WV-AGENT-CONVERSATION-KEY", "Name a workflow and a key hash of 64 lowercase hex characters"
        ) from None


def _params(scope: Scope, **values: Any) -> dict[str, Any]:
    return {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id, **values}


@service
class AgentConversationService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    def _precheck(self, actor: Principal, scope: Scope, capability: str, context: AuditContext) -> None:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Agent conversations live in an environment")
        self.authorization.require(actor, scope, capability, context=context)

    async def _current(
        self, tx: Transaction, actor: Principal, scope: Scope, capability: str, context: AuditContext
    ) -> Principal:
        current = await load_principal(tx.session, actor.id)
        self.authorization.require(current, scope, capability, context=context)
        return current

    async def list(
        self, actor: Principal, scope: Scope, query: AgentConversationQuery, *, context: AuditContext
    ) -> Page[AgentConversationSummary]:
        """A page of the environment's live conversations, the most recently updated first."""
        self._precheck(actor, scope, "run.read", context)
        filtered = hashlib.sha256(json.dumps({"workflow": query.workflow}).encode()).hexdigest()
        collection = f"agent_conversations.list:{filtered}"
        position = decode_cursor_v2(query.cursor, scope, collection)
        after = ""
        values = _params(scope, workflow=query.workflow, limit=query.limit + 1)
        if position is not None:
            sort_value, identifier = position
            workflow, _, key_hash = identifier.partition("/")
            try:
                at = datetime.fromisoformat(sort_value) if isinstance(sort_value, str) else None
            except ValueError:
                at = None
            if at is None or at.tzinfo is None:
                raise CatalogError(422, "WV-VALIDATION", "Invalid request value")
            conversation_name(workflow, key_hash)
            after = " AND (updated_at,workflow,key_hash)<(:at,:after_workflow,:after_hash)"
            values.update(at=at, after_workflow=workflow, after_hash=key_hash)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, scope, "run.read", context)
            rows = (
                (
                    await tx.session.execute(
                        text(
                            f"SELECT {_SUMMARY} FROM agent_conversations WHERE {_SCOPE} "
                            "AND expires_at>clock_timestamp() "
                            "AND (cast(:workflow AS text) IS NULL OR workflow=:workflow)"
                            f"{after} ORDER BY updated_at DESC,workflow DESC,key_hash DESC LIMIT :limit"
                        ),
                        values,
                    )
                )
                .mappings()
                .all()
            )
        items = [AgentConversationSummary.model_validate(dict(row)) for row in rows[: query.limit]]
        next_cursor = None
        if len(rows) > query.limit:
            last = items[-1]
            next_cursor = encode_cursor_v2(
                scope, collection, last.updated_at.isoformat(), f"{last.workflow}/{last.key_hash}"
            )
        return Page[AgentConversationSummary](items=items, next_cursor=next_cursor)

    async def read(
        self, actor: Principal, scope: Scope, workflow: str, key_hash: str, *, context: AuditContext
    ) -> AgentConversation:
        """One live conversation with its messages; 404 when it was never stored or has expired."""
        workflow, key_hash = conversation_name(workflow, key_hash)
        self._precheck(actor, scope, "run.read", context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, scope, "run.read", context)
            row = (
                (
                    await tx.session.execute(
                        text(
                            f"SELECT {_SUMMARY},messages,last_run_id FROM agent_conversations WHERE {_SCOPE} "
                            "AND workflow=:workflow AND key_hash=:key_hash AND expires_at>clock_timestamp()"
                        ),
                        _params(scope, workflow=workflow, key_hash=key_hash),
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise CatalogError(404, "WV-AGENT-CONVERSATION-NOT-FOUND", NOT_FOUND)
        return AgentConversation.model_validate(dict(row))

    async def delete(
        self, actor: Principal, scope: Scope, workflow: str, key_hash: str, *, context: AuditContext
    ) -> AgentConversationDeletion:
        """Delete one conversation, audited whether or not one was stored, and name the runs that loaded it, which
        keep the messages they loaded until they are purged."""
        workflow, key_hash = conversation_name(workflow, key_hash)
        self._precheck(actor, scope, "run.purge", context)
        async with self.uow.open(scope) as tx:
            current = await self._current(tx, actor, scope, "run.purge", context)
            values = _params(scope, workflow=workflow, key_hash=key_hash)
            deleted = (
                await tx.session.execute(
                    text(
                        f"DELETE FROM agent_conversations WHERE {_SCOPE} AND workflow=:workflow "
                        "AND key_hash=:key_hash RETURNING 1"
                    ),
                    values,
                )
            ).first() is not None
            runs: list[UUID] = list(
                (
                    await tx.session.execute(
                        text(
                            f"SELECT DISTINCT run_id FROM agent_memory_loads WHERE {_SCOPE} AND workflow=:workflow "
                            "AND key_hash=:key_hash AND delivered_at IS NOT NULL AND NOT dropped AND NOT test "
                            "ORDER BY run_id LIMIT :limit"
                        ),
                        {**values, "limit": LOADED_BY_LIMIT + 1},
                    )
                )
                .scalars()
                .all()
            )
            await audit(
                tx.session,
                current,
                "agent_conversation.delete",
                f"{workflow}/{key_hash}",
                scope=scope,
                capability="run.purge",
                context=context,
                details={"deleted": deleted},
            )
        return AgentConversationDeletion(
            workflow=workflow,
            key_hash=key_hash,
            deleted=deleted,
            loaded_by=list(runs[:LOADED_BY_LIMIT]),
            more_loaded_by=len(runs) > LOADED_BY_LIMIT,
        )

    async def purge_workflow(
        self,
        actor: Principal,
        scope: Scope,
        workflow: str,
        request: ConversationPurgeRequest,
        key: str,
        *,
        context: AuditContext,
    ) -> ConversationPurge:
        """Delete up to 1,000 of a workflow's conversations; ``more`` says to call again. The body must name the
        workflow in the path, and a repeated idempotency key answers what the first call did."""
        try:
            workflow = _WORKFLOW.validate_python(workflow)
        except ValidationError:
            raise CatalogError(422, "WV-AGENT-CONVERSATION-KEY", "Name a workflow") from None
        self._precheck(actor, scope, "run.purge", context)
        if request.confirm_workflow != workflow:
            raise CatalogError(422, "WV-AGENT-CONVERSATION-CONFIRM", "Confirm the exact workflow name")
        if not key or len(key) > 200:
            raise CatalogError(422, "WV-IDEMPOTENCY", "A bounded Idempotency-Key is required")
        async with self.uow.open(scope) as tx:
            current = await self._current(tx, actor, scope, "run.purge", context)
            replay = Idempotency(
                tx,
                current.id,
                f"agent-conversations:{scope.environment_id}:{workflow}:purge",
                key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return ConversationPurge.model_validate(prior)
            values = _params(scope, workflow=workflow, batch=PURGE_BATCH)
            # The batch is materialized once, so a purge never deletes more than one batch.
            deleted = await tx.session.scalar(
                text(
                    "WITH batch AS MATERIALIZED (SELECT workflow,key_hash FROM agent_conversations "
                    f"WHERE {_SCOPE} AND workflow=:workflow ORDER BY key_hash LIMIT :batch FOR UPDATE), "
                    "gone AS (DELETE FROM agent_conversations c USING batch b "
                    f"WHERE c.{_SCOPE.replace(' AND ', ' AND c.')} AND c.workflow=b.workflow "
                    "AND c.key_hash=b.key_hash RETURNING 1) SELECT count(*) FROM gone"
                ),
                values,
            )
            more = (
                await tx.session.execute(
                    text(f"SELECT 1 FROM agent_conversations WHERE {_SCOPE} AND workflow=:workflow LIMIT 1"), values
                )
            ).first() is not None
            result = ConversationPurge(workflow=workflow, deleted=int(deleted or 0), more=more)
            await audit(
                tx.session,
                current,
                "agent_conversation.purge_workflow",
                workflow,
                scope=scope,
                capability="run.purge",
                context=context,
                details={"deleted": result.deleted, "more": more},
            )
            await replay.save(result.model_dump(mode="json"))
        return result
