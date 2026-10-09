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

"""Terminal-only SQL persistence keeps oversized retained values inside PostgreSQL."""

import json
from functools import partial
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.operations import RecordedEvidence, SignalFact, WaitReceipt
from firefly_weave.contracts.runtime import CapacityRunAcknowledgment
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import execute_pure
from firefly_weave.operations.exports import lock_digest
from firefly_weave.operations.facts import FactRepository, task_fact_update
from firefly_weave.operations.redaction import Omission
from firefly_weave.runtime.admission import terminal_signals
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository


async def oversized_row(repository: RuntimeRepository, identifier: UUID) -> dict[str, Any] | None:
    from firefly_weave.runtime.repository import RUN_FETCH_BYTES

    rows = await repository.rows(
        f"SELECT id,tenant_id,project_id,environment_id,jsonb_build_object("
        "'status',state->'status','accepted_sequence',state->'accepted_sequence') AS state,"
        "CASE WHEN octet_length(artifact::text)<=8388608 THEN artifact ELSE NULL END AS artifact "
        f"FROM runs WHERE {SCOPE} AND id=:id AND public.weave_runs_bytes(runs)>:maximum FOR UPDATE",
        id=identifier,
        maximum=RUN_FETCH_BYTES,
    )
    return rows[0] if rows else None


async def due_terminal(repository: RuntimeRepository, row: dict[str, Any]) -> RuntimeEvent | None:
    signals = await execute_pure(partial(terminal_signals, row), control=True) if row.get("artifact") else None
    due = await repository.rows(
        f"SELECT id,node_id,deadline FROM run_deadlines WHERE {SCOPE} AND run_id=:run "
        "AND NOT consumed AND deadline<=clock_timestamp() "
        "AND (node_id='@run' OR node_id=ANY(cast(:signals AS text[]))) "
        "ORDER BY (node_id='@run') DESC,deadline,node_id LIMIT 1001",
        run=row["id"],
        signals=list(signals or {}),
    )
    for deadline in due:
        name = (signals or {}).get(deadline["node_id"])
        if deadline["node_id"] != "@run" and name is None:
            continue
        if name is not None and await repository.rows(
            f"SELECT 1 FROM signal_receipts WHERE {SCOPE} AND run_id=:run AND name=:name "
            "AND NOT consumed AND accepted_at<:deadline LIMIT 1",
            run=row["id"],
            name=name,
            deadline=deadline["deadline"],
        ):
            continue
        return RuntimeEvent(
            id=uuid4(),
            type="timed_out",
            timestamp=await repository.now(),
            sequence=row["state"]["accepted_sequence"] + 1,
            data={
                "node_id": deadline["node_id"],
                "deadline": deadline["deadline"].isoformat(),
                "wait_id": str(deadline["id"]),
            },
        )
    return None


async def persist_terminal(
    repository: RuntimeRepository, row: dict[str, Any], event: RuntimeEvent, request_hash: str
) -> CapacityRunAcknowledgment:
    from firefly_weave.workers.control import revoke_attempts

    if event.type not in {"cancelled", "timed_out"} or event.sequence != row["state"]["accepted_sequence"] + 1:
        raise ValueError("Terminal sequence required")
    if row["state"]["status"] not in {"queued", "waiting", "suspended"}:
        raise CatalogError(409, "WV-RUNTIME-TERMINAL", "Historical terminal run")
    if repository.outbox is None:
        raise RuntimeError("Outbox required")
    await repository.execute("SELECT weave_control_begin('run',:id)", id=row["id"])
    if event.type == "timed_out":
        accepted = await due_terminal(repository, row)
        if accepted is None or accepted.data != event.data:
            raise CatalogError(409, "WV-RUNTIME-EVENT", "Terminal deadline is not eligible")
        await repository.execute(
            "INSERT INTO wait_wakeups VALUES(:tenant,:project,:environment,:wait,'timeout',:now) "
            "ON CONFLICT DO NOTHING",
            wait=UUID(str(event.data["wait_id"])),
            now=event.timestamp,
        )
    # Existing JSON values stay in SQL; only their terminal control fields change.
    inserted = await repository.rows(
        "WITH updated AS (UPDATE runs SET state=state || jsonb_build_object("
        "'status',cast(:status AS text),'accepted_sequence',cast(:sequence AS bigint),'incident',NULL,"
        "'active','[]'::jsonb,'waits','{}'::jsonb,'incidents','{}'::jsonb,'deferred_results','[]'::jsonb,"
        "'branches',coalesce((SELECT jsonb_object_agg(key,CASE WHEN value->>'status' IN ('pending','running') "
        'THEN value||\'{"status":"cancelled"}\'::jsonb ELSE value END) '
        "FROM jsonb_each(coalesce(state->'branches','{}'))),'{}'),"
        "'joins',coalesce((SELECT jsonb_object_agg(key,CASE WHEN value->>'status'='waiting' "
        'THEN value||\'{"status":"terminated"}\'::jsonb ELSE value END) '
        "FROM jsonb_each(coalesce(state->'joins','{}'))),'{}')) "
        f"WHERE {SCOPE} AND id=:run AND (state->>'accepted_sequence')::bigint=:previous RETURNING *) "
        "INSERT INTO run_events SELECT tenant_id,project_id,environment_id,id,:event,:sequence,:status,"
        "cast(:data AS jsonb),:now,:hash,jsonb_build_object('state',state,'steps','[]'::jsonb,'commands',"
        "CASE WHEN :status='timed_out' AND coalesce(state->'branches','{}')='{}'::jsonb THEN '[]'::jsonb ELSE "
        "jsonb_build_array(jsonb_build_object('kind','revoke_leases','branch',NULL,'node_id',"
        "coalesce(cast(:node AS text),artifact->'executable'->'graph'->>'entry','@run')),"
        "jsonb_build_object('kind','terminate','branch',NULL,'node_id',"
        "coalesce(cast(:node AS text),artifact->'executable'->'graph'->>'entry','@run'))) END),"
        "jsonb_build_object('id',id,'activation',activation,'artifact_digest',artifact->>'digest','state',state,"
        "'correlation_key',request->'correlation_key','business_key',request->'business_key',"
        "'parent_run_id',(SELECT parent_run_id FROM run_retry_links l WHERE l.run_id=updated.id),"
        "'external_effects_may_continue',:status='cancelled') FROM updated RETURNING id",
        run=row["id"],
        status=event.type,
        sequence=event.sequence,
        previous=event.sequence - 1,
        event=event.id,
        data=json.dumps(event.data),
        now=event.timestamp,
        hash=request_hash,
        node=event.data.get("node_id"),
    )
    if len(inserted) != 1:
        raise CatalogError(409, "WV-RUNTIME-SEQUENCE", "Terminal sequence changed")
    from firefly_weave.human_tasks.persistence import close_tasks

    await close_tasks(repository, row["id"], event.type, event.data.get("node_id", "@run"))
    while True:
        tasks = await repository.rows(
            f"SELECT id,run_id,tenant_id,project_id,environment_id,status FROM task_intents WHERE {SCOPE} "
            "AND run_id=:run AND status IN ('ready','leased','retry_pending','incident','failed') "
            "ORDER BY id LIMIT 100 FOR UPDATE",
            run=row["id"],
        )
        if not tasks:
            break
        await revoke_attempts(repository.tx, row["id"], tasks, control_failure=event.type == "cancelled")
        await repository.execute(
            task_fact_update(
                f"UPDATE task_intents SET status='cancelled' WHERE {SCOPE} AND id=ANY(cast(:ids AS uuid[]))"
            ),
            at=event.timestamp,
            ids=[task["id"] for task in tasks],
        )
    await repository.execute(
        f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run",
        run=row["id"],
    )
    await repository.execute(
        f"WITH closed AS (UPDATE incidents SET status='closed',revision=revision+1 WHERE {SCOPE} AND run_id=:run "
        "AND status='active' RETURNING id) UPDATE incident_facts SET updated_at=:at "
        f"WHERE {SCOPE} AND incident_id IN (SELECT id FROM closed)",
        at=event.timestamp,
        run=row["id"],
    )
    if row.get("artifact"):
        try:
            artifact = await execute_pure(partial(import_artifact, row["artifact"]), control=True)
        except ValueError:
            artifact = None
        if artifact is not None:
            evidence = RecordedEvidence(
                run_id=row["id"],
                scope=repository.tx.scope,
                artifact_digest=artifact.digest,
                lock_digest=lock_digest(artifact),
                omissions=[Omission(path="/expected_transition_digest", reason="resource_limit")],
            )
            if event.type == "timed_out":
                signals = await repository.rows(
                    f"SELECT id,name,accepted_at FROM signal_receipts WHERE {SCOPE} AND run_id=:run "
                    "AND NOT consumed AND accepted_at<=:now ORDER BY accepted_at,id LIMIT 1001",
                    run=row["id"],
                    now=event.timestamp,
                )
                wait_receipt = WaitReceipt(
                    wait_id=UUID(str(event.data["wait_id"])),
                    complete=len(signals) <= 1000,
                    pending_signals=[
                        SignalFact(receipt_id=s["id"], name=s["name"], accepted_at=s["accepted_at"])
                        for s in signals[:1000]
                    ],
                )
                evidence = evidence.model_copy(update={"wait_receipt": wait_receipt})
            await repository.execute(
                "INSERT INTO run_event_evidence "
                "VALUES(:tenant,:project,:environment,:run,:event,:sequence,cast(:evidence AS jsonb))",
                run=row["id"],
                event=event.id,
                sequence=event.sequence,
                evidence=evidence.model_dump_json(),
            )
    await FactRepository(repository.tx).project_terminal(
        row["id"], cast(Literal["cancelled", "timed_out"], event.type), event.timestamp, event.sequence
    )
    await repository.outbox.append(
        repository.tx,
        IntegrationEvent(
            event_id=uuid5(NAMESPACE_URL, f"run:{row['id']}:{event.id}"),
            type="run.transition",
            scope=repository.tx.scope,
            resource_id=row["id"],
            correlation_id=row["id"],
            emitted_at=event.timestamp,
            payload=EventMetadata(status=cast(Literal["cancelled", "timed_out"], event.type), sequence=event.sequence),
        ),
    )
    repository.tx.record("transition", "runs.cancel" if event.type == "cancelled" else "deadline")
    return CapacityRunAcknowledgment(
        id=row["id"], status=cast(Literal["cancelled", "timed_out"], event.type), accepted_sequence=event.sequence
    )
