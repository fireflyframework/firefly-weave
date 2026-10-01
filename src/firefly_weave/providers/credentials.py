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
"""Exact-source credential access through the connections-owned bounded resolver.

Verifiers inject this native service without creating an ingress-service cycle.
Provider I/O occurs outside database transactions; source and owner authority are
checked on both sides by the standing binding resolver.
"""

from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.repository import load_principal
from firefly_weave.connections.models import unavailable
from firefly_weave.connections.source_bindings import SourceBindingService
from firefly_weave.contracts.connectors import ConnectionRevision, ResolvedSecret
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.repository import ProviderRepository
from firefly_weave.runtime.service import RuntimeService


@service
class ProviderCredentials:
    def __init__(self, bindings: SourceBindingService, runtime: RuntimeService) -> None:
        self.bindings, self.runtime = bindings, runtime

    async def resolve(
        self, source: ProviderSource, *, slots: tuple[str, ...] | None = None
    ) -> tuple[ConnectionRevision, dict[str, ResolvedSecret]]:
        async def check(tx: Transaction) -> UUID:
            current = await ProviderRepository(tx).source(source.id)
            if current != source or current.disabled:
                raise unavailable()
            actor = await load_principal(tx.session, current.principal_id)
            for capability in ("trigger.manage", "run.start" if current.kind == "run" else "run.signal"):
                self.runtime.require(actor, tx.scope, capability, AuditContext())
            return current.binding_id

        return await self.bindings.resolve(source.scope, source.binding_id, check, slots=slots)
