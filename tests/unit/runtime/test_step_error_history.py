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

"""A history with handled failures replays from its own events and evidence, and exports carry codes only."""

from datetime import timedelta
from uuid import uuid4

from step_error_support import NOW, action, compiled, path

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations import (
    DeadlineFact,
    RecordedEvidence,
    SourceReference,
    TaskFact,
    TaskReceipt,
    TaskTiming,
    WaitReceipt,
)
from firefly_weave.operations.exports import lock_digest, safe_event, transition_digest
from firefly_weave.operations.redaction import SafeProjection
from firefly_weave.operations.replay import replay
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent, TaskIntent

SIGNAL = {
    "id": "confirmed",
    "kind": "signal",
    "name": "confirmed",
    "timeoutSeconds": 60,
    "payloadSchema": {},
    "onError": "errorOutput",
    "errorPath": path(output={"literal": "late"}),
}


def recorded(flow, events, *, recovery=(), waits=()):
    """Accept events through the kernel and attach the evidence the runtime records for each.

    ``recovery`` names the sequences decided by recovery (their task timing), ``waits`` those fired by a deadline.
    """
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source = SourceReference(scope=scope, version_id=uuid4(), source_hash=flow.source_hash, format="json")
    run_id, state, history, tasks, deadlines = uuid4(), RunState(input={}), [], {}, {}
    for index, (kind, at, data) in enumerate(events, 1):
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=at, sequence=index, data=data)
        change = transition(state, event, flow)
        issued_tasks, issued_deadlines = [], []
        for command in change.commands:
            if isinstance(command, TaskIntent):
                tasks[command.node_id] = TaskFact(
                    task_id=uuid4(),
                    release_id=uuid4(),
                    node_id=command.node_id,
                    action_digest=command.action_digest,
                    deadline=command.deadline,
                )
                issued_tasks.append(tasks[command.node_id])
            elif isinstance(command, Deadline):
                deadlines[command.node_id] = DeadlineFact(
                    wait_id=uuid4(), node_id=command.node_id, deadline=command.deadline, name=command.name
                )
                issued_deadlines.append(deadlines[command.node_id])
        task = tasks.get(str(data.get("node_id")))
        receipt = None
        if task is not None and data.get("generation", 0) > 0:
            timing = TaskTiming(
                kind="recovery_decision" if index in recovery else "live_admission",
                checked_at=at,
                effective_deadline=task.deadline,
            )
            receipt = TaskReceipt(task=task, receipt_id=event.id, generation=data["generation"], timing=timing)
        wait = None
        if index in waits:
            wait = WaitReceipt(wait_id=deadlines[data["node_id"]].wait_id, complete=True, pending_signals=[])
            event = event.model_copy(update={"data": {**data, "wait_id": str(wait.wait_id)}})
            change = transition(state, event, flow)
        evidence = RecordedEvidence(
            provenance="synthetic_kernel",
            run_id=run_id,
            scope=scope,
            artifact_digest=flow.digest,
            lock_digest=lock_digest(flow),
            source=source,
            initial_input=SafeProjection(available=True, value={}) if index == 1 else None,
            expected_transition_digest=transition_digest(change),
            issued_tasks=issued_tasks,
            issued_deadlines=issued_deadlines,
            task_receipt=receipt,
            wait_receipt=wait,
        )
        history.append(event.model_copy(update={"evidence": evidence.model_dump(mode="json")}))
        state = change.state
    return tuple(history)


START = ("started", NOW, {"admission_policy": "classified-v1"})
FAILED = (
    "task_failed",
    NOW,
    {
        "node_id": "lookup",
        "generation": 1,
        "output": {"code": "RATE_LIMITED", "outcome": "failed", "completion_id": str(uuid4())},
    },
)
HANDLED = (
    "step_failed",
    NOW,
    {"node_id": "lookup", "generation": 1, "code": "RATE_LIMITED", "outcome": "failed", "attempts": 1},
)


def test_a_failure_recovery_handed_to_its_step_replays_consistently():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error/code"})
    report = replay(flow, recorded(flow, [START, FAILED, HANDLED], recovery={3}))
    assert report.status == "consistent", report
    assert report.final_state is not None and report.final_state.output == "RATE_LIMITED"


def test_a_tampered_handled_failure_is_inconsistent():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error/code"})
    events = list(recorded(flow, [START, FAILED, HANDLED], recovery={3}))
    events[2] = events[2].model_copy(update={"data": {**events[2].data, "code": "UNAVAILABLE"}})
    report = replay(flow, tuple(events))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-TRANSITION"])


def test_a_handled_failure_on_a_step_that_stops_the_run_is_inconsistent():
    flow = compiled(action("lookup"))
    events = recorded(flow, [START, FAILED, HANDLED], recovery={3})
    report = replay(flow, events)
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])


def test_a_task_nobody_claimed_replays_only_after_its_deadline():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error/code"})
    late = NOW + timedelta(seconds=31)
    unclaimed = {
        "node_id": "lookup",
        "generation": 0,
        "code": "WV-TASK-DEADLINE",
        "outcome": "not_started",
        "attempts": 0,
    }
    assert replay(flow, recorded(flow, [START, ("step_failed", late, unclaimed)])).status == "consistent"
    early = replay(flow, recorded(flow, [START, ("step_failed", NOW, unclaimed)]))
    assert (early.status, [d.code for d in early.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])
    # The attempts it reports are the attempts that started before the one nobody claimed.
    miscounted = {**unclaimed, "attempts": 1}
    wrong = replay(flow, recorded(flow, [START, ("step_failed", late, miscounted)]))
    assert (wrong.status, [d.code for d in wrong.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])


def test_a_wait_deadline_handed_to_its_step_replays_with_its_deadline_evidence():
    flow = compiled(SIGNAL, output={"ref": "/steps/confirmed/output"})
    due = NOW + timedelta(seconds=60)
    timeout = {
        "node_id": "confirmed",
        "generation": 0,
        "code": "WV-SIGNAL-TIMEOUT",
        "outcome": "not_started",
        "attempts": 1,
        "deadline": due.isoformat(),
    }
    report = replay(flow, recorded(flow, [START, ("step_failed", due, timeout)], waits={2}))
    assert report.status == "consistent", report
    assert report.final_state is not None and report.final_state.output == "late"


def test_exports_keep_codes_outcomes_and_counts_of_a_handled_failure_and_nothing_else():
    flow = compiled(action("lookup", onError="continue"))
    data = {**HANDLED[2], "message": "worker text that must not leave", "child_run_id": str(uuid4())}
    event = RuntimeEvent(id=uuid4(), type="step_failed", timestamp=NOW, sequence=3, data=data)
    exported = safe_event(event, flow).data
    assert "message" not in exported and exported["code"] == "RATE_LIMITED" and "child_run_id" in exported


def test_confirming_that_a_step_had_no_effect_replays_and_is_refused_for_a_step_that_stops():
    def history(**settings):
        flow = compiled(action("place", uses="submit", value={"object": {}}, **settings), output={"literal": "done"})
        unknown = {"code": "TIMEOUT", "outcome": "unknown", "completion_id": str(uuid4())}
        resolved = {
            "incident_key": "place:1",
            "receipt_id": str(uuid4()),
            "node_id": "place",
            "generation": 1,
            "kind": "accept_reconciled_failure",
            "output": None,
            "attempts": 1,
            "actor_id": str(uuid4()),
        }
        events = [
            START,
            ("task_failed", NOW, {"node_id": "place", "generation": 1, "output": unknown}),
            ("incident_opened", NOW, {"node_id": "place", "generation": 1, "code": "WV-TASK-AMBIGUOUS"}),
            ("incident_resolved", NOW, resolved),
        ]
        return flow, recorded(flow, events, recovery={3})

    flow, events = history(onError="continue")
    report = replay(flow, events)
    assert report.status == "consistent", report
    assert report.final_state is not None and report.final_state.output == "done"
    flow, events = history()
    report = replay(flow, events)
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])


def test_confirming_no_effect_does_not_replay_for_an_interim_failure():
    flow = compiled(action("lookup", onError="continue"), output={"literal": "done"})
    resolved = {
        "incident_key": "lookup:1",
        "receipt_id": str(uuid4()),
        "node_id": "lookup",
        "generation": 1,
        "kind": "accept_reconciled_failure",
        "output": None,
        "attempts": 1,
        "actor_id": str(uuid4()),
    }
    report = replay(flow, recorded(flow, [START, FAILED, ("incident_resolved", NOW, resolved)]))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])


def test_a_task_nobody_claimed_after_a_lost_attempt_replays_only_with_an_unknown_outcome():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error/code"})
    lost = (
        "recovery_scheduled",
        NOW,
        {
            "node_id": "lookup",
            "generation": 1,
            "code": None,
            "next_attempt_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
    )
    late = NOW + timedelta(seconds=31)
    unclaimed = {"node_id": "lookup", "generation": 1, "code": "WV-TASK-DEADLINE", "attempts": 1}
    honest = replay(
        flow, recorded(flow, [START, lost, ("step_failed", late, {**unclaimed, "outcome": "unknown"})], recovery={2})
    )
    assert honest.status == "consistent", honest
    hidden = replay(
        flow,
        recorded(flow, [START, lost, ("step_failed", late, {**unclaimed, "outcome": "not_started"})], recovery={2}),
    )
    assert (hidden.status, [d.code for d in hidden.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])


def test_a_handled_failure_after_retries_replays_only_with_the_outcome_of_its_attempts():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error/code"})
    hidden = ("step_failed", NOW, {**HANDLED[2], "outcome": "not_started"})
    report = replay(flow, recorded(flow, [START, FAILED, hidden], recovery={3}))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-EVENT"])
