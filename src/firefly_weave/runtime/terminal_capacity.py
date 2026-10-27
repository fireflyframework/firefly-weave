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
from datetime import datetime
from functools import partial
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.operations import RecordedEvidence, SignalFact, SourceReference, WaitReceipt
from firefly_weave.contracts.runtime import CapacityRunAcknowledgment
from firefly_weave.contracts.values import JsonValue
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
        "AND (node_id='@run' OR substring(node_id from '^[^[#~]+')=ANY(cast(:signals AS text[]))) "
        "ORDER BY (node_id='@run') DESC,deadline,node_id LIMIT 1001",
        run=row["id"],
        signals=list(signals or {}),
    )
    for deadline in due:
        name = (signals or {}).get(node_of(deadline["node_id"]))
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


UNSETTLED_SQL = "('open','deciding','committing','compensating','in_doubt')"


async def hold_transactions(repository: RuntimeRepository, row: dict[str, Any], event: RuntimeEvent) -> bool:
    """A run too large for its own state cannot undo its transactions, and a plain cancel or timeout never abandons
    them. A cancel is refused. A timeout, or a transaction's own deadline, fires once and holds the run, its
    transactions left open, behind an in-doubt incident in the run's own state that only a force end or a force cancel
    closes, so later changes keep it and forward work waits. It is written in SQL, as the oversized terminal path is,
    because the kernel cannot take the run any more. True when the deadline was held this way (or the run already was).
    """
    unsettled = await repository.rows(
        f"SELECT 1 FROM run_transactions WHERE {SCOPE} AND run_id=:run AND state IN {UNSETTLED_SQL} LIMIT 1",
        run=row["id"],
    )
    if not unsettled:
        return False
    from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES
    from firefly_weave.runtime.transactions import HELD_CODE, HELD_INCIDENT, HELD_NODE

    if event.type == "cancelled":
        raise CatalogError(409, "WV-TXN-IN-DOUBT", TRANSACTION_MESSAGES[HELD_CODE])
    wait = UUID(str(event.data["wait_id"]))
    await repository.execute(
        "INSERT INTO wait_wakeups VALUES(:tenant,:project,:environment,:wait,:kind,:now) ON CONFLICT DO NOTHING",
        wait=wait,
        kind="elapsed" if event.type == "wait_elapsed" else "timeout",
        now=event.timestamp,
    )
    await repository.execute(
        f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run AND id=:wait", run=row["id"], wait=wait
    )
    if HELD_INCIDENT in ((row.get("state") or {}).get("incidents") or {}):
        # Already held: a later deadline only fires once, and the hold stays as it is.
        repository.tx.record("transition", "deadline")
        return True
    data: dict[str, JsonValue] = {
        "node_id": HELD_NODE,
        "generation": 0,
        "code": HELD_CODE,
        "origin_code": "WV-RUNTIME-CAPACITY",
    }
    held = RuntimeEvent(
        id=uuid5(NAMESPACE_URL, f"firefly-weave/held/{row['id']}/{wait}"),
        type="incident_opened",
        timestamp=event.timestamp,
        sequence=event.sequence,
        data=data,
    )
    incident = {"node_id": HELD_NODE, "code": HELD_CODE, "origin_code": "WV-RUNTIME-CAPACITY"}
    inserted = await repository.rows(
        "WITH updated AS (UPDATE runs SET state=state || jsonb_build_object('status','suspended',"
        "'accepted_sequence',cast(:sequence AS bigint),'incident',cast(:code AS text),'incidents',"
        "coalesce(state->'incidents','{}'::jsonb) || jsonb_build_object(cast(:key AS text),cast(:incident AS jsonb))) "
        f"WHERE {SCOPE} AND id=:run AND (state->>'accepted_sequence')::bigint=:previous RETURNING *) "
        "INSERT INTO run_events SELECT tenant_id,project_id,environment_id,id,:event,:sequence,'incident_opened',"
        "cast(:data AS jsonb),:now,:hash,jsonb_build_object('state',state,'steps','[]'::jsonb,'commands','[]'::jsonb),"
        "jsonb_build_object('id',id,'activation',activation,'artifact_digest',artifact->>'digest','state',state,"
        "'correlation_key',request->'correlation_key','business_key',request->'business_key',"
        "'parent_run_id',(SELECT parent_run_id FROM run_retry_links l WHERE l.run_id=updated.id),"
        "'external_effects_may_continue',false) FROM updated RETURNING id",
        run=row["id"],
        sequence=held.sequence,
        previous=held.sequence - 1,
        code=HELD_CODE,
        key=HELD_INCIDENT,
        incident=json.dumps(incident),
        event=held.id,
        data=json.dumps(data),
        now=held.timestamp,
        hash=canonical_digest({"held": str(wait)}),
    )
    if len(inserted) != 1:
        raise CatalogError(409, "WV-RUNTIME-SEQUENCE", "Run sequence changed")
    await record_held_evidence(repository, row, held)
    await repository.execute(
        "WITH opened AS (INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,"
        "generation,origin_code,code,status,revision) VALUES(md5(cast(cast(:run AS uuid) AS text) || ':' || :key)"
        "::uuid,:tenant,:project,:environment,:run,:key,:node,0,'WV-RUNTIME-CAPACITY',:code,'active',1) "
        "ON CONFLICT(run_id,incident_key) DO NOTHING RETURNING id) "
        "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,opened_at,"
        "updated_at,workflow_name,workflow_version,test) SELECT :tenant,:project,:environment,o.id,NULL,'',:at,:at,"
        "f.workflow_name,f.workflow_version,f.test FROM opened o "
        f"JOIN run_facts f ON f.run_id=:run AND f.{SCOPE.replace(' AND ', ' AND f.')} "
        "ON CONFLICT(incident_id) DO NOTHING",
        run=row["id"],
        key=HELD_INCIDENT,
        node=HELD_NODE,
        code=HELD_CODE,
        at=held.timestamp,
    )
    await repository.execute(
        "UPDATE run_facts SET status='suspended',updated_at=greatest(updated_at,:at),last_event_sequence=:sequence,"
        f"active_incidents=(SELECT count(*) FROM incidents WHERE {SCOPE} AND run_id=:run AND status='active') "
        f"WHERE {SCOPE} AND run_id=:run AND last_event_sequence<:sequence",
        run=row["id"],
        at=held.timestamp,
        sequence=held.sequence,
    )
    if repository.outbox is not None:
        await repository.outbox.append(
            repository.tx,
            IntegrationEvent(
                event_id=uuid5(NAMESPACE_URL, f"run:{row['id']}:{held.id}"),
                type="run.transition",
                scope=repository.tx.scope,
                resource_id=row["id"],
                correlation_id=row["id"],
                emitted_at=held.timestamp,
                payload=EventMetadata(status="suspended", sequence=held.sequence),
            ),
        )
    repository.tx.record("transition", "deadline")
    return True


async def recorded_source(repository: RuntimeRepository, run_id: UUID, source_hash: str) -> SourceReference | None:
    """The source reference every evidence row of a run carries, found as ``persist`` finds it, so replay sees one
    identity across the run's history."""
    rows = await repository.rows(
        "SELECT s.format,(r.activation->'request'->>'version_id')::uuid AS version FROM runs r "
        "JOIN definition_sources s ON s.tenant_id=r.tenant_id AND s.project_id=r.project_id "
        "AND s.version_id=(r.activation->'request'->>'version_id')::uuid AND s.source_hash=:hash "
        "WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment AND r.id=:run "
        "ORDER BY s.format LIMIT 2",
        run=run_id,
        hash=source_hash,
    )
    if len(rows) != 1:
        return None
    return SourceReference(
        scope=repository.tx.scope, version_id=rows[0]["version"], source_hash=source_hash, format=rows[0]["format"]
    )


async def record_held_evidence(repository: RuntimeRepository, row: dict[str, Any], held: RuntimeEvent) -> None:
    """Evidence for the hold, so a history export names it: the run is too large for the kernel, so the expected
    transition is omitted for its size and replay stops at the hold, as it stops at an oversized stop."""
    if not row.get("artifact"):
        return
    try:
        artifact = await execute_pure(partial(import_artifact, row["artifact"]), control=True)
    except ValueError:
        return
    evidence = RecordedEvidence(
        run_id=row["id"],
        scope=repository.tx.scope,
        artifact_digest=artifact.digest,
        lock_digest=lock_digest(artifact),
        source=await recorded_source(repository, row["id"], artifact.source_hash),
        omissions=[Omission(path="/expected_transition_digest", reason="resource_limit")],
    )
    await repository.execute(
        "INSERT INTO run_event_evidence VALUES(:tenant,:project,:environment,:run,:event,:sequence,"
        "cast(:evidence AS jsonb))",
        run=row["id"],
        event=held.id,
        sequence=held.sequence,
        evidence=evidence.model_dump_json(),
    )


async def force_end_transactions(repository: RuntimeRepository, row: dict[str, Any], at: datetime) -> list[str]:
    """A reconciler ends a run too large for the kernel without undoing anything: its unsettled transactions end
    force-ended, in the record and in the run's own state, in SQL, and each one emits ``transaction.force_ended`` and
    counts as a forced resolution, as the kernel's force end does. Returns their keys."""
    from firefly_weave.runtime.models import TransactionRecord
    from firefly_weave.runtime.settlement import emit

    ended = await repository.rows(
        f"UPDATE run_transactions SET state='force_ended',ended_at=:at,revision=revision+1 WHERE {SCOPE} "
        f"AND run_id=:run AND state IN {UNSETTLED_SQL} RETURNING instance_key,revision",
        run=row["id"],
        at=at,
    )
    keys = [item["instance_key"] for item in ended]
    if not keys:
        return keys
    await repository.execute(
        "UPDATE runs SET state=jsonb_set(state,'{transactions}',(SELECT jsonb_object_agg(key,CASE WHEN "
        "key=ANY(cast(:keys AS text[])) THEN value || jsonb_build_object('status','force_ended','ended_at',"
        "cast(:stamp AS text)) ELSE value END) FROM jsonb_each(state->'transactions'))) "
        f"WHERE {SCOPE} AND id=:run",
        run=row["id"],
        keys=keys,
        stamp=at.isoformat(),
    )
    await repository.execute(
        "UPDATE run_facts SET transaction_state='force_ended' "
        f"WHERE {SCOPE} AND run_id=:run AND transaction_state IS DISTINCT FROM 'force_ended'",
        run=row["id"],
    )
    facts = await repository.rows(
        f"SELECT workflow_name,workflow_version,test FROM run_facts WHERE {SCOPE} AND run_id=:run", run=row["id"]
    )
    records = await repository.rows(
        "SELECT key,value FROM runs,jsonb_each(state->'transactions') "
        f"WHERE {SCOPE} AND id=:run AND key=ANY(cast(:keys AS text[]))",
        run=row["id"],
        keys=keys,
    )
    revisions = {item["instance_key"]: item["revision"] for item in ended}
    for item in records:
        record = TransactionRecord.model_validate_json(json.dumps(item["value"]))
        record = record.model_copy(update={"revision": revisions[item["key"]], "incident": None})
        await emit(
            repository,
            row["id"],
            item["key"],
            record,
            {},
            at,
            facts[0] if facts else {"workflow_name": "", "workflow_version": "", "test": False},
        )
        repository.tx.record("transaction", "transaction.resolve", "cancelled")
    return keys


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
    if await repository.rows(
        f"SELECT 1 FROM run_transactions WHERE {SCOPE} AND run_id=:run AND state IN {UNSETTLED_SQL} LIMIT 1",
        run=row["id"],
    ):
        # Ending here would abandon a transaction without undoing it: callers hold the run first.
        raise RuntimeError("Unsettled transactions require hold_transactions first")
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
    stopped = await repository.stopped_iterations(row["id"])
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
        # Loops end as the reducer ends them; a state without loops keeps leaving the field out.
        "|| CASE WHEN jsonb_typeof(state->'loops')='object' THEN jsonb_build_object('loops',"
        "coalesce((SELECT jsonb_object_agg(key,CASE WHEN value->>'status'='running' "
        'THEN value||\'{"status":"terminated"}\'::jsonb ELSE value END) '
        "FROM jsonb_each(state->'loops')),'{}')) ELSE '{}'::jsonb END "
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
    if stopped:
        repository.tx.record("loop_iteration", "loop", "cancelled", amount=stopped)
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
    from firefly_weave.runtime.calls import caller_ended, child_ended

    await caller_ended(repository, row["id"], event.timestamp)
    await child_ended(repository, row["id"], event.type, None, None, event.timestamp)
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
                source=await recorded_source(repository, row["id"], artifact.source_hash),
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
