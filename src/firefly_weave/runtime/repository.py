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
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.models import (
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
                        f"SELECT runs.*, (SELECT parent_run_id FROM run_retry_links l WHERE l.run_id=runs.id) "
                        f"AS parent_run_id FROM runs WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else "")
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

    async def insert(self, view: RunView, artifact: dict[str, Any], request: StartRunRequest, principal: UUID) -> None:
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
        await self.project_incidents(view)
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
                await self.execute(
                    "INSERT INTO task_intents(id,tenant_id,project_id,environment_id,run_id,node_id,payloa"
                    "d,worker_release_id,operation_key,status) "
                    "VALUES(:id,:tenant,:project,:environment,:run,:node,"
                    "cast(:payload AS jsonb),:release,:operation,'ready')",
                    id=uuid4(),
                    run=view.id,
                    node=command.node_id,
                    payload=json.dumps(payload),
                    release=release,
                    operation=f"{view.id}:{command.node_id}",
                )
            elif isinstance(command, HumanTaskIntent):
                from firefly_weave.human_tasks.service import create_task

                await create_task(self, view, command, event.timestamp)
            elif isinstance(command, Deadline):
                await self.execute(
                    "INSERT INTO "
                    "run_deadlines(tenant_id,project_id,environment_id,run_id,node_id,deadline,consumed) "
                    "VALUES(:tenant,:project,:environment,:run,:node,:deadline,false)",
                    run=view.id,
                    node=command.node_id,
                    deadline=command.deadline,
                )
        if isinstance(view, RunView):
            await self.record_evidence(view, event, result, task_timing)
        if event.type == "task_completed":
            await self.execute(
                f"UPDATE task_intents SET status='completed' WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data["node_id"],
            )
        if event.type in {"signal_received", "wait_elapsed", "human_completed"}:
            await self.execute(
                f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run AND node_id=:node",
                run=view.id,
                node=event.data["node_id"],
            )
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
                f"UPDATE task_intents SET status='cancelled' WHERE {SCOPE} AND run_id=:run "
                "AND status IN ('ready','leased','retry_pending','incident','failed')",
                run=view.id,
            )
            await self.execute(f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run", run=view.id)

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
        rows = await self.tx.session.execute(
            text(
                f"SELECT id FROM task_intents WHERE {SCOPE} AND worker_release_id=:release "
                "AND status='ready' AND run_id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id "
                "FROM runtime_capacity_blocks WHERE active) "
                "AND (next_attempt_at IS NULL OR "
                "next_attempt_at<=clock_timestamp()) AND ((payload->>'task_type') || '@' || "
                "(payload->>'task_version')) "
                "=ANY(cast(:references AS text[])) ORDER BY id LIMIT :limit"
            ),
            {**self.params, "release": release, "references": task_references, "limit": limit},
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

    async def task_status(self, identifier: UUID, status: str) -> None:
        await self.execute(
            f"UPDATE task_intents SET status=:status WHERE {SCOPE} AND id=:id", id=identifier, status=status
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

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.params, **values})
        return [dict(row) for row in result.mappings()]

    async def scanner_runs(self, limit: int) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded scanner page required")
        return await self.rows(
            f"SELECT id FROM runs WHERE {SCOPE} AND state->>'status' IN ('queued','waiting','suspended') "
            "AND (EXISTS (SELECT 1 FROM run_deadlines d WHERE d.run_id=runs.id AND NOT d.consumed "
            "AND d.deadline<=clock_timestamp()) OR (state->>'status'='waiting' AND EXISTS "
            "(SELECT 1 FROM signal_receipts s WHERE s.run_id=runs.id AND NOT s.consumed AND s.name IN "
            "(SELECT n->>'name' FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(runs.artifact->'executable'->'graph'->'nodes')='array' "
            "THEN runs.artifact->'executable'->'graph'->'nodes' ELSE '[]'::jsonb END) n "
            "WHERE runs.state->'active' ? (n->>'id'))))) "
            "AND (id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id FROM "
            "runtime_capacity_blocks WHERE active) OR EXISTS "
            "(SELECT 1 FROM run_deadlines d WHERE d.run_id=runs.id AND NOT d.consumed "
            "AND d.deadline<=clock_timestamp() AND (d.node_id='@run' OR ((EXISTS "
            "(SELECT 1 FROM run_policy_blocks b WHERE b.run_id=runs.id AND b.terminal_signals_verified) OR EXISTS "
            "(SELECT 1 FROM runtime_capacity_blocks b WHERE b.run_id=runs.id AND b.active)) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN "
            "jsonb_typeof(runs.artifact->'executable'->'graph'->'nodes')='array' "
            "THEN runs.artifact->'executable'->'graph'->'nodes' ELSE '[]'::jsonb END) n "
            "WHERE n->>'kind' IN ('signal','humanTask') AND n->>'id'=d.node_id AND NOT EXISTS "
            "(SELECT 1 FROM signal_receipts s WHERE s.run_id=runs.id AND s.name=n->>'name' "
            "AND s.accepted_at<d.deadline)))))) "
            "ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED",
            limit=limit,
        )

    async def expired_ready(self, limit: int) -> list[UUID]:
        rows = await self.rows(
            f"SELECT id FROM task_intents WHERE {SCOPE} AND status='ready' "
            "AND run_id NOT IN (SELECT run_id FROM run_policy_blocks UNION SELECT run_id FROM "
            "runtime_capacity_blocks WHERE active) "
            f"AND run_id IN (SELECT id FROM runs WHERE {SCOPE} AND state->>'status'='waiting') "
            "AND cast(payload->>'deadline' AS timestamptz)<=clock_timestamp() ORDER BY id LIMIT :limit",
            limit=limit,
        )
        return [row["id"] for row in rows]

    async def project_incidents(self, view: RunView | TerminalControl) -> None:
        """Projection only: never changes or repairs authoritative accepted state."""
        active = {key: value.model_dump() for key, value in view.state.incidents.items()}
        if view.state.status == "suspended" and not active and view.state.incident:
            active["@legacy"] = {"node_id": None, "generation": None, "code": view.state.incident}
        for key, value in active.items():
            await self.execute(
                "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,"
                "node_id,generation,origin_code,code,status,revision) VALUES("
                "md5(cast(cast(:run AS uuid) AS text) || ':' || :key)::uuid,:tenant,:project,:environment,:run,:key,"
                ":node,:generation,:code,:code,'active',1) ON CONFLICT(run_id,incident_key) DO UPDATE "
                "SET code=excluded.code,revision=incidents.revision+1 "
                "WHERE incidents.status='active' AND incidents.code<>excluded.code",
                run=view.id,
                key=key,
                node=value["node_id"],
                generation=value["generation"],
                code=value["code"],
            )
        await self.execute(
            f"UPDATE incidents SET status='closed',revision=revision+1 WHERE {SCOPE} AND run_id=:run "
            "AND status='active' AND NOT (incident_key=ANY(cast(:keys AS text[])))",
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
        if event.type in {"task_completed", "task_failed", "incident_opened", "recovery_scheduled"}:
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
        if event.type in {"wait_elapsed", "timed_out", "signal_received"}:
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
