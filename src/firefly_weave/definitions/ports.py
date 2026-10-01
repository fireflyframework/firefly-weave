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

"""Runtime-only catalog dependencies; compiler imports never load this module."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision
from firefly_weave.contracts.workers import ConnectorExecutionPin
from firefly_weave.persistence.uow import Transaction


@runtime_checkable
class ConnectionBindingPort(Protocol):
    async def resolve_binding(
        self,
        actor: Principal,
        scope: Scope,
        slot: str,
        revision_id: UUID,
        connector: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> BoundConnection: ...

    async def read(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> ConnectionRevision: ...


@runtime_checkable
class WorkerAdmissionPort(Protocol):
    async def capabilities(self, tx: Transaction, base: CatalogSnapshot) -> CatalogSnapshot: ...

    async def admit(
        self,
        actor: Principal,
        scope: Scope,
        request: ActivationRequest,
        artifact: CompiledArtifact,
        *,
        capability: str,
        context: AuditContext,
        tx: Transaction,
    ) -> list[ConnectorExecutionPin]: ...
