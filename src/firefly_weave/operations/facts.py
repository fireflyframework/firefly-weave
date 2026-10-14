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
import re
from dataclasses import dataclass
from datetime import datetime
from functools import partial
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
_AUTHOR_KEY: TypeAdapter[str] = TypeAdapter(InstanceKeyText)
_AUTHOR_NODE: TypeAdapter[str] = TypeAdapter(ResourceName)


def author_instance(key: str) -> InstanceView | None:
    if type(key) is not str:
        raise ValueError("Instance key must be text")
    if key.startswith("@"):
        return None
    _AUTHOR_KEY.validate_python(key, strict=True)
    value = instance_view(key)
    _AUTHOR_NODE.validate_python(value.node_id, strict=True)
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


REVALIDATION_BATCH = 100


def revalidation_due(policy: str) -> str:
    """SQL condition: the cached decision of fact row ``f`` is due for a refresh from its retained source.

    Due when an available or unsupported decision was recorded under another classification policy, or when it is a
    transient unsupported decision whose pinned IR version and features this platform runs again. A policy change
    never re-checks an unavailable decision: it can rest on evidence the source row does not show, such as an
    incoherent event history found by the upgrade, so it stays unavailable. Bind
    ``runtime.admission.runnable_parameters()``. The policy is written as a literal so the planner can prove that the
    partial index ``run_facts_revalidation``, which holds the rows not classified under the current policy and the
    unsupported ones, applies.
    """
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", policy) is None:
        raise ValueError("Classification policy must be a plain identifier")
    return (
        f"((f.classification_policy<>'{policy}' AND f.classification_state<>'unavailable') "
        "OR (f.classification_state='unsupported' "
        "AND f.pinned_ir_version=ANY(cast(:ir_versions AS text[])) "
        "AND f.pinned_features <@ cast(:ir_features AS jsonb)))"
    )


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
        if (
            type(self.test) is not bool
            or (self.origin == "test" and not self.test)
            or (self.test and self.origin not in {"test", "call"})
        ):
            raise ValueError("Test flag requires a direct test or inherited call origin")
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

    async def revalidation_cursor(self) -> UUID | None:
        """The environment's keyset boundary: the last run whose refresh was started."""
        value: UUID | None = await self.tx.session.scalar(
            text(f"SELECT after_run_id FROM operations_fact_cursors WHERE {SCOPE}"), self.params
        )
        return value

    async def advance_revalidation_cursor(self, after: UUID | None) -> None:
        """Move the boundary; bookkeeping only, with no fact or source change and no storage charge."""
        await self.execute(
            "INSERT INTO operations_fact_cursors(tenant_id,project_id,environment_id,after_run_id) "
            "VALUES(:tenant,:project,:environment,:after) ON CONFLICT(tenant_id,project_id,environment_id) "
            "DO UPDATE SET after_run_id=excluded.after_run_id",
            after=after,
        )

    async def revalidation_candidates(self, after: UUID | None) -> list[UUID]:
        """Up to ``REVALIDATION_BATCH`` due runs after ``after`` in UUID order, from fact metadata only."""
        from firefly_weave.runtime import admission

        rows = await self.rows(
            "SELECT f.run_id FROM run_facts f WHERE f.tenant_id=:tenant AND f.project_id=:project "
            f"AND f.environment_id=:environment AND {revalidation_due(admission.POLICY)} "
            f"AND (cast(:after AS uuid) IS NULL OR f.run_id>:after) ORDER BY f.run_id LIMIT {REVALIDATION_BATCH}",
            after=after,
            **admission.runnable_parameters(),
        )
        return [row["run_id"] for row in rows]

    async def refresh_listing_metadata(self, run_id: UUID) -> None:
        """Recompute one run's cached listing decision from its retained source.

        Only the five classification columns change: never the run's state, timing or status, and never a runtime
        transition, capacity block or policy block. Runtime admission stays the authority for execution. A source
        larger than the bounded fetch is recorded unavailable without being read. A run whose fact changed since it
        was selected, so that it is no longer due, keeps that newer decision.
        """
        from firefly_weave.operations.execution import execute_pure
        from firefly_weave.runtime import admission
        from firefly_weave.runtime.repository import RUN_FETCH_BYTES, RuntimeRepository

        values = {**self.params, "run": run_id}
        # Lock the source first, in the order runtime writers lock a run and then its facts.
        locked = await self.tx.session.scalar(text(f"SELECT id FROM runs WHERE {SCOPE} AND id=:run FOR UPDATE"), values)
        if locked is None:
            return
        # Rechecked under the lock: an authoritative update since the selection keeps its newer decision.
        due = await self.tx.session.scalar(
            text(
                "SELECT f.run_id FROM run_facts f WHERE f.tenant_id=:tenant AND f.project_id=:project "
                f"AND f.environment_id=:environment AND f.run_id=:run AND {revalidation_due(admission.POLICY)} "
                "FOR UPDATE"
            ),
            {**values, **admission.runnable_parameters()},
        )
        if due is None:
            return
        # Measured under the lock, so the size cannot change before the fetch.
        size = await self.tx.session.scalar(
            text(f"SELECT public.weave_runs_bytes(runs) FROM runs WHERE {SCOPE} AND id=:run"), values
        )
        if size is None or size > RUN_FETCH_BYTES:
            await self.execute(
                "UPDATE run_facts SET classification_state='unavailable',classification_policy=:policy "
                f"WHERE {SCOPE} AND run_id=:run",
                policy=admission.POLICY,
                run=run_id,
            )
            return
        row = await RuntimeRepository(self.tx).run(run_id, lock=True)
        # A refresh is never a runtime candidate: a failed write must not leave a capacity block on the run.
        self.tx.session.info.pop("weave_runtime_candidate", None)
        metadata = await execute_pure(partial(listing_metadata, row))
        await self.execute(
            "UPDATE run_facts SET classification_state=:classification_state,"
            "classification_policy=:classification_policy,pinned_ir_version=:pinned_ir_version,"
            "pinned_features=cast(:pinned_features AS jsonb),node_kinds=cast(:node_kinds AS jsonb) "
            f"WHERE {SCOPE} AND run_id=:run",
            run=run_id,
            classification_state=metadata["classification_state"],
            classification_policy=metadata["classification_policy"],
            pinned_ir_version=metadata["pinned_ir_version"],
            pinned_features=json.dumps(metadata["pinned_features"], ensure_ascii=False),
            node_kinds=json.dumps(metadata["node_kinds"], ensure_ascii=False),
        )

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

        def kind_of(node: str) -> StepKind | None:
            # Retained runs can be admitted even when their listing cache is incomplete.
            try:
                return TypeAdapter(StepKind).validate_python(kinds.get(node), strict=True)
            except ValueError:
                return None

        def add(key: str, status: str, *, scheduled: bool = False, error: str | None = None) -> None:
            # A loop's yield (each~1) is a wake-up of the loop, not a run of a step.
            identity = author_instance(key) if "~" not in key else None
            if identity is None:
                return
            kind = kind_of(identity.node_id)
            observed = scheduled or kind in {"transform", "decisionTable", "fail"}
            previous = steps.get((identity.node_id, identity.instance_key), {})
            at = event.timestamp.isoformat()
            steps[identity.node_id, identity.instance_key] = dict(
                node_id=identity.node_id,
                instance_key=identity.instance_key,
                kind=kind,
                status=status,
                scheduled_at=previous.get("scheduled_at") or (at if observed else None),
                started_at=previous.get("started_at")
                or (at if observed and status != "scheduled" and kind not in {"action", "llm", "agent"} else None),
                ended_at=at if status in TERMINAL else None,
                error_code=error,
            )

        if not isinstance(view, TerminalControl):
            tasks = {command.node_id for command in result.commands if isinstance(command, TaskIntent)}
            for command in result.commands:
                if isinstance(command, (TaskIntent, Deadline, HumanTaskIntent)):
                    add(command.node_id, "scheduled" if isinstance(command, TaskIntent) else "waiting", scheduled=True)
                elif command.kind == "spawn_branch":
                    join = result.state.joins.get(command.node_id)
                    first = result.state.branches.get(join.branches[0]) if join and join.branches else None
                    # Later branch dispatches do not witness the join's original activation.
                    add(command.node_id, "running", scheduled=first is not None and command.branch == first.name)
            for change in result.steps:
                identity = author_instance(change.node_id)
                if identity is None:
                    continue
                kind = kind_of(identity.node_id)
                status = (
                    "succeeded"
                    if change.status == "completed"
                    else ("scheduled" if kind in {"action", "llm", "agent"} or change.node_id in tasks else "waiting")
                )
                add(change.node_id, status, scheduled=change.status == "waiting")
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
            "SELECT :tenant,:project,:environment,:run,s.node_id,s.instance_key,coalesce(s.kind,known.kind),"
            "s.status,s.scheduled_at,"
            "s.started_at,s.ended_at,s.error_code FROM jsonb_to_recordset(cast(:steps AS jsonb)) "
            "AS s(node_id text,instance_key text,kind text,status text,scheduled_at timestamptz,started_at timestamptz,"
            "ended_at timestamptz,error_code text) LEFT JOIN step_facts known ON known.run_id=:run "
            "AND known.tenant_id=:tenant AND known.project_id=:project AND known.environment_id=:environment "
            "AND known.node_id=s.node_id AND known.instance_key=s.instance_key "
            "WHERE coalesce(s.kind,known.kind) IS NOT NULL ON CONFLICT(run_id,node_id,instance_key) DO UPDATE SET "
            "status=excluded.status,scheduled_at=coalesce(step_facts.scheduled_at,excluded.scheduled_at),"
            "started_at=coalesce(step_facts.started_at,excluded.started_at),"
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
