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

"""Native connector dispatch reuses the same current-authority task service as remote workers."""

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.service import AccessService
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ActionContext, ResolvedSecret
from firefly_weave.contracts.email import EmailReplyRequest, EmailSendRequest
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import (
    CompletionAcknowledgment,
    CredentialRequest,
    LeaseProof,
    TaskError,
    TaskLease,
)
from firefly_weave.email.service import EmailService
from firefly_weave.operations.execution import ReservedSlots, request_execution
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.workers.leases import TaskService

if TYPE_CHECKING:
    from firefly_weave.providers.teams.references import TeamsReferences


@service
class ConnectorExecutionService:
    def __init__(
        self,
        tasks: TaskService,
        access: AccessService,
        uow: UnitOfWork,
        registry: ConnectorRegistry,
        email: EmailService,
    ) -> None:
        self.tasks, self.access, self.uow, self.registry = tasks, access, uow, registry
        self.email = email

    async def operation(self, name: str, scope: Scope, principal_id: UUID, *args: Any) -> Any:
        if name not in {"claim", "heartbeat", "complete", "fail", "invocation"}:
            raise ValueError("Unsupported native task operation")
        async with self.uow.open(scope) as tx:
            actor = await self.access.load_principal(principal_id, tx=tx)
            return await getattr(self.tasks, name)(tx, *args, actor=actor, scope=scope, context=AuditContext())

    async def execute(
        self, scope: Scope, principal_id: UUID, lease: TaskLease, *, reservation: ReservedSlots | None = None
    ) -> JsonValue:
        self.registry.require_operational()
        # A request lease covers the whole connector call. In-process native workers bring their
        # own reservation instead, whose slots only the call's pure work takes.
        async with request_execution() if reservation is None else reservation.execution():
            return await self._execute(scope, principal_id, lease)

    async def _execute(self, scope: Scope, principal_id: UUID, lease: TaskLease) -> JsonValue:
        adapter, invocation = await self.operation("invocation", scope, principal_id, lease.proof)
        active = True

        async def credentials(slot: str) -> ResolvedSecret:
            if not active or slot not in invocation.connection.secret_refs:
                raise ValueError("Credential unavailable")
            actor = await self.access.load_principal(principal_id)
            return await self.tasks.credentials(
                CredentialRequest(lease=lease.proof, connection_revision_id=invocation.connection.id, slot=slot),
                actor=actor,
                scope=scope,
                context=AuditContext(),
            )

        async def authorize() -> None:
            self.registry.require_operational()
            if not active:
                raise ValueError("Connector authority unavailable")
            current_adapter, current = await self.operation("invocation", scope, principal_id, lease.proof)
            if current_adapter != adapter or current != invocation:
                raise ValueError("Connector authority changed")
            actor = await self.access.load_principal(principal_id)
            for slot in invocation.connection.secret_refs:
                await self.tasks.credential_authority(
                    CredentialRequest(lease=lease.proof, connection_revision_id=invocation.connection.id, slot=slot),
                    actor=actor,
                    scope=scope,
                    context=AuditContext(),
                )

        async def reference(identifier: UUID, generation: int, activity_id: str | None) -> JsonObject:
            await authorize()
            if adapter != "weave-teams":
                raise ValueError("Reference unavailable")
            references = cast("TeamsReferences", self.registry.builtin_verifier(adapter).references)
            return await references.resolve(scope, invocation.connection.id, identifier, generation, activity_id)

        async def email_submit(payload: JsonObject) -> JsonObject:
            await authorize()
            if adapter != "weave-email" or invocation.action not in {"send", "reply"}:
                raise ValueError("Email authority unavailable")
            async with self.uow.open(scope, mutation=False) as tx:
                rows = await RuntimeRepository(tx).rows(
                    "SELECT r.principal_id FROM task_intents t JOIN runs r ON r.id=t.run_id "
                    "WHERE t.tenant_id=:tenant AND t.project_id=:project AND t.environment_id=:environment "
                    "AND t.id=:id",
                    id=lease.proof.task_id,
                )
                if not rows:
                    raise ValueError("Email run unavailable")
                owner = await self.access.load_principal(rows[0]["principal_id"], tx=tx)
            values = {
                **payload,
                "request_id": str(uuid5(NAMESPACE_URL, "weave-email:" + lease.operation_key)),
                "connection_revision_id": str(invocation.connection.id),
            }
            command: EmailSendRequest | EmailReplyRequest
            conversation_id = None
            if invocation.action == "reply":
                conversation_id = UUID(str(values.pop("conversation_id")))
                command = EmailReplyRequest.model_validate_json(json.dumps(values))
            else:
                command = EmailSendRequest.model_validate_json(json.dumps(values))
            submission = await self.email.queue(owner, scope, command, conversation_id, automated=True)
            await authorize()
            result = await self.email.execute(owner, scope, submission.id, authorize=authorize)
            return cast(JsonObject, result.model_dump(mode="json"))

        try:
            return await self.registry.get(adapter).execute(
                cast(JsonObject, lease.input),
                ActionContext(
                    lease.operation_key, lease.deadline, credentials, invocation, authorize, reference, email_submit
                ),
            )
        finally:
            active = False


# Admission rejections: the operation ran nothing, so the identical call may be sent again.
CAPACITY_CODES = frozenset({"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"})


def _capacity_rejected(error: BaseException) -> bool:
    from firefly_weave.definitions.models import CatalogError

    return isinstance(error, CatalogError) and error.status == 429 and error.code in CAPACITY_CODES


class ServiceTransport:
    """The in-process worker transport, with the remote transport's replay policy (sdk/transport.py)."""

    def __init__(self, service: ConnectorExecutionService, scope: Scope, principal_id: UUID, instance_id: UUID) -> None:
        self.service, self.scope, self.principal_id, self.instance_id = service, scope, principal_id, instance_id

    async def claim(self, limit: int) -> list[TaskLease]:
        from firefly_weave.definitions.models import CatalogError

        try:
            return cast(
                list[TaskLease],
                await self.service.operation("claim", self.scope, self.principal_id, self.instance_id, limit),
            )
        except CatalogError as error:
            # Effects open while compatibility still reports restricted; claim nothing until it is
            # operational instead of ending the in-process worker (and readiness) for good.
            if error.status == 503 and error.code == "WV-COMPATIBILITY":
                return []
            # A busy database turned the claim away before it ran: claim nothing and poll again.
            if _capacity_rejected(error):
                return []
            raise

    async def _replay(self, name: str, *args: Any, settlement: bool = False) -> Any:
        """Run one task operation, sending it again while the platform rejects it for capacity.

        Settlement (complete, fail) can outlast a short admission burst; renewal keeps a short
        window. The arguments, including the completion identity, are identical on every attempt.
        """
        try:
            return await self.service.operation(name, self.scope, self.principal_id, *args)
        except Exception as error:
            if not _capacity_rejected(error):
                raise
            rejected = error
        attempts, seconds = (48, 10) if settlement else (3, 1)
        try:
            async with asyncio.timeout(seconds):
                for retry in range(attempts - 1):
                    await asyncio.sleep(min(0.05 * 2 ** min(retry, 3), 0.25))
                    try:
                        return await self.service.operation(name, self.scope, self.principal_id, *args)
                    except Exception as error:
                        if not _capacity_rejected(error):
                            raise
                        rejected = error
        except TimeoutError:
            pass
        raise rejected

    async def heartbeat(self, lease: LeaseProof) -> TaskLease:
        return cast(TaskLease, await self._replay("heartbeat", lease))

    async def complete(self, lease: LeaseProof, completion_id: UUID, output: JsonValue) -> CompletionAcknowledgment:
        return cast(
            CompletionAcknowledgment,
            await self._replay("complete", lease, completion_id, output, settlement=True),
        )

    async def fail(self, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment:
        return cast(CompletionAcknowledgment, await self._replay("fail", lease, error, settlement=True))
