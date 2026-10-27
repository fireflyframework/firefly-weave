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

"""Durable retry scheduling and the minimum run-wide ambiguity barrier."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.access.service import AccessService
from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.ai import TRANSIENT_MODEL_CODES
from firefly_weave.contracts.operations import TaskTiming
from firefly_weave.contracts.runtime import RecoveryReport
from firefly_weave.contracts.transactions import split_phase
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.facts import task_fact_update
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.calls import CallService
from firefly_weave.runtime.deadlines import DeadlineService
from firefly_weave.runtime.kernel import error_policy, workflow
from firefly_weave.runtime.kernel import transition_async as transition
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService, event_hash, view_of
from firefly_weave.runtime.step_failures import LEASE_EXPIRED, attempt_outcomes, failure_data, final_failure
from firefly_weave.runtime.transactions import in_transaction, shapes
from firefly_weave.runtime.waits import TERMINAL
from firefly_weave.workers.leases import TaskService

# Failure codes recovery may retry, only for read_only, idempotent and idempotency_key actions.
TRANSIENT_FAILURE_CODES = frozenset({"TRANSIENT", "UNAVAILABLE", "RATE_LIMITED", "TIMEOUT"}) | TRANSIENT_MODEL_CODES


@service
class RecoveryService:
    def __init__(
        self, runtime: RuntimeService, tasks: TaskService, deadlines: DeadlineService, access: AccessService
    ) -> None:
        self.runtime, self.tasks, self.deadlines = runtime, tasks, deadlines
        self.access = access
        self.calls = CallService(runtime)

    async def scan(
        self,
        scope: Scope,
        limit: int,
        *,
        actor: Principal | None = None,
        context: AuditContext | None = None,
    ) -> RecoveryReport:
        if actor is None or context is None:
            raise AccessDenied()

        async def authorize(tx: Transaction) -> None:
            for current in (actor, await self.access.load_principal(actor.id, tx=tx)):
                self.runtime.require(current, scope, "run.retry", context)

        return await self._quanta(scope, limit, authorize)

    async def _scan_scheduled(self, authority: _SchedulerScope, limit: int) -> RecoveryReport:
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        return await self._quanta(authority.scope, limit, authority.verify)

    async def _scan_terminal_scheduled(self, authority: _SchedulerScope, limit: int) -> RecoveryReport:
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        return await self._quanta(authority.scope, limit, authority.verify, terminal_only=True)

    async def _quanta(
        self,
        scope: Scope,
        limit: int,
        authorize: Callable[[Transaction], Awaitable[None]],
        *,
        terminal_only: bool = False,
    ) -> RecoveryReport:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded recovery page required")
        totals = {name: 0 for name in RecoveryReport.model_fields}
        # Cancelling the children of ended callers is terminal control, so it also runs while starts are refused, and
        # it runs right after due deadlines: stopping work never waits behind task recovery or call starts.
        kinds = ("terminal", "cancel") if terminal_only else ("deadline", "cancel", "expired", "retry", "dispatch")
        for kind in kinds:
            for _ in range(limit):
                try:
                    async with self.runtime.definitions.transaction(scope, None) as tx:
                        await authorize(tx)
                        if kind in {"expired", "retry", "dispatch"}:
                            self.runtime.definitions.registry.require_operational()
                        report = await self._recover(tx, kind)
                except CatalogError as error:
                    if error.code == "WV-RUNTIME-LIMIT":
                        continue
                    raise
                for name, value in report.model_dump().items():
                    totals[name] += value
                if not any(report.model_dump().values()):
                    break
        return RecoveryReport(**totals)

    async def _recover(self, tx: Transaction, kind: str) -> RecoveryReport:
        limit = 1
        if kind == "dispatch":
            started, rejected = await self.calls.dispatch(tx, 10)
            return RecoveryReport(calls_started=started, calls_rejected=rejected)
        if kind == "cancel":
            return RecoveryReport(calls_cancelled=await self.calls.cancel_children(tx, 10))
        deadlines = (
            await self.deadlines.scan(tx, 1, terminal_only=kind == "terminal")
            if kind in {"terminal", "deadline"}
            else RecoveryReport()
        )
        resumed = incidents = failures = 0
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        for identifier in await repository.expired_ready(limit) if kind == "expired" else []:
            locked = await self.runtime.lock_task(tx, identifier, skip_locked=True)
            if locked is None:
                continue
            row, task = locked
            if await self.runtime.observe_unavailable(tx, row):
                continue
            view = view_of(row)
            # Settlement work also expires while its run is held by another incident.
            if task["status"] != "ready" or (
                view.state.status != "waiting" and not (view.state.status == "suspended" and task["settlement"])
            ):
                continue
            artifact = import_artifact(row["artifact"])
            expired: dict[str, Any] = {"node_id": task["node_id"], "code": "WV-TASK-DEADLINE"}
            kind = "incident_opened"
            inside = bool(view.state.transactions) and in_transaction(shapes(workflow(artifact)), task["node_id"])
            if inside or error_policy(workflow(artifact), task["node_id"]) != "stop":
                # The deadline passed before any worker claimed the latest attempt: it failed for good, and only
                # earlier attempts may have applied. A transaction already knows what they did; a step that handles
                # its failures takes this one.
                generations = await repository.rows(
                    f"SELECT coalesce(max(generation),0) AS value FROM task_leases WHERE {SCOPE} AND task_id=:task",
                    task=identifier,
                )
                attempts = int(generations[0]["value"])
                kind = "step_failed"
                expired = failure_data(task["node_id"], attempts, "WV-TASK-DEADLINE", "not_started", attempts)
                if inside:
                    expired["effect"] = "none"
            event = RuntimeEvent(
                id=uuid4(),
                type=kind,  # type: ignore[arg-type]
                timestamp=await repository.now(),
                sequence=view.state.accepted_sequence + 1,
                data=expired,
            )
            tx.session.info["weave_runtime_candidate"] = row["id"]
            result = await transition(view.state, event, artifact)
            await repository.persist(view.model_copy(update={"state": result.state}), event, result, event_hash(event))
            if kind == "incident_opened":
                await repository.task_status(identifier, "incident", at=event.timestamp)
                tx.record("step_error", "step.incident", "failed")
            if kind == "step_failed" and not inside:
                failures += 1
            else:
                incidents += 1
        for identifier in await self.tasks.recovery_candidates(tx, limit) if kind == "retry" else []:
            locked = await self.runtime.lock_task(tx, identifier, skip_locked=True)
            if locked is None:
                continue
            row, task = locked
            if task["status"] == "handled":
                continue
            if await self.runtime.observe_unavailable(tx, row):
                continue
            attempt = await self.tasks.recovery_attempt(tx, identifier)
            if attempt is None:
                continue
            if (
                any(
                    item[field] != getattr(tx.scope, field)
                    for item in (row, task, attempt)
                    for field in ("tenant_id", "project_id", "environment_id")
                )
                or task["run_id"] != row["id"]
                or attempt["task_id"] != task["id"]
                or (row["state"]["status"] not in TERMINAL and task["node_id"] not in row["state"]["active"])
                or task["payload"]["node_id"] != task["node_id"]
            ):
                raise AccessDenied()
            view = view_of(row)
            # Another run-wide incident must never be cleared by task recovery.
            node_incident = view.state.incidents.get(f"{task['node_id']}:{attempt['generation']}")
            if (
                view.state.status in TERMINAL
                or (node_incident is not None and node_incident.code != "WV-TASK-FAILED")
                or (
                    view.state.status == "suspended"
                    and not view.state.incidents
                    and view.state.incident != "WV-TASK-FAILED"
                )
            ):
                await self.tasks.retire_attempt(tx, identifier, attempt["generation"])
                continue
            now = await repository.now()
            policy = attempt["policy"]
            retry = policy["retry"]
            safe = policy["side_effect"] in {"read_only", "idempotent", "idempotency_key"}
            # An expired lease is a classified transport loss. Explicit failures require a known transient code.
            transient = attempt["status"] == "active"
            failure: dict[str, Any] = {}
            if attempt["status"] == "failed":
                events = await repository.rows(
                    f"SELECT data FROM run_events WHERE {SCOPE} AND run_id=:run "
                    "AND type='task_failed' AND data->>'node_id'=:node "
                    "AND coalesce((data->>'generation')::int,:generation)=:generation ORDER BY sequence DESC LIMIT 1",
                    run=row["id"],
                    node=task["node_id"],
                    generation=attempt["generation"],
                )
                failure = events[0]["data"] if events else {}
                transient = bool(events and events[0]["data"]["output"]["code"] in TRANSIENT_FAILURE_CODES)
            artifact = import_artifact(row["artifact"])
            # Only a run that entered a transaction has steps whose failures belong to one.
            facts = shapes(workflow(artifact)) if view.state.transactions else None
            transactional = in_transaction(facts, task["node_id"])
            # A step still in flight when its transaction went back is never tried again: it settles now. Any other
            # step, the one that can't be undone included, keeps its action's retries.
            owner = facts.owner_key(task["node_id"]) if facts is not None else None
            going_back = owner is not None and (
                (record := view.state.transactions.get(owner)) is not None
                and (record.rollback or record.status in {"compensating", "in_doubt"})
            )
            settles = going_back and split_phase(task["node_id"]) is None
            allowed = (
                safe
                and transient
                and not settles
                and attempt["generation"] < retry["maxAttempts"]
                and now < attempt["deadline"]
            )
            delay = min(
                retry["maxDelaySeconds"], retry["initialDelaySeconds"] * 2 ** min(attempt["generation"] - 1, 30)
            )
            next_attempt = (
                now + timedelta(seconds=delay) if delay < (attempt["deadline"] - now).total_seconds() else None
            )
            allowed = allowed and next_attempt is not None
            code = None if allowed else ("WV-TASK-AMBIGUOUS" if not safe else "WV-TASK-RETRIES-EXHAUSTED")
            kind = "recovery_scheduled" if allowed else "incident_opened"
            handled: JsonObject | None = None
            data: dict[str, Any] = {
                "node_id": task["node_id"],
                "generation": attempt["generation"],
                "code": code,
                "next_attempt_at": next_attempt.isoformat() if allowed and next_attempt is not None else None,
            }
            if transactional:
                # An expired lease may have applied; a failure says what its release could prove.
                effect = failure.get("effect", "unknown") if attempt["status"] == "failed" else "unknown"
                if allowed:
                    data["effect"] = effect
                else:
                    # It failed for good: its transaction decides what follows, with the worker's own code.
                    reported = failure.get("output", {}).get("code") if failure else None
                    kind = "step_failed"
                    data = {
                        "node_id": task["node_id"],
                        "generation": attempt["generation"],
                        "code": reported or code,
                        "outcome": "unknown" if effect == "unknown" else "failed",
                        "attempts": attempt["generation"],
                        "effect": effect,
                    }
            elif not allowed and (setting := error_policy(workflow(artifact), task["node_id"])) != "stop":
                # A step that handles its failures takes this one, unless its effect may have happened and it is
                # never retried: then the run stops as it does today, until someone checks the effect.
                leases = await repository.rows(
                    f"SELECT generation FROM task_leases WHERE {SCOPE} AND task_id=:task", task=identifier
                )
                reports = await repository.rows(
                    f"SELECT coalesce((data->>'generation')::int,0) AS generation,"
                    f"coalesce(data->'output'->>'outcome','unknown') AS outcome FROM run_events WHERE {SCOPE} "
                    "AND run_id=:run AND type='task_failed' AND data->>'node_id'=:node",
                    run=row["id"],
                    node=task["node_id"],
                )
                outcome = attempt_outcomes(
                    [lease["generation"] for lease in leases],
                    {report["generation"]: report["outcome"] for report in reports},
                )
                if final_failure(setting, policy["side_effect"], outcome) == "handled":
                    last = failure["output"]["code"] if attempt["status"] == "failed" else LEASE_EXPIRED
                    handled = failure_data(task["node_id"], attempt["generation"], last, outcome, len(leases))
                    kind = "step_failed"
                    data = handled
            event = RuntimeEvent(
                id=uuid4(),
                type=kind,  # type: ignore[arg-type]
                timestamp=now,
                sequence=view.state.accepted_sequence + 1,
                data=data,
            )
            tx.session.info["weave_runtime_candidate"] = row["id"]
            result = await transition(view.state, event, artifact)
            await repository.persist(
                view.model_copy(update={"state": result.state}),
                event,
                result,
                event_hash(event),
                task_timing=TaskTiming(
                    kind="recovery_decision", checked_at=now, effective_deadline=attempt["deadline"]
                ),
            )
            await self.tasks.retire_attempt(tx, identifier, attempt["generation"])
            # Releasing the barrier may apply a deferred result whose continuation
            # terminates the run. Its cancellation must remain authoritative.
            if result.state.status in TERMINAL or kind == "step_failed":
                failures += int(handled is not None)
                incidents += int(kind == "step_failed" and handled is None)
                continue
            await repository.execute(
                task_fact_update(
                    f"UPDATE task_intents SET status=:status,next_attempt_at=:next WHERE {SCOPE} AND id=:id"
                ),
                at=now,
                id=identifier,
                status="ready" if allowed else "handled" if handled else "incident",
                next=next_attempt if allowed else None,
            )
            resumed += int(allowed)
            failures += int(handled is not None)
            incidents += int(not allowed and handled is None)
            if not allowed and handled is None:
                tx.record("step_error", "step.incident", "failed")
        return RecoveryReport(
            resumed=resumed,
            incidents=incidents,
            timed_out=deadlines.timed_out,
            elapsed=deadlines.elapsed,
            handled_failures=failures,
        )
