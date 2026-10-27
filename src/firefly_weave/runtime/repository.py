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

"""Scoped runtime SQL. Durable intents become visible only after caller commit."""

import json
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.operations import TaskTiming
from firefly_weave.contracts.runtime import RunView, StartRunRequest, UnavailableRunAcknowledgment
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.facts import FactRepository, RunStartFacts, author_instance, task_fact_update
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.models import (
    CallIntent,
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    RuntimeEvent,
    TaskIntent,
    TerminalControl,
    Transition,
)

RUN_FETCH_BYTES = 128 * 1024 * 1024

SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
# A run's retry source and, for a child run, the call that started it.
RUN_LINKS = (
    "(SELECT parent_run_id FROM run_retry_links l WHERE l.run_id=runs.id) AS parent_run_id,"
    "(SELECT jsonb_build_object('run_id',c.caller_run_id,'node_id',c.node_id,'generation',c.generation,"
    "'mode',c.mode,'depth',c.depth) FROM run_call_links c WHERE c.run_id=runs.id) AS call_link"
)


def loop_activity(result: Transition) -> tuple[int, int, int]:
    """Loop iterations a transition collected, loop iterations it cancelled, and loop yields it registered.

    Every iteration running when the run ends counts as cancelled, the one holding a failing step included: the
    failure itself is the run's outcome.
    """
    collected = cancelled = yields = 0
    ended = False
    for command in result.commands:
        if isinstance(command, ControlCommand):
            collected += command.kind == "accept_output" and command.index is not None
            ended = ended or command.kind == "terminate"
        elif isinstance(command, Deadline):
            yields += "~" in command.node_id
    if ended:
        cancelled = sum(b.index is not None and b.status == "cancelled" for b in result.state.branches.values())
    return collected, cancelled, yields


# Iteration frames a stored run state is still running; read before terminal control replaces that state.
STOPPED_ITERATIONS = (
    "SELECT count(*) FROM runs, jsonb_each(CASE WHEN jsonb_typeof(state->'branches')='object' "
    "THEN state->'branches' ELSE '{}'::jsonb END) AS frame "
    f"WHERE {SCOPE} AND id=:run AND jsonb_typeof(frame.value->'index')='number' "
    "AND frame.value->>'status' IN ('pending','running')"
)


def candidates_query() -> str:
    """The claim's candidate tasks of one release: settlement work first, then forward work of runs that take it.

    Settlement work (confirms, cancels, undos and steps a transaction settles) is read through its own partial index
    and runs while its run is paused or held by another incident. Forward work is read only for runs that can take it,
    so paused or held runs never fill a claim's page. Binds the scope, ``runnable_parameters()``, ``release``,
    ``references`` and ``limit``.
    """
    from firefly_weave.runtime.admission import runnable

    common = (
        "worker_release_id=:release AND run_id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id "
        "FROM runtime_capacity_blocks WHERE active) "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=clock_timestamp()) "
        "AND ((payload->>'task_type') || '@' || (payload->>'task_version'))=ANY(cast(:references AS text[]))"
    )
    return (
        f"(SELECT id,0 AS rank FROM task_intents WHERE {SCOPE} AND settlement AND status='ready' AND {common} "
        f"AND (SELECT {runnable('r')} AND r.state->>'status' IN ('waiting','suspended') FROM runs r "
        "WHERE r.id=task_intents.run_id) ORDER BY id LIMIT :limit) UNION ALL "
        f"(SELECT id,1 AS rank FROM task_intents WHERE {SCOPE} AND status='ready' AND NOT settlement "
        f"AND {common} AND (SELECT {runnable('r')} AND r.state->>'status'='waiting' "
        "AND NOT coalesce((r.state->>'manual_paused')::boolean,false) FROM runs r "
        "WHERE r.id=task_intents.run_id) ORDER BY id LIMIT :limit) ORDER BY rank,id LIMIT :limit"
    )


class RuntimeRepository:
    def __init__(self, tx: Transaction, outbox: OutboxService | None = None) -> None:
        self.tx = tx
        self.outbox = outbox
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.params, **values})

    async def now(self) -> datetime:
        value: datetime = await self.tx.session.scalar(text("SELECT clock_timestamp()"))
        return value

    async def _bounded_run(self, identifier: UUID) -> None:
        size = await self.tx.session.scalar(
            text(f"SELECT public.weave_runs_bytes(runs) FROM runs WHERE {SCOPE} AND id=:id"),
            {**self.params, "id": identifier},
        )
        if size is not None and size > RUN_FETCH_BYTES:
            if self.tx.session.info.get("weave_admission"):
                from firefly_weave.runtime.capacity import RuntimeCapacityError

                self.tx.session.info["weave_runtime_candidate"] = identifier
                raise RuntimeCapacityError()
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Retained run exceeds the bounded fetch budget")

    async def run(self, identifier: UUID, *, lock: bool = False) -> dict[str, Any]:
        await self._bounded_run(identifier)
        row = (
            (
                await self.tx.session.execute(
                    text(
                        f"SELECT runs.*, {RUN_LINKS} FROM runs WHERE {SCOPE} AND id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {**self.params, "id": identifier},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
        if lock:
            self.tx.session.info["weave_runtime_candidate"] = identifier
        return dict(row)

    async def locked_run(self, identifier: UUID) -> dict[str, Any] | None:
        """Lock a run unless another transaction holds it; None when it is busy (or does not exist)."""
        await self._bounded_run(identifier)
        row = (
            (
                await self.tx.session.execute(
                    text(f"SELECT runs.*, {RUN_LINKS} FROM runs WHERE {SCOPE} AND id=:id FOR UPDATE SKIP LOCKED"),
                    {**self.params, "id": identifier},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        self.tx.session.info["weave_runtime_candidate"] = identifier
        return dict(row)

    async def require_work(self, identifier: UUID, state: dict[str, Any]) -> None:
        blocked = await self.tx.session.scalar(
            text(f"SELECT active FROM runtime_capacity_blocks WHERE {SCOPE} AND run_id=:id"),
            {**self.params, "id": identifier},
        )
        if blocked:
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Run requires terminal control after a capacity rejection")
        from functools import partial

        from firefly_weave.operations.execution import execute_pure
        from firefly_weave.runtime.capacity import STATE_BYTES, logical_size

        self.tx.session.info["weave_runtime_candidate"] = identifier
        await execute_pure(partial(logical_size, state, STATE_BYTES))

    async def insert(
        self,
        view: RunView,
        artifact: dict[str, Any],
        request: StartRunRequest,
        principal: UUID,
        *,
        start_facts: RunStartFacts,
        at: datetime,
        imported: Any = None,
        ir: Any = None,
    ) -> None:
        await self.execute(
            "INSERT INTO runs VALUES(:id,:tenant,:project,:environment,:activation_id,:principal,"
            "cast(:artifact AS jsonb),cast(:activation AS jsonb),cast(:request AS jsonb),cast(:state AS jsonb))",
            id=view.id,
            activation_id=view.activation.id,
            principal=principal,
            artifact=json.dumps(artifact),
            activation=view.activation.model_dump_json(),
            request=request.model_dump_json(),
            state=view.state.model_dump_json(),
        )

        await FactRepository(self.tx).insert_run(view, artifact, request, start_facts, at, imported=imported, ir=ir)

    async def event(self, run_id: UUID, event_id: UUID) -> dict[str, Any] | None:
        row = (
            (
                await self.tx.session.execute(
                    text(f"SELECT request_hash,response FROM run_events WHERE {SCOPE} AND run_id=:run AND id=:id"),
                    {**self.params, "run": run_id, "id": event_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def persist(
        self,
        view: RunView | TerminalControl,
        event: RuntimeEvent,
        result: Transition,
        request_hash: str,
        *,
        safe_response: UnavailableRunAcknowledgment | None = None,
        task_timing: TaskTiming | None = None,
    ) -> None:
        if self.outbox is None:
            raise RuntimeError("Native outbox append port is required for authoritative persistence")
        if isinstance(view, TerminalControl) and (
            view.scope != self.tx.scope
            or safe_response is None
            or event.type not in {"cancelled", "timed_out"}
            or not result.state.unavailable
            or result.state.status not in {"cancelled", "timed_out"}
            or result.steps
            or any(not isinstance(command, ControlCommand) for command in result.commands)
        ):
            raise ValueError("Unavailable terminal control required")
        from firefly_weave.files.authority import require_run_files

        await require_run_files(self.tx, view.id, result.model_dump(mode="json"))
        # Terminal control of an unavailable run never reads its stored state, so its iterations are counted here.
        stopped = await self.stopped_iterations(view.id) if isinstance(view, TerminalControl) else 0
        await self.execute(
            "INSERT INTO run_events VALUES(:tenant,:project,:environment,:run,:id,:sequence,:type,"
            "cast(:data AS jsonb),:created,:hash,cast(:transition AS jsonb),cast(:response AS jsonb))",
            run=view.id,
            id=event.id,
            sequence=event.sequence,
            type=event.type,
            data=json.dumps(event.data),
            created=event.timestamp,
            hash=request_hash,
            transition=result.model_dump_json(),
            response=(safe_response or view).model_dump_json(),
        )
        await self.execute(
            f"UPDATE runs SET state=cast(:state AS jsonb) WHERE {SCOPE} AND id=:run",
            state=result.state.model_dump_json(),
            run=view.id,
        )
        await self.project_incidents(view, at=event.timestamp)
        for change in result.steps:
            await self.execute(
                "INSERT INTO step_instances "
                "VALUES(:tenant,:project,:environment,:run,:node,:status,cast(:output AS jsonb)) "
                "ON CONFLICT(run_id,node_id) DO UPDATE SET status=excluded.status,output=excluded.output",
                run=view.id,
                node=change.node_id,
                status=change.status,
                output=json.dumps(change.output),
            )
        for command in result.commands:
            if isinstance(command, TaskIntent):
                assert isinstance(view, RunView)
                release = self.release_pin(view.activation, command)
                payload = command.model_dump(mode="json")
                if command.task_type is None:
                    pin = next(
                        p for p in view.activation.connector_execution_pins if p.action_digest == command.action_digest
                    )
                    payload.update(
                        task_type=pin.task_type,
                        task_version=pin.task_version,
                        connector_target=pin.model_dump(mode="json"),
                    )
                identifier = uuid4()
                values = FactRepository(self.tx).task_values(
                    identifier, view.id, command, f"{payload['task_type']}@{payload['task_version']}", event.timestamp
                )
                await self.execute(
                    "WITH inserted AS (INSERT INTO task_intents(id,tenant_id,project_id,environment_id,run_id,node_id,"
                    "payload,worker_release_id,operation_key,status,settlement) "
                    "VALUES(:id,:tenant,:project,:environment,:run,:node,"
                    "cast(:payload AS jsonb),:release,:operation,'ready',:settlement) RETURNING id) "
                    "INSERT INTO task_facts(tenant_id,project_id,environment_id,task_id,run_id,node_id,instance_key,"
                    "task_type,status,created_at,ready_since) SELECT :tenant,:project,:environment,id,:run,"
                    ":fact_node,:instance,:task_type,'ready',:at,:at FROM inserted",
                    **values,
                    node=command.node_id,
                    payload=json.dumps(payload),
                    release=release,
                    operation=f"{view.id}:{command.node_id}",
                    settlement=command.settlement,
                )
            elif isinstance(command, HumanTaskIntent):
                from firefly_weave.human_tasks.service import create_task

                await create_task(self, view, command, event.timestamp)
            elif isinstance(command, Deadline):
                from firefly_weave.runtime.transactions import split_deadline

                # A transaction's deadline keeps one row per key: arming it again moves that row's time, so the row
                # (and the wakeup that names it) stays the same. Any other key is armed once.
                rearm = (
                    " ON CONFLICT(run_id,node_id) DO UPDATE SET deadline=excluded.deadline "
                    "WHERE NOT run_deadlines.consumed"
                    if split_deadline(command.node_id) is not None
                    else ""
                )
                await self.execute(
                    "INSERT INTO "
                    "run_deadlines(tenant_id,project_id,environment_id,run_id,node_id,deadline,consumed) "
                    "VALUES(:tenant,:project,:environment,:run,:node,:deadline,false)" + rearm,
                    run=view.id,
                    node=command.node_id,
                    deadline=command.deadline,
                )
        if isinstance(view, RunView):
            from firefly_weave.runtime.calls import record_calls

            await record_calls(self, view, result, event.timestamp)
            from firefly_weave.runtime.settlement import project_transactions, settle_work

            for command in result.commands:
                if isinstance(command, ControlCommand) and command.kind == "settle":
                    await settle_work(self, view.id, command.node_id, event.timestamp)
            await project_transactions(self, view, result, event)
            await self.record_evidence(view, event, result, task_timing)
        if event.type == "task_completed":
            await self.execute(
                task_fact_update(
                    f"UPDATE task_intents SET status='completed' WHERE {SCOPE} AND run_id=:run AND node_id=:node"
                ),
                at=event.timestamp,
                run=view.id,
                node=event.data["node_id"],
            )
        if event.type == "timed_out" and result.state.status not in {"succeeded", "failed", "cancelled", "timed_out"}:
            # The run's timeout fired, and the run ends once its transactions settle: it fires once.
            await self.execute(
                f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run AND id=:wait",
                run=view.id,
                wait=event.data["wait_id"],
            )
        if event.type in {"signal_received", "wait_elapsed", "human_completed", "step_failed"}:
            await self.execute(
                f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data["node_id"],
            )
        if event.type == "step_failed":
            # A step whose failure its setting or its transaction handled is done with: no revoke, recovery or claim
            # touches it.
            await self.execute(
                task_fact_update(
                    f"UPDATE task_intents SET status='handled',next_attempt_at=NULL WHERE {SCOPE} AND run_id=:run "
                    "AND node_id=:node"
                ),
                at=event.timestamp,
                run=view.id,
                node=event.data["node_id"],
            )
            from firefly_weave.human_tasks.persistence import expire_task

            # A human task nobody completed in time is closed as expired, with its audit row.
            await expire_task(self, view.id, event.data["node_id"])
        from firefly_weave.runtime.step_failures import first_failures

        for _, handling, _ in first_failures(result):
            self.tx.record("step_error", f"step.{handling}", "failed")
        if result.state.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            from firefly_weave.human_tasks.persistence import close_tasks

            await close_tasks(self, view.id, result.state.status, event.data.get("node_id", "@run"))
            from firefly_weave.workers.control import revoke_attempts

            tasks = await self.rows(
                f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run "
                "AND status IN ('ready','leased','retry_pending','incident','failed') ORDER BY id FOR UPDATE",
                run=view.id,
            )
            await revoke_attempts(
                self.tx,
                view.id,
                tasks,
                control_failure=result.state.status in {"failed", "cancelled"}
                and any(
                    isinstance(command, ControlCommand) and command.kind == "revoke_leases"
                    for command in result.commands
                ),
            )
            await self.execute(
                task_fact_update(
                    f"UPDATE task_intents SET status='cancelled' WHERE {SCOPE} AND run_id=:run "
                    "AND status IN ('ready','leased','retry_pending','incident','failed')"
                ),
                at=event.timestamp,
                run=view.id,
            )
            await self.execute(f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run", run=view.id)
            from firefly_weave.runtime.calls import caller_ended, child_ended

            await caller_ended(self, view.id, event.timestamp)
            await child_ended(
                self, view.id, result.state.status, result.state.output, result.state.incident, event.timestamp
            )

        await FactRepository(self.tx).project_run(view, event, result)
        await self.outbox.append(
            self.tx,
            IntegrationEvent(
                event_id=uuid5(NAMESPACE_URL, f"run:{view.id}:{event.id}"),
                type="run.transition",
                scope=self.tx.scope,
                resource_id=view.id,
                correlation_id=view.id,
                emitted_at=event.timestamp,
                payload=EventMetadata(status=result.state.status, sequence=event.sequence),
            ),
        )

        self.tx.record(
            "transition",
            "runs.cancel"
            if event.type == "cancelled"
            else "recovery"
            if event.type in {"recovery_scheduled", "incident_opened"}
            else "deadline"
            if event.type in {"timed_out", "wait_elapsed"}
            else "other",
        )
        collected, cancelled, yields = loop_activity(result)
        for kind, status, amount in (
            ("loop_iteration", "ok", collected),
            ("loop_iteration", "cancelled", cancelled + stopped),
            ("loop_yield", "ok", yields),
        ):
            if amount:
                self.tx.record(kind, "loop", status, amount=amount)

    @staticmethod
    def release_pin(activation: Activation, command: TaskIntent) -> UUID | None:
        if command.task_type is None:
            pins = [p for p in activation.connector_execution_pins if p.action_digest == command.action_digest]
            if len(pins) != 1:
                raise CatalogError(422, "WV-READINESS", "Missing pinned connector execution target")
            return pins[0].release_id
        release = activation.request.worker_release_ids.get(command.task_type)
        if release is None:
            raise CatalogError(422, "WV-READINESS", "Task requires its exact admitted worker release")
        return release

    async def ready(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded scanner page required")
        rows = await self.tx.session.execute(
            text(
                f"SELECT * FROM task_intents WHERE {SCOPE} AND status='ready' "
                "AND run_id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id FROM "
                "runtime_capacity_blocks WHERE active) ORDER BY id LIMIT :limit"
            ),
            {**self.params, "limit": limit},
        )
        return [dict(row) for row in rows.mappings()]

    async def candidates(self, release: UUID, task_references: list[str], *, limit: int = 100) -> list[UUID]:
        from firefly_weave.runtime.admission import runnable_parameters

        rows = await self.tx.session.execute(
            text(candidates_query()),
            {
                **self.params,
                **runnable_parameters(),
                "release": release,
                "references": task_references,
                "limit": limit,
            },
        )
        return list(rows.scalars())

    async def locked_task(
        self, identifier: UUID, *, skip_locked: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        params = {**self.params, "id": identifier}
        run_id = await self.tx.session.scalar(text(f"SELECT run_id FROM task_intents WHERE {SCOPE} AND id=:id"), params)
        if run_id is None:
            raise CatalogError(404, "WV-NOT-FOUND", "Task not found")
        await self._bounded_run(run_id)
        suffix = " FOR UPDATE" + (" SKIP LOCKED" if skip_locked else "")
        run = (
            (
                await self.tx.session.execute(
                    text(f"SELECT * FROM runs WHERE {SCOPE} AND id=:run" + suffix), {**params, "run": run_id}
                )
            )
            .mappings()
            .first()
        )
        if run is None:
            return None
        task = (
            (
                await self.tx.session.execute(
                    text(f"SELECT * FROM task_intents WHERE {SCOPE} AND id=:id" + suffix), params
                )
            )
            .mappings()
            .first()
        )
        return (dict(run), dict(task)) if task else None

    async def task_status(self, identifier: UUID, status: str, *, at: datetime | None = None) -> None:
        await self.execute(
            task_fact_update(f"UPDATE task_intents SET status=:status WHERE {SCOPE} AND id=:id"),
            id=identifier,
            status=status,
            at=at if at is not None else await self.now(),
        )

    async def task_deadline(self, run_id: UUID, task_deadline: datetime) -> datetime:
        deadline = await self.tx.session.scalar(
            text(
                f"SELECT min(deadline) FROM run_deadlines WHERE {SCOPE} AND run_id=:run AND "
                f"node_id='@run' AND NOT consumed"
            ),
            {**self.params, "run": run_id},
        )
        return min(task_deadline, deadline) if deadline else task_deadline

    async def stopped_iterations(self, run_id: UUID) -> int:
        """Loop iterations the stored state of a run is running, without loading that state."""
        result = await self.tx.session.execute(text(STOPPED_ITERATIONS), {**self.params, "run": run_id})
        return int(result.scalar() or 0)

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.params, **values})
        return [dict(row) for row in result.mappings()]

    async def scanner_runs(self, limit: int) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded scanner page required")
        from firefly_weave.runtime.admission import runnable, runnable_parameters

        return await self.rows(
            f"SELECT id FROM runs WHERE {SCOPE} AND state->>'status' IN ('queued','waiting','suspended') "
            # CASE reads the artifact only for a due run: the planner cannot evaluate THEN first.
            "AND CASE WHEN (EXISTS (SELECT 1 FROM run_deadlines d WHERE d.run_id=runs.id AND NOT d.consumed "
            "AND d.deadline<=clock_timestamp()) OR (state->>'status'='waiting' AND EXISTS "
            "(SELECT 1 FROM signal_receipts s WHERE s.run_id=runs.id AND NOT s.consumed AND s.name IN "
            "(SELECT n->>'name' FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(runs.artifact->'executable'->'graph'->'nodes')='array' "
            "THEN runs.artifact->'executable'->'graph'->'nodes' ELSE '[]'::jsonb END) n "
            "WHERE runs.state->'active' ? (n->>'id')))) "
            # A waiting call whose child ended is delivered under this run's lock (index run_calls_undelivered).
            "OR EXISTS (SELECT 1 FROM run_calls c WHERE c.caller_run_id=runs.id AND c.mode='wait' "
            "AND NOT c.delivered AND c.status IN ('succeeded','failed','cancelled','timed_out')) "
            # A participant task revoked before any worker claimed it is reported to its transaction
            # (index task_intents_revoked).
            "OR EXISTS (SELECT 1 FROM task_intents t WHERE t.run_id=runs.id AND t.status='revoked')) "
            "AND (id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id FROM "
            "runtime_capacity_blocks WHERE active) OR EXISTS "
            "(SELECT 1 FROM run_deadlines d WHERE d.run_id=runs.id AND NOT d.consumed "
            "AND d.deadline<=clock_timestamp() AND (d.node_id='@run' OR ((EXISTS "
            "(SELECT 1 FROM run_policy_blocks b WHERE b.run_id=runs.id AND b.terminal_signals_verified) OR EXISTS "
            "(SELECT 1 FROM runtime_capacity_blocks b WHERE b.run_id=runs.id AND b.active)) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(runs.artifact->'executable'->'graph'->'nodes')='array' "
            "THEN runs.artifact->'executable'->'graph'->'nodes' ELSE '[]'::jsonb END) n "
            "WHERE n->>'kind' IN ('signal','humanTask') AND NOT n ? 'onError' "
            "AND n->>'id'=substring(d.node_id from '^[^[#~]+') "
            "AND NOT EXISTS "
            "(SELECT 1 FROM signal_receipts s WHERE s.run_id=runs.id AND s.name=n->>'name' "
            "AND s.accepted_at<d.deadline)))))) "
            f"THEN {runnable('runs')} ELSE false END "
            "ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED",
            limit=limit,
            **runnable_parameters(),
        )

    async def expired_ready(self, limit: int) -> list[UUID]:
        from firefly_weave.runtime.admission import runnable, runnable_parameters

        rows = await self.rows(
            f"SELECT id FROM task_intents WHERE {SCOPE} AND status='ready' "
            "AND run_id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id FROM "
            "runtime_capacity_blocks WHERE active) "
            # Settlement work also expires while its run is held by another incident.
            f"AND run_id IN (SELECT id FROM runs WHERE {SCOPE} AND (state->>'status'='waiting' "
            "OR (task_intents.settlement AND state->>'status'='suspended'))) "
            "AND cast(payload->>'deadline' AS timestamptz)<=clock_timestamp() "
            f"AND (SELECT {runnable('r')} FROM runs r WHERE r.id=task_intents.run_id) ORDER BY id LIMIT :limit",
            limit=limit,
            **runnable_parameters(),
        )
        return [row["id"] for row in rows]

    async def project_incidents(self, view: RunView | TerminalControl, *, at: datetime | None = None) -> None:
        """Projection only: never changes or repairs authoritative accepted state."""
        at = at if at is not None else await self.now()
        active = {key: value.model_dump() for key, value in view.state.incidents.items()}
        if view.state.status == "suspended" and not active and view.state.incident:
            active["@legacy"] = {"node_id": None, "generation": None, "code": view.state.incident}
        for key, value in active.items():
            identity = author_instance(value["node_id"]) if value["node_id"] is not None else None
            await self.execute(
                "WITH changed AS (INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,"
                "node_id,generation,origin_code,code,status,revision) VALUES("
                "md5(cast(cast(:run AS uuid) AS text) || ':' || :key)::uuid,:tenant,:project,:environment,:run,:key,"
                ":node,:generation,coalesce(:origin,:code),:code,'active',1) "
                "ON CONFLICT(run_id,incident_key) DO UPDATE "
                # A transaction in doubt keeps the failure that put it there.
                "SET code=excluded.code,origin_code=CASE WHEN :origin IS NULL THEN incidents.origin_code "
                "ELSE excluded.origin_code END,revision=incidents.revision+1 "
                "WHERE incidents.status='active' AND incidents.code<>excluded.code RETURNING id) "
                "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,"
                "opened_at,updated_at,workflow_name,workflow_version,test) SELECT :tenant,:project,:environment,"
                "c.id,:fact_node,:instance,:at,:at,f.workflow_name,f.workflow_version,f.test FROM changed c "
                f"JOIN run_facts f ON f.run_id=:run AND f.{SCOPE.replace(' AND ', ' AND f.')} "
                "ON CONFLICT(incident_id) DO UPDATE SET updated_at=excluded.updated_at",
                fact_node=identity.node_id if identity else None,
                instance=identity.instance_key if identity else "",
                at=at,
                run=view.id,
                key=key,
                node=value["node_id"],
                generation=value["generation"],
                code=value["code"],
                origin=value.get("origin_code"),
            )
        await self.execute(
            f"WITH closed AS (UPDATE incidents SET status='closed',revision=revision+1 WHERE {SCOPE} AND run_id=:run "
            "AND status='active' AND NOT (incident_key=ANY(cast(:keys AS text[]))) RETURNING id) "
            f"UPDATE incident_facts SET updated_at=:at WHERE {SCOPE} AND incident_id IN (SELECT id FROM closed)",
            at=at,
            run=view.id,
            keys=list(active),
        )

    async def observe_policy_block(self, row: dict[str, Any]) -> None:
        """Trusted scanner/claim observation; caller holds this scoped run lock."""
        if any(row[field] != getattr(self.tx.scope, field) for field in ("tenant_id", "project_id", "environment_id")):
            raise ValueError("Policy observation scope mismatch")
        from firefly_weave.runtime.admission import terminal_signals

        await self.execute(
            "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,"
            "reason_code,terminal_signals_verified) "
            "VALUES(:tenant,:project,:environment,:run,'WV-LEGACY-UNAVAILABLE',:verified) ON CONFLICT DO NOTHING",
            run=row["id"],
            verified=terminal_signals(row) is not None,
        )

        from firefly_weave.workers.control import block_attempts

        tasks = await self.rows(
            f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run ORDER BY id FOR UPDATE", run=row["id"]
        )
        await block_attempts(self.tx, row["id"], tasks)

    async def record_evidence(
        self, view: RunView, event: RuntimeEvent, result: Transition, task_timing: TaskTiming | None
    ) -> None:
        """Bind optional safe facts in the accepted-event transaction, never amend old rows."""
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.contracts.operations import (
            CallFact,
            CallReceipt,
            DeadlineFact,
            HumanDecisionReceipt,
            HumanTaskFact,
            RecordedEvidence,
            SignalFact,
            SourceReference,
            TaskFact,
            TaskReceipt,
            WaitReceipt,
        )
        from firefly_weave.operations.exports import lock_digest, transition_digest
        from firefly_weave.operations.redaction import project
        from firefly_weave.runtime.admission import unavailable
        from firefly_weave.runtime.kernel import workflow

        row = await self.run(view.id)
        if unavailable(row):
            return
        artifact = import_artifact(row["artifact"])
        ir = workflow(artifact)
        sources = await self.rows(
            "SELECT format FROM definition_sources WHERE tenant_id=:tenant AND project_id=:project "
            "AND version_id=:version AND source_hash=:hash ORDER BY format LIMIT 2",
            version=view.activation.request.version_id,
            hash=artifact.source_hash,
        )
        source = (
            SourceReference(
                scope=self.tx.scope,
                version_id=view.activation.request.version_id,
                source_hash=artifact.source_hash,
                format=sources[0]["format"],
            )
            if len(sources) == 1
            else None
        )
        evidence = RecordedEvidence(
            run_id=view.id,
            scope=self.tx.scope,
            artifact_digest=artifact.digest,
            lock_digest=lock_digest(artifact),
            source=source,
            expected_transition_digest=transition_digest(result),
            initial_input=project(
                row["request"]["input"],
                ir.schemas[ir.input_schema],
                {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"},
            )
            if event.type == "started"
            else None,
        )

        def task_fact(task: dict[str, Any]) -> TaskFact:
            return TaskFact(
                task_id=task["id"],
                node_id=task["node_id"],
                action_digest=task["payload"]["action_digest"],
                release_id=task["worker_release_id"],
                deadline=datetime.fromisoformat(task["payload"]["deadline"]),
            )

        for command in result.commands:
            if isinstance(command, TaskIntent):
                tasks = await self.rows(
                    f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                    run=view.id,
                    node=command.node_id,
                )
                evidence.issued_tasks.append(task_fact(tasks[0]))
            elif isinstance(command, HumanTaskIntent):
                human_rows = await self.rows(
                    f"SELECT id,assignment FROM human_tasks WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                    run=view.id,
                    node=command.node_id,
                )
                from firefly_weave.compiler.canonical import canonical_digest

                evidence.issued_human_tasks.append(
                    HumanTaskFact(
                        task_id=human_rows[0]["id"],
                        node_id=command.node_id,
                        assignment_digest=canonical_digest(human_rows[0]["assignment"]),
                    )
                )
            elif isinstance(command, Deadline):
                due = await self.rows(
                    f"SELECT id FROM run_deadlines WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                    run=view.id,
                    node=command.node_id,
                )
                evidence.issued_deadlines.append(
                    DeadlineFact(
                        wait_id=due[0]["id"], node_id=command.node_id, deadline=command.deadline, name=command.name
                    )
                )

        async def call_fact(node_id: object, generation: int | None = None) -> tuple[CallFact, dict[str, Any]] | None:
            rows = await self.rows(
                f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND node_id=:node "
                "AND (cast(:generation AS integer) IS NULL OR generation=:generation) ORDER BY generation DESC LIMIT 1",
                run=view.id,
                node=node_id,
                generation=generation,
            )
            if not rows:
                return None
            row = rows[0]
            fact = CallFact(
                call_id=row["id"],
                node_id=row["node_id"],
                generation=row["generation"],
                mode=row["mode"],
                workflow_reference=row["workflow_reference"],
            )
            return fact, row

        for command in result.commands:
            if isinstance(command, CallIntent):
                issued = await call_fact(command.node_id, 0)
                assert issued is not None
                evidence.issued_calls.append(issued[0])
        if (
            event.type == "incident_resolved"
            and event.data.get("kind") == "retry_safe"
            and (retried := await call_fact(event.data.get("node_id"))) is not None
            and retried[1]["status"] == "requested"
        ):
            evidence.issued_calls.append(retried[0])
        generation = event.data.get("generation")
        if event.type in {"call_completed", "incident_opened", "step_failed"} and isinstance(generation, int):
            received = await call_fact(event.data.get("node_id"), generation)
            ended = {"succeeded", "failed", "cancelled", "timed_out", "rejected"}
            if received is not None and received[1]["status"] in ended:
                fact, row = received
                evidence = evidence.model_copy(
                    update={
                        "call_receipt": CallReceipt(
                            call=fact, child_run_id=row["child_run_id"], status=row["status"], code=row["code"]
                        )
                    }
                )
        if event.type == "human_completed":
            human_rows = await self.rows(
                f"SELECT id,assignment FROM human_tasks WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data.get("node_id"),
            )
            from firefly_weave.compiler.canonical import canonical_digest

            evidence = evidence.model_copy(
                update={
                    "human_receipt": HumanDecisionReceipt(
                        task=HumanTaskFact(
                            task_id=human_rows[0]["id"],
                            node_id=str(event.data["node_id"]),
                            assignment_digest=canonical_digest(human_rows[0]["assignment"]),
                        ),
                        actor_id=UUID(str(event.data["actor_id"])),
                        accepted_at=event.timestamp,
                    )
                }
            )
        if event.type in {"task_completed", "task_failed", "incident_opened", "recovery_scheduled", "step_failed"}:
            tasks = await self.rows(
                f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data.get("node_id"),
            )
            if tasks:
                attempts = await self.rows(
                    f"SELECT generation FROM task_leases WHERE {SCOPE} AND task_id=:task AND generation=:generation",
                    task=tasks[0]["id"],
                    generation=event.data.get("generation", 0),
                )
                if attempts:
                    evidence = evidence.model_copy(
                        update={
                            "task_receipt": TaskReceipt(
                                task=task_fact(tasks[0]),
                                receipt_id=event.id,
                                generation=attempts[0]["generation"],
                                timing=task_timing,
                            )
                        }
                    )
        if event.type in {"wait_elapsed", "timed_out", "signal_received"} or (
            event.type == "step_failed" and "wait_id" in event.data
        ):
            due = await self.rows(
                f"SELECT id FROM run_deadlines WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data.get("node_id"),
            )
            signals = await self.rows(
                f"SELECT id,name,accepted_at FROM signal_receipts WHERE {SCOPE} "
                "AND run_id=:run AND NOT consumed AND accepted_at<=:now "
                "ORDER BY accepted_at,id LIMIT 1001",
                run=view.id,
                now=event.timestamp,
            )
            if due:
                evidence = evidence.model_copy(
                    update={
                        "wait_receipt": WaitReceipt(
                            wait_id=due[0]["id"],
                            complete=len(signals) <= 1000,
                            pending_signals=[
                                SignalFact(receipt_id=s["id"], name=s["name"], accepted_at=s["accepted_at"])
                                for s in signals[:1000]
                            ],
                        )
                    }
                )
        await self.execute(
            "INSERT INTO run_event_evidence VALUES(:tenant,:project,:environment,:run,:id,:sequence,"
            "cast(:evidence AS jsonb))",
            run=view.id,
            id=event.id,
            sequence=event.sequence,
            evidence=evidence.model_dump_json(),
        )
