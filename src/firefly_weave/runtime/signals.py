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

"""Authorized durable inbox acceptance and atomic wait advancement."""

import json
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import SignalNode
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.runtime import SignalReceipt, SignalRequest
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import validate, workflow
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService, view_of
from firefly_weave.runtime.waits import TERMINAL, settle


@service
class SignalService:
    def __init__(self, runtime: RuntimeService, access: AccessService) -> None:
        self.runtime = runtime
        self.access = access

    async def deliver(
        self,
        tx: Transaction,
        run_id: UUID,
        event_id: str,
        name: str,
        payload: JsonValue,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> SignalReceipt:
        self.runtime.require(actor, scope, "run.signal", context)
        async with self.runtime.definitions.transaction(scope, tx) as tx:
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            row = await repository.run(run_id, lock=True)
            self.runtime.require(await self.access.load_principal(actor.id, tx=tx), scope, "run.signal", context)
            view_of(row)
            try:
                measure_value(payload)
            except ValueError as error:
                raise CatalogError(422, "WV-SIGNAL-PAYLOAD", "Signal payload exceeds runtime limits") from error
            request = SignalRequest(eventId=event_id, name=name, payload=payload)
            fingerprint = canonical_digest(request.model_dump(mode="json"))
            prior = await repository.rows(
                f"SELECT * FROM signal_receipts WHERE {SCOPE} AND run_id=:run AND external_event_id=:event",
                run=run_id,
                event=event_id,
            )
            if prior:
                if prior[0]["request_hash"] != fingerprint:
                    raise CatalogError(409, "WV-SIGNAL-CONFLICT", "Signal identity payload conflict")
                return SignalReceipt(id=prior[0]["id"], request_hash=fingerprint, accepted_at=prior[0]["accepted_at"])
            self.runtime.definitions.registry.require_operational()
            await repository.require_work(run_id, row["state"])
            if row["state"]["status"] in TERMINAL:
                raise CatalogError(409, "WV-SIGNAL-TERMINAL", "Run is terminal")
            ir = workflow(import_artifact(row["artifact"]))
            node = next((n for n in ir.graph.nodes if isinstance(n, SignalNode) and n.name == name), None)
            if node is None:
                raise CatalogError(422, "WV-SIGNAL-NAME", "Unknown signal name")
            try:
                validate(ir, ir.schemas[node.schema_ref], payload)
            except ValueError as error:
                raise CatalogError(422, "WV-SIGNAL-PAYLOAD", "Signal payload violates pinned schema") from error
            receipt = SignalReceipt(id=uuid4(), request_hash=fingerprint, accepted_at=await repository.now())
            await repository.execute(
                "INSERT INTO signal_receipts "
                "VALUES(:id,:tenant,:project,:environment,:run,:event,:name,cast(:payload AS "
                "jsonb),:hash,:accepted,false)",
                id=receipt.id,
                run=run_id,
                event=event_id,
                name=name,
                payload=json.dumps(payload),
                hash=fingerprint,
                accepted=receipt.accepted_at,
            )
            await settle(repository, row)
            await audit(
                tx.session,
                actor,
                "run.signal",
                str(run_id),
                scope=scope,
                capability="run.signal",
                context=context.model_copy(update={"run_id": run_id}),
            )
            return receipt
