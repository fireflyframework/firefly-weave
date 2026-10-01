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

from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.service import AccessService
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ActionContext, ResolvedSecret
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import (
    CompletionAcknowledgment,
    CredentialRequest,
    LeaseProof,
    TaskError,
    TaskLease,
)
from firefly_weave.operations.execution import request_execution
from firefly_weave.persistence.uow import UnitOfWork
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
    ) -> None:
        self.tasks, self.access, self.uow, self.registry = tasks, access, uow, registry

    async def operation(self, name: str, scope: Scope, principal_id: UUID, *args: Any) -> Any:
        if name not in {"claim", "heartbeat", "complete", "fail", "invocation"}:
            raise ValueError("Unsupported native task operation")
        async with self.uow.open(scope) as tx:
            actor = await self.access.load_principal(principal_id, tx=tx)
            return await getattr(self.tasks, name)(tx, *args, actor=actor, scope=scope, context=AuditContext())

    async def execute(self, scope: Scope, principal_id: UUID, lease: TaskLease) -> JsonValue:
        self.registry.require_operational()
        async with request_execution():
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

        try:
            return await self.registry.get(adapter).execute(
                cast(JsonObject, lease.input),
                ActionContext(lease.operation_key, lease.deadline, credentials, invocation, authorize, reference),
            )
        finally:
            active = False


class ServiceTransport:
    def __init__(self, service: ConnectorExecutionService, scope: Scope, principal_id: UUID, instance_id: UUID) -> None:
        self.service, self.scope, self.principal_id, self.instance_id = service, scope, principal_id, instance_id

    async def claim(self, limit: int) -> list[TaskLease]:
        return cast(
            list[TaskLease],
            await self.service.operation("claim", self.scope, self.principal_id, self.instance_id, limit),
        )

    async def heartbeat(self, lease: LeaseProof) -> TaskLease:
        return cast(TaskLease, await self.service.operation("heartbeat", self.scope, self.principal_id, lease))

    async def complete(self, lease: LeaseProof, completion_id: UUID, output: JsonValue) -> CompletionAcknowledgment:
        return cast(
            CompletionAcknowledgment,
            await self.service.operation("complete", self.scope, self.principal_id, lease, completion_id, output),
        )

    async def fail(self, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment:
        return cast(
            CompletionAcknowledgment, await self.service.operation("fail", self.scope, self.principal_id, lease, error)
        )
