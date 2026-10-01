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

"""Current scoped read authority and bounded, immutable accepted-event history."""

import base64
import json
from typing import Any
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService
from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.operations import (
    AuxiliaryReceipt,
    EventPage,
    HistoryExport,
    RecordedEvidence,
    ReplayDiagnostic,
    ReplayReport,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.exports import bounded_size, safe_event
from firefly_weave.operations.redaction import Omission
from firefly_weave.operations.replay import MAX_BYTES, replay
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.admission import unavailable
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService


class HistoryCursor(ContractModel):
    scope: Scope
    run_id: UUID
    last: int
    high_water: int


def encode(cursor: HistoryCursor) -> str:
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode()


def decode(value: str, scope: Scope, run_id: UUID, high_water: int) -> HistoryCursor:
    try:
        if len(value) > 2048:
            raise ValueError
        cursor = HistoryCursor.model_validate_json(base64.b64decode(value, altchars=b"-_", validate=True))
        if cursor.scope != scope or cursor.run_id != run_id or not 0 <= cursor.last < cursor.high_water <= high_water:
            raise ValueError
        return cursor
    except (ValueError, UnicodeError):
        raise CatalogError(422, "WV-HISTORY-CURSOR", "Cursor does not identify this scoped history") from None


@service
class HistoryService:
    def __init__(self, runtime: RuntimeService, access: AccessService) -> None:
        self.runtime, self.access = runtime, access

    async def _authorize(self, tx: Transaction, actor: Principal, scope: Scope, context: AuditContext) -> None:
        self.runtime.require(await self.access.load_principal(actor.id, tx=tx), scope, "run.read", context)

    async def _events(
        self, tx: Transaction, row: dict[str, Any], last: int, high: int, limit: int, *, byte_limit: int = MAX_BYTES
    ) -> tuple[list[RuntimeEvent], bool]:
        repository = RuntimeRepository(tx)
        hidden = unavailable(row)
        artifact = None if hidden else import_artifact(row["artifact"])
        query = text(
            "SELECT e.id,e.sequence,e.type,e.created_at, "
            "CASE WHEN octet_length(e.data::text)+coalesce(octet_length(f.evidence::text),0)<=:bytes "
            "THEN e.data END AS data, "
            "CASE WHEN octet_length(e.data::text)+coalesce(octet_length(f.evidence::text),0)<=:bytes "
            "THEN f.evidence END AS evidence, "
            "octet_length(e.data::text)+coalesce(octet_length(f.evidence::text),0)>:bytes AS oversized "
            "FROM run_events e "
            "LEFT JOIN run_event_evidence f USING(tenant_id,project_id,environment_id,run_id,id,sequence) "
            "WHERE e.tenant_id=:tenant AND e.project_id=:project AND e.environment_id=:environment "
            "AND e.run_id=:run AND e.sequence>:last AND e.sequence<=:high ORDER BY e.sequence LIMIT :limit"
        )
        result = await tx.session.stream(
            query,
            {
                **repository.params,
                "run": row["id"],
                "last": last,
                "high": high,
                "limit": limit,
                "bytes": max(0, byte_limit),
            },
            execution_options={"yield_per": 1},
        )
        events: list[RuntimeEvent] = []
        try:
            async for event_row in result.mappings():
                raw = dict(event_row)
                if raw["oversized"]:
                    return events, True
                event = RuntimeEvent.model_validate_json(
                    json.dumps(
                        {
                            "id": raw["id"],
                            "sequence": raw["sequence"],
                            "type": raw["type"],
                            "data": raw["data"],
                            "timestamp": raw["created_at"],
                            "evidence": raw["evidence"],
                        },
                        default=str,
                    )
                )
                projected = safe_event(event, artifact, unavailable=hidden)
                wire_size = bounded_size(projected, byte_limit)
                if wire_size is None:
                    return events, True
                byte_limit -= wire_size + 1
                events.append(projected)
        finally:
            await result.close()
        return events, False

    async def page(
        self,
        tx: Transaction,
        run_id: UUID,
        cursor: str | None,
        limit: int,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> EventPage:
        self.runtime.require(actor, scope, "run.read", context)
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-HISTORY-LIMIT", "History page limit is 1 through 100")
        async with self.runtime.definitions.transaction(scope, tx, mutation=False) as enlisted:
            await self._authorize(enlisted, actor, scope, context)
            row = await RuntimeRepository(enlisted).run(run_id)
            high = row["state"]["accepted_sequence"]
            position = (
                decode(cursor, scope, run_id, high)
                if cursor
                else HistoryCursor(scope=scope, run_id=run_id, last=0, high_water=high)
            )
            events, limited = await self._events(enlisted, row, position.last, position.high_water, limit)
            last = events[-1].sequence if events else position.last
            if limited and not events:
                raise CatalogError(422, "WV-HISTORY-LIMIT", "A recorded event exceeds the inspection budget")
            return EventPage(
                run_id=run_id,
                scope=scope,
                high_water_sequence=position.high_water,
                events=events,
                next_cursor=encode(position.model_copy(update={"last": last}))
                if events and last < position.high_water
                else None,
            )

    async def export(
        self, tx: Transaction, run_id: UUID, limit: int = 1000, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> HistoryExport:
        self.runtime.require(actor, scope, "run.read", context)
        if not 1 <= limit <= 1000:
            raise CatalogError(422, "WV-HISTORY-LIMIT", "Export limit is 1 through 1000")
        async with self.runtime.definitions.transaction(scope, tx, mutation=False) as enlisted:
            await self._authorize(enlisted, actor, scope, context)
            repository = RuntimeRepository(enlisted)
            # The outer repeatable-read transaction fixes accepted facts and receipts together.
            row = await repository.run(run_id)
            await self._authorize(enlisted, actor, scope, context)
            high = row["state"]["accepted_sequence"]
            auxiliary_high_water = await repository.now()
            artifact: CompiledArtifact | None = None
            if not unavailable(row):
                artifact = import_artifact(row["artifact"])
            artifact_bytes = len(artifact.to_bytes()) if artifact is not None else 0
            events, limited = await self._events(
                enlisted, row, 0, high, limit, byte_limit=MAX_BYTES - artifact_bytes - 64 * 1024
            )
            source = None
            if events and events[0].evidence is not None and artifact is not None:
                source = RecordedEvidence.model_validate_json(json.dumps(events[0].evidence)).source
            receipts = await repository.rows(
                "SELECT c.task_id,c.generation,c.completion_id AS receipt_id,c.payload->>'status' AS status,"
                "(c.payload->>'accepted_at')::timestamptz AS accepted_at FROM completion_receipts c "
                "JOIN task_intents t ON t.id=c.task_id AND t.tenant_id=c.tenant_id AND t.project_id=c.project_id "
                "AND t.environment_id=c.environment_id WHERE c.tenant_id=:tenant AND c.project_id=:project "
                "AND c.environment_id=:environment AND t.run_id=:run AND c.payload->>'status'='ignored' "
                "AND (c.payload->>'accepted_at')::timestamptz<=:high "
                "ORDER BY (c.payload->>'accepted_at')::timestamptz,c.completion_id LIMIT 101",
                run=run_id,
                high=auxiliary_high_water,
            )
            bounded = limited or bool(high and (not events or events[-1].sequence < high))
            report = (
                replay(artifact, tuple(events))
                if artifact is not None
                else ReplayReport(
                    status="incomplete", diagnostics=[ReplayDiagnostic(code="WV-REPLAY-UNAVAILABLE", sequence=0)]
                )
            )
            if bounded and report.status != "inconsistent":
                report = report.model_copy(
                    update={
                        "status": "incomplete",
                        "diagnostics": [
                            ReplayDiagnostic(
                                code="WV-REPLAY-LIMIT" if limited else "WV-REPLAY-BOUNDED-PREFIX",
                                sequence=report.last_verified_sequence,
                            )
                        ],
                    }
                )
            omissions = [Omission(path="/events", reason="uncertain_derived")] if artifact is None else []
            exported = HistoryExport(
                run_id=run_id,
                scope=scope,
                artifact_digest=artifact.digest if artifact is not None else None,
                source_reference=source,
                source_map=artifact.source_map if artifact is not None else {},
                node_paths={n.id: n.path for n in workflow(artifact).graph.nodes} if artifact is not None else {},
                events=events,
                high_water_sequence=high,
                bounded_prefix=bounded,
                auxiliary_receipts=[AuxiliaryReceipt.model_validate(r) for r in receipts[:100]],
                auxiliary_high_water=auxiliary_high_water,
                auxiliary_complete=len(receipts) <= 100,
                replay=report,
                omissions=omissions,
            )

            if bounded_size(exported, MAX_BYTES) is None:
                report = report.model_copy(
                    update={
                        "status": "incomplete",
                        "final_state": None,
                        "diagnostics": [
                            ReplayDiagnostic(code="WV-REPLAY-OUTPUT-LIMIT", sequence=report.last_verified_sequence)
                        ],
                        "omissions": [Omission(path="/final_state", reason="resource_limit")],
                    }
                )
                exported = exported.model_copy(update={"replay": report})
                if bounded_size(exported, MAX_BYTES) is None:
                    # Metadata itself can exceed an administratively reduced inspection budget.
                    exported = exported.model_copy(
                        update={
                            "events": [],
                            "source_map": {},
                            "node_paths": {},
                            "auxiliary_receipts": [],
                            "auxiliary_complete": False,
                            "bounded_prefix": True,
                            "omissions": [*omissions, Omission(path="/*", reason="resource_limit")],
                        }
                    )
            return exported
