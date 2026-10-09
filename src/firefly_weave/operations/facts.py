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

"""Transaction-bound SQL for operational projections."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.definitions import ResourceName
from firefly_weave.contracts.instance_keys import InstanceKeyText, InstanceView, instance_view
from firefly_weave.contracts.run_views import RunOrigin, RunSummaryCaller, StepKind
from firefly_weave.contracts.runtime import RunView, StartRunRequest
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.models import (
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    RunState,
    RuntimeEvent,
    TaskIntent,
    TerminalControl,
    Transition,
)

FACT_TABLES: tuple[str, str, str, str, str] = (
    "run_facts",
    "step_facts",
    "task_facts",
    "incident_facts",
    "worker_facts",
)


SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


def author_instance(key: str) -> InstanceView | None:
    if type(key) is not str:
        raise ValueError("Instance key must be text")
    if key.startswith("@"):
        return None
    TypeAdapter(InstanceKeyText).validate_python(key, strict=True)
    value = instance_view(key)
    TypeAdapter(ResourceName).validate_python(value.node_id, strict=True)
    return value


def listing_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Bounded cache metadata; execution admission remains the runtime's authority."""
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime import admission as retained
    from firefly_weave.runtime.kernel import workflow

    result: dict[str, Any] = dict(
        classification_state="unavailable",
        classification_policy=retained.POLICY,
        pinned_ir_version=None,
        pinned_features=[],
        node_kinds={},
    )
    try:
        RunState.model_validate_json(json.dumps(row["state"]))
        Activation.model_validate_json(json.dumps(row["activation"]))
        StartRunRequest.model_validate_json(json.dumps(row["request"]))
        decision = retained.admission(row)
        executable = row["artifact"].get("executable", {})
        version, features = executable.get("irVersion"), executable.get("features", [])
        if not isinstance(version, str) or len(version) > 256 or not isinstance(features, list):
            return result
        if len(features) > 256 or any(type(value) is not str for value in features):
            return result
        if len(json.dumps(features, ensure_ascii=False).encode()) > 16384:
            return result
        result.update(classification_state=decision, pinned_ir_version=version, pinned_features=features)
        if decision == "available":
            kinds: dict[str, StepKind] = {}
            for node in workflow(import_artifact(row["artifact"])).graph.nodes:
                identity = author_instance(node.id)
                if identity is not None:
                    kinds[identity.node_id] = TypeAdapter(StepKind).validate_python(node.kind, strict=True)
            if len(kinds) > 10000 or len(json.dumps(kinds, ensure_ascii=False).encode()) > 1048576:
                raise ValueError("Node-kind projection exceeds its bound")
            result["node_kinds"] = kinds
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result.update(classification_state="unavailable", node_kinds={})
    state = row.get("state")
    status = state.get("status") if isinstance(state, dict) else None
    if type(status) is not str or status not in {"queued", "running", "waiting", "suspended", *TERMINAL}:
        result.update(classification_state="unavailable", node_kinds={})
    return result


def task_fact_update(source: str) -> str:
    """Enlist task metadata in the existing authoritative source statement."""
    return (
        "WITH changed AS (" + source + " AND status<>'handled' RETURNING id,status,next_attempt_at) "
        "UPDATE task_facts f SET status=c.status,ready_since=CASE WHEN c.status='ready' "
        "THEN greatest(:at,coalesce(c.next_attempt_at,:at)) ELSE NULL END,"
        "first_claimed_at=CASE WHEN c.status='leased' THEN coalesce(f.first_claimed_at,:at) "
        "ELSE f.first_claimed_at END FROM changed c WHERE f.task_id=c.id "
        "AND f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment"
    )


@dataclass(frozen=True)
class RunStartFacts:
    origin: RunOrigin = "manual"
    test: bool = False
    caller: RunSummaryCaller | None = None
    retried_from_run_id: UUID | None = None

    def __post_init__(self) -> None:
        TypeAdapter(RunOrigin).validate_python(self.origin, strict=True)
        if type(self.test) is not bool or self.test != (self.origin == "test"):
            raise ValueError("Test origin and test flag must agree")
        if (self.origin == "call") != (self.caller is not None):
            raise ValueError("Call origin requires a caller")
        if self.caller is not None:
            if not isinstance(self.caller, RunSummaryCaller):
                raise ValueError("Call origin requires typed caller metadata")
            RunSummaryCaller.model_validate_json(self.caller.model_dump_json())
        if (self.origin == "retry") != (self.retried_from_run_id is not None):
            raise ValueError("Retry origin requires a source run")
        if self.retried_from_run_id is not None and not isinstance(self.retried_from_run_id, UUID):
            raise ValueError("Retry source must be a UUID")


class FactRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**values, **self.params})

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**values, **self.params})
        return [dict(row) for row in result.mappings()]

    async def insert_run(
        self, view: RunView, artifact: dict[str, Any], request: StartRunRequest, facts: RunStartFacts, at: datetime
    ) -> None:
        metadata = listing_metadata(
            dict(
                state=view.state.model_dump(mode="json"),
                artifact=artifact,
                activation=view.activation.model_dump(mode="json"),
                request=request.model_dump(mode="json"),
            )
        )
        await self.execute(
            "INSERT INTO run_facts(tenant_id,project_id,environment_id,run_id,definition_version_id,"
            "workflow_name,workflow_version,activation_id,activation_name,activation_revision,status,paused,"
            "test,origin,caller_run_id,caller_node_id,caller_instance_key,retried_from_run_id,business_key,"
            "correlation_key,started_at,updated_at,classification_state,classification_policy,pinned_ir_version,"
            "pinned_features,node_kinds,facts_started_at) SELECT :tenant,:project,:environment,:run,:version,"
            "name,version,:activation,:name,:revision,:status,:paused,:test,:origin,:caller_run,:caller_node,"
            ":caller_instance,:retried,:business,:correlation,:at,:at,:classification_state,:classification_policy,"
            ":pinned_ir_version,cast(:pinned_features AS jsonb),cast(:node_kinds AS jsonb),:at "
            "FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project AND id=:version",
            run=view.id,
            version=view.activation.request.version_id,
            activation=view.activation.id,
            name=view.activation.name,
            revision=view.activation.revision,
            status=view.state.status,
            paused=view.state.manual_paused,
            test=facts.test,
            origin=facts.origin,
            caller_run=facts.caller.run_id if facts.caller else None,
            caller_node=facts.caller.node_id if facts.caller else None,
            caller_instance=facts.caller.instance_key if facts.caller else None,
            retried=facts.retried_from_run_id,
            business=request.business_key,
            correlation=request.correlation_key,
            at=at,
            **{
                **metadata,
                "pinned_features": json.dumps(metadata["pinned_features"], ensure_ascii=False),
                "node_kinds": json.dumps(metadata["node_kinds"], ensure_ascii=False),
            },
        )

    def task_values(
        self, task_id: UUID, run_id: UUID, command: TaskIntent, task_type: str, at: datetime
    ) -> dict[str, Any]:
        identity = author_instance(command.node_id)
        if identity is None:
            raise ValueError("Task requires an author step")
        return dict(
            id=task_id,
            run=run_id,
            fact_node=identity.node_id,
            instance=identity.instance_key,
            task_type=task_type,
            at=at,
        )

    async def project_run(self, view: RunView | TerminalControl, event: RuntimeEvent, result: Transition) -> None:
        failed = None
        if result.state.status == "failed":
            key = next(
                (c.node_id for c in result.commands if isinstance(c, ControlCommand) and c.kind == "terminate"),
                event.data.get("node_id"),
            )
            failed = author_instance(key) if isinstance(key, str) else None
        rows = await self.rows(
            f"UPDATE run_facts SET status=:status,paused=:paused,updated_at=greatest(updated_at,:at),"
            "ended_at=CASE WHEN :terminal THEN :at ELSE ended_at END,failed_node_id=:failed_node,"
            "failed_instance_key=:failed_instance,failed_error_code=:code,last_event_sequence=:sequence,"
            "classification_state=CASE WHEN :unavailable THEN 'unavailable' ELSE classification_state END,"
            f"active_incidents=(SELECT count(*) FROM incidents WHERE {SCOPE} AND run_id=:run AND status='active') "
            f"WHERE {SCOPE} AND run_id=:run AND last_event_sequence<:sequence RETURNING node_kinds",
            run=view.id,
            status=result.state.status,
            paused=result.state.manual_paused,
            at=event.timestamp,
            terminal=result.state.status in TERMINAL,
            failed_node=failed.node_id if failed else None,
            failed_instance=failed.instance_key if failed else None,
            code=result.state.incident if result.state.status == "failed" else None,
            sequence=event.sequence,
            unavailable=result.state.unavailable,
        )
        if not rows:
            return
        kinds = rows[0]["node_kinds"]
        steps: dict[tuple[str, str], dict[str, Any]] = {}

        def add(key: str, status: str, *, error: str | None = None) -> None:
            identity = author_instance(key)
            if identity is None:
                return
            kind: StepKind = TypeAdapter(StepKind).validate_python(kinds[identity.node_id], strict=True)
            steps[identity.node_id, identity.instance_key] = dict(
                node_id=identity.node_id,
                instance_key=identity.instance_key,
                kind=kind,
                status=status,
                started_at=None if kind in {"action", "llm", "agent"} else event.timestamp.isoformat(),
                ended_at=event.timestamp.isoformat() if status in TERMINAL else None,
                error_code=error,
            )

        if not isinstance(view, TerminalControl):
            for command in result.commands:
                if isinstance(command, (TaskIntent, Deadline, HumanTaskIntent)):
                    add(command.node_id, "scheduled" if isinstance(command, TaskIntent) else "waiting")
                elif command.kind == "spawn_branch":
                    add(command.node_id, "running")
            for change in result.steps:
                identity = author_instance(change.node_id)
                if identity is None:
                    continue
                kind = kinds[identity.node_id]
                status = (
                    "succeeded"
                    if change.status == "completed"
                    else ("scheduled" if kind in {"action", "llm", "agent"} else "waiting")
                )
                add(change.node_id, status)
            for owner, join in result.state.joins.items():
                if join.status == "waiting" and (owner, "") not in steps:
                    add(owner, "running")
            node = event.data.get("node_id")
            if isinstance(node, str) and any(value.id == event.id for value in result.state.deferred_results):
                add(node, "succeeded")
            if isinstance(node, str) and event.type in {"task_failed", "incident_opened", "recovery_scheduled"}:
                output = event.data.get("output")
                code = output.get("code") if isinstance(output, dict) else event.data.get("code")
                add(
                    node,
                    "scheduled" if event.type == "recovery_scheduled" else "failed",
                    error=code if isinstance(code, str) else None,
                )
            if (
                isinstance(node, str)
                and event.type == "incident_resolved"
                and event.data.get("kind") == "retry_safe"
                and result.state.status not in TERMINAL
            ):
                add(node, "scheduled")
            if failed:
                add(failed.instance_key or failed.node_id, "failed", error=result.state.incident)
        if not steps and result.state.status not in TERMINAL:
            return
        closed = result.state.status if result.state.status in {"cancelled", "timed_out"} else "cancelled"
        if result.state.status in TERMINAL:
            for step in steps.values():
                if step["status"] in {"scheduled", "running", "waiting"}:
                    step.update(status=closed, ended_at=event.timestamp.isoformat())
        await self.execute(
            "WITH projected AS (INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,"
            "instance_key,kind,status,scheduled_at,started_at,ended_at,error_code) "
            "SELECT :tenant,:project,:environment,:run,s.node_id,s.instance_key,s.kind,s.status,:at,"
            "s.started_at,s.ended_at,s.error_code FROM jsonb_to_recordset(cast(:steps AS jsonb)) "
            "AS s(node_id text,instance_key text,kind text,status text,started_at timestamptz,"
            "ended_at timestamptz,error_code text) ON CONFLICT(run_id,node_id,instance_key) DO UPDATE SET "
            "status=excluded.status,started_at=coalesce(step_facts.started_at,excluded.started_at),"
            "ended_at=CASE WHEN step_facts.status=excluded.status THEN coalesce(step_facts.ended_at,excluded.ended_at) "
            "ELSE excluded.ended_at END,error_code=excluded.error_code WHERE step_facts.handled IS NULL "
            "RETURNING node_id,instance_key) UPDATE step_facts SET status=:closed,ended_at=:at "
            f"WHERE {SCOPE} AND run_id=:run AND :terminal AND status IN ('scheduled','running','waiting') "
            "AND NOT EXISTS(SELECT 1 FROM projected p WHERE p.node_id=step_facts.node_id "
            "AND p.instance_key=step_facts.instance_key)",
            run=view.id,
            at=event.timestamp,
            steps=json.dumps(list(steps.values())),
            terminal=result.state.status in TERMINAL,
            closed=closed,
        )

    async def step_claimed(self, task_id: UUID, generation: int, worker_id: UUID, at: datetime) -> None:
        await self.execute(
            "UPDATE step_facts s SET started_at=coalesce(s.started_at,:at),status='running',"
            "ended_at=NULL,error_code=NULL,attempts=greatest(s.attempts,:generation),worker_id=:worker "
            "FROM task_facts f WHERE f.task_id=:task AND f.status='leased' "
            "AND f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment "
            "AND s.tenant_id=f.tenant_id AND s.project_id=f.project_id AND s.environment_id=f.environment_id "
            "AND s.run_id=f.run_id AND s.node_id=f.node_id AND s.instance_key=f.instance_key AND s.handled IS NULL",
            task=task_id,
            generation=generation,
            worker=worker_id,
            at=at,
        )

    async def project_terminal(
        self, run_id: UUID, status: Literal["cancelled", "timed_out"], at: datetime, sequence: int
    ) -> None:
        if status not in {"cancelled", "timed_out"}:
            raise ValueError("Terminal control status required")
        await self.execute(
            "WITH changed AS (UPDATE run_facts SET status=:status,updated_at=greatest(updated_at,:at),ended_at=:at,"
            "failed_node_id=NULL,failed_instance_key=NULL,failed_error_code=NULL,active_incidents=0,"
            f"last_event_sequence=:sequence WHERE {SCOPE} AND run_id=:run AND last_event_sequence<:sequence "
            "RETURNING run_id), tasks AS (UPDATE task_facts f SET status=t.status,ready_since=NULL FROM "
            "task_intents t WHERE f.task_id=t.id AND f.run_id IN (SELECT run_id FROM changed) "
            "AND f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment "
            "AND t.tenant_id=f.tenant_id AND t.project_id=f.project_id AND t.environment_id=f.environment_id "
            f"RETURNING f.task_id) UPDATE step_facts SET status=:status,ended_at=:at WHERE {SCOPE} "
            "AND run_id IN (SELECT run_id FROM changed) AND status IN ('scheduled','running','waiting')",
            run=run_id,
            status=status,
            at=at,
            sequence=sequence,
        )

    async def handled_failure(
        self,
        run_id: UUID,
        key: str,
        kind: StepKind,
        disposition: Literal["continue", "errorOutput"],
        error_code: str,
        at: datetime,
        *,
        task_id: UUID | None = None,
    ) -> None:
        identity = author_instance(key)
        if identity is None or disposition not in {"continue", "errorOutput"}:
            raise ValueError("Handled failure requires an author step and disposition")
        TypeAdapter(StepKind).validate_python(kind, strict=True)
        if task_id is not None:
            tasks = await self.rows(
                f"SELECT run_id,node_id,status FROM task_intents WHERE {SCOPE} AND id=:task", task=task_id
            )
            if len(tasks) != 1 or tasks[0] != dict(run_id=run_id, node_id=key, status="handled"):
                raise ValueError("Handled task authority does not match the projection")
        await self.execute(
            "WITH changed AS (INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,"
            "instance_key,kind,status,ended_at,error_code,handled) VALUES(:tenant,:project,:environment,:run,"
            ":node,:instance,:kind,'failed',:at,:code,:handled) ON CONFLICT(run_id,node_id,instance_key) "
            "DO UPDATE SET status='failed',ended_at=excluded.ended_at,error_code=excluded.error_code,"
            "handled=excluded.handled WHERE step_facts.handled IS NULL RETURNING run_id) "
            f"UPDATE run_facts SET handled_errors=handled_errors+1 WHERE {SCOPE} "
            "AND run_id IN (SELECT run_id FROM changed)",
            run=run_id,
            node=identity.node_id,
            instance=identity.instance_key,
            kind=kind,
            at=at,
            code=error_code,
            handled=disposition,
        )
        if task_id is not None:
            await self.execute(
                f"UPDATE task_facts SET status='handled',ready_since=NULL WHERE {SCOPE} AND task_id=:task", task=task_id
            )
