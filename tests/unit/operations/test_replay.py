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

"""Recorded kernel facts are replayed without external outcomes or guessed input."""

import json
import socket
from datetime import UTC, datetime
from uuid import uuid4

import pytest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def recorded_events(worker_runtime_fixture):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.operations import RecordedEvidence, SourceReference, TaskFact, TaskReceipt, TaskTiming
    from firefly_weave.operations.exports import lock_digest, transition_digest
    from firefly_weave.operations.redaction import SafeProjection
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent

    artifact = worker_runtime_fixture["artifact"]
    run_id, version_id, task_id, release_id = (uuid4() for _ in range(4))
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source = SourceReference(scope=scope, version_id=version_id, source_hash=artifact.source_hash, format="json")
    state = RunState(input=3)
    result = []
    task = None
    for i, (kind, data) in enumerate(
        [
            ("started", {"admission_policy": "classified-v1"}),
            ("task_completed", {"node_id": "work", "generation": 1, "output": 9}),
        ],
        1,
    ):
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=i, data=data)
        change = transition(state, event, artifact)
        issued = []
        for command in change.commands:
            if isinstance(command, TaskIntent):
                task = TaskFact(
                    task_id=task_id,
                    release_id=release_id,
                    node_id=command.node_id,
                    action_digest=command.action_digest,
                    deadline=command.deadline,
                )
                issued.append(task)
        evidence = RecordedEvidence(
            provenance="synthetic_kernel",
            run_id=run_id,
            scope=scope,
            artifact_digest=artifact.digest,
            lock_digest=lock_digest(artifact),
            source=source,
            initial_input=SafeProjection(available=True, value=3) if i == 1 else None,
            expected_transition_digest=transition_digest(change),
            issued_tasks=issued,
            task_receipt=TaskReceipt(
                task=task,
                receipt_id=event.id,
                generation=1,
                timing=TaskTiming(kind="live_admission", checked_at=NOW, effective_deadline=task.deadline),
            )
            if i == 2
            else None,
        )
        result.append(event.model_copy(update={"evidence": evidence.model_dump(mode="json")}))
        state = change.state
    return tuple(result)


def test_full_recorded_terminal_stream_offline(worker_runtime_fixture, recorded_events, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay attempted networking")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from firefly_weave.operations.replay import replay

    report = replay(worker_runtime_fixture["artifact"], recorded_events)
    assert report.status == "consistent", report
    assert report.final_state.output == 9 and report.last_verified_sequence == 2


def test_gap_in_history_is_incomplete(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    events = (recorded_events[0], recorded_events[1].model_copy(update={"sequence": 3}))
    report = replay(worker_runtime_fixture["artifact"], events)
    assert report.status == "incomplete" and report.last_verified_sequence == 1


def test_missing_initial_facts_and_open_prefix_are_incomplete(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    artifact = worker_runtime_fixture["artifact"]
    legacy = tuple(e.model_copy(update={"evidence": None}) for e in recorded_events)
    assert replay(artifact, legacy).status == "incomplete"
    assert replay(artifact, legacy).last_verified_sequence == 0
    prefix = replay(artifact, recorded_events[:1])
    assert prefix.status == "incomplete" and prefix.last_verified_sequence == 1
    assert prefix.final_state.status == "waiting"


def test_required_redaction_stops_at_prefix(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    evidence = {**recorded_events[1].evidence, "omissions": [{"path": "/data/output", "reason": "classified_secret"}]}
    last = recorded_events[1].model_copy(update={"data": {"node_id": "work", "generation": 1}, "evidence": evidence})
    report = replay(worker_runtime_fixture["artifact"], (recorded_events[0], last))
    assert report.status == "incomplete" and report.last_verified_sequence == 1


@pytest.mark.parametrize(
    "mutation", ["artifact", "lock", "duplicate", "transition", "task", "generation", "boolean-generation", "node"]
)
def test_corrupt_evidence_is_inconsistent(worker_runtime_fixture, recorded_events, mutation):
    from firefly_weave.operations.replay import replay

    events = list(recorded_events)
    last = events[-1].model_copy(deep=True)
    if mutation in {"artifact", "lock"}:
        last.evidence[mutation + "_digest"] = "0" * 64
    elif mutation == "duplicate":
        events.append(last.model_copy(update={"sequence": 3}))
    elif mutation == "transition":
        last.evidence["expected_transition_digest"] = "0" * 64
    elif mutation == "task":
        last.evidence["task_receipt"]["task"]["task_id"] = str(uuid4())
    elif mutation == "generation":
        last.data["generation"] = 2
    elif mutation == "boolean-generation":
        last.data["generation"] = True
    else:
        last.data["node_id"] = "missing"
    if mutation != "duplicate":
        events[-1] = last
    assert replay(worker_runtime_fixture["artifact"], tuple(events)).status == "inconsistent"


def test_wrong_artifact(worker_runtime_fixture, recorded_events):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.operations.replay import replay

    source = json.loads(worker_runtime_fixture["source"])
    source["metadata"]["version"] = "1.0.1"
    other = compile_source(json.dumps(source), format="json", catalog=worker_runtime_fixture["catalog"]).artifact
    assert replay(other, recorded_events).status == "inconsistent"


def test_missing_issuance_is_incomplete_and_wrong_release_is_inconsistent(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    artifact = worker_runtime_fixture["artifact"]
    first = recorded_events[0].model_copy(deep=True)
    first.evidence["issued_tasks"] = []
    report = replay(artifact, (first, recorded_events[1]))
    assert report.status == "incomplete" and report.last_verified_sequence == 0
    last = recorded_events[1].model_copy(deep=True)
    last.evidence["task_receipt"]["task"]["release_id"] = str(uuid4())
    assert replay(artifact, (recorded_events[0], last)).status == "inconsistent"


def test_replay_limits_preserve_verified_prefix(worker_runtime_fixture, recorded_events, monkeypatch):
    import importlib

    module = importlib.import_module("firefly_weave.operations.replay")
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    result = module.replay(worker_runtime_fixture["artifact"], recorded_events)
    assert result.status == "incomplete" and result.last_verified_sequence == 1
    assert result.diagnostics[0].code == "WV-REPLAY-LIMIT"


def test_source_revision_is_part_of_identity(worker_runtime_fixture, recorded_events):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.operations.replay import replay

    source = json.dumps(json.loads(worker_runtime_fixture["source"]), indent=4)
    other = compile_source(source, format="json", catalog=worker_runtime_fixture["catalog"]).artifact
    assert other.digest == worker_runtime_fixture["artifact"].digest
    assert other.source_hash != worker_runtime_fixture["artifact"].source_hash
    assert replay(other, recorded_events).status == "inconsistent"


def timer_facts(kind="wait", *, received_seconds=1):
    from datetime import timedelta

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.operations import (
        DeadlineFact,
        RecordedEvidence,
        SignalFact,
        SourceReference,
        WaitReceipt,
    )
    from firefly_weave.operations.exports import lock_digest, transition_digest
    from firefly_weave.operations.redaction import SafeProjection
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent

    step = {
        "id": "pause",
        "kind": kind,
        **(
            {"durationSeconds": 2}
            if kind == "wait"
            else {"name": "approval", "timeoutSeconds": 2, "payloadSchema": {"type": "integer"}}
        ),
    }
    artifact = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "timers", "version": "1.0.0"},
            "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [step], "output": {"ref": "/steps/pause/output"}},
        },
        format="object",
        catalog=CatalogSnapshot.empty(),
    ).artifact
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    base = dict(
        run_id=uuid4(),
        scope=scope,
        artifact_digest=artifact.digest,
        lock_digest=lock_digest(artifact),
        source=SourceReference(scope=scope, version_id=uuid4(), source_hash=artifact.source_hash, format="json"),
        provenance="synthetic_kernel",
    )
    first = RuntimeEvent(
        id=uuid4(), sequence=1, timestamp=NOW, type="started", data={"admission_policy": "classified-v1"}
    )
    change = transition(RunState(input={}), first, artifact)
    command = next(c for c in change.commands if isinstance(c, Deadline))
    due = DeadlineFact(wait_id=uuid4(), node_id=command.node_id, deadline=command.deadline, name=command.name)
    evidence = RecordedEvidence(
        **base,
        initial_input=SafeProjection(available=True, value={}),
        expected_transition_digest=transition_digest(change),
        issued_deadlines=[due],
    )
    first = first.model_copy(update={"evidence": evidence.model_dump(mode="json")})
    identifier = uuid4()
    data = {"node_id": "pause", "deadline": due.deadline.isoformat()}
    pending = []
    if kind == "signal":
        pending = [
            SignalFact(receipt_id=identifier, name="approval", accepted_at=NOW + timedelta(seconds=received_seconds))
        ]
        data.update(output=7, accepted_at=pending[0].accepted_at.isoformat())
    else:
        data.update(wait_id=str(due.wait_id))
    last = RuntimeEvent(
        id=identifier,
        sequence=2,
        timestamp=NOW + timedelta(seconds=2),
        type="signal_received" if kind == "signal" else "wait_elapsed",
        data=data,
    )
    final = transition(change.state, last, artifact)
    evidence = RecordedEvidence(
        **base,
        expected_transition_digest=transition_digest(final),
        wait_receipt=WaitReceipt(wait_id=due.wait_id, pending_signals=pending),
    )
    return artifact, (first, last.model_copy(update={"evidence": evidence.model_dump(mode="json")}))


@pytest.mark.parametrize("kind", ["wait", "signal"])
def test_issued_timer_and_signal_arbitration(kind):
    from firefly_weave.operations.replay import replay

    artifact, events = timer_facts(kind)
    assert replay(artifact, events).status == "consistent"
    bad = events[1].model_copy(deep=True)
    bad.evidence["wait_receipt"]["wait_id"] = str(uuid4())
    assert replay(artifact, (events[0], bad)).status == "inconsistent"
    bad.evidence["wait_receipt"]["complete"] = False
    assert replay(artifact, (events[0], bad)).status == "incomplete"


def test_signal_at_exact_deadline_is_inconsistent_even_if_kernel_accepts():
    from firefly_weave.operations.replay import replay

    artifact, events = timer_facts("signal", received_seconds=2)
    assert replay(artifact, events).status == "inconsistent"


def test_cli_local_replay_uses_same_report(worker_runtime_fixture, recorded_events, tmp_path):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli

    artifact_path, events_path = tmp_path / "artifact.json", tmp_path / "events.json"
    artifact_path.write_bytes(worker_runtime_fixture["artifact"].to_bytes())
    events_path.write_text(json.dumps([e.model_dump(mode="json") for e in recorded_events]))
    result = CliRunner().invoke(cli, ["run", "replay", "--artifact", str(artifact_path), "--events", str(events_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "consistent"


def test_malformed_completion_is_not_a_legitimate_kernel_incident(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = worker_runtime_fixture["artifact"]
    first = recorded_events[0]
    prior = transition(RunState(input=3), first, artifact).state
    bad = recorded_events[1].model_copy(deep=True)
    bad.data["output"] = "invalid integer"
    # The kernel legitimately suspends on invalid admission, but that isn't an accepted completion envelope.
    bad.evidence["expected_transition_digest"] = transition_digest(transition(prior, bad, artifact))
    assert replay(artifact, (first, bad)).status == "inconsistent"


def test_legitimate_mapping_incident_retains_verified_prefix():
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.operations import RecordedEvidence, SourceReference
    from firefly_weave.operations.exports import lock_digest, transition_digest
    from firefly_weave.operations.redaction import SafeProjection
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "failure", "version": "1.0.0"},
            "spec": {
                "inputSchema": {},
                "outputSchema": {},
                "steps": [{"id": "bad", "kind": "transform", "value": {"ref": "/input/missing"}}],
                "output": {"ref": "/steps/bad/output"},
            },
        },
        format="object",
        catalog=CatalogSnapshot.empty(),
    ).artifact
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    event = RuntimeEvent(
        id=uuid4(), type="started", sequence=1, timestamp=NOW, data={"admission_policy": "classified-v1"}
    )
    result = transition(RunState(input={}), event, artifact)
    assert result.state.status == "suspended"
    evidence = RecordedEvidence(
        run_id=uuid4(),
        scope=scope,
        artifact_digest=artifact.digest,
        lock_digest=lock_digest(artifact),
        source=SourceReference(scope=scope, version_id=uuid4(), source_hash=artifact.source_hash, format="json"),
        initial_input=SafeProjection(available=True, value={}),
        expected_transition_digest=transition_digest(result),
    )
    report = replay(artifact, (event.model_copy(update={"evidence": evidence.model_dump(mode="json")}),))
    assert report.status == "incomplete" and report.last_verified_sequence == 1
    assert report.final_state.incident == result.state.incident


def test_wire_size_matches_models_aliases_exclusions_and_utf8(recorded_events):
    from firefly_weave.contracts.diagnostics import SourceRange
    from firefly_weave.contracts.operations import RecordedEvidence
    from firefly_weave.operations.exports import bounded_size
    from firefly_weave.runtime.models import RunState

    values = [
        recorded_events[0],
        RecordedEvidence.model_validate_json(json.dumps(recorded_events[0].evidence)),
        RunState(input={'中"\n': [1.5e-7, "😀", None]}),
        SourceRange(file='label\\"😀', line=1, column=2, endLine=3, endColumn=4),
    ]
    for value in values:
        for aliases in (True, False):
            expected = len(
                json.dumps(
                    value.model_dump(mode="json", by_alias=aliases), ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
            assert bounded_size(value, expected, by_alias=aliases) == expected
            assert bounded_size(value, expected - 1, by_alias=aliases) is None
            assert bounded_size(value, expected + 1, by_alias=aliases) == expected


def test_signal_receipt_selection_obeys_recorded_active_node_order():
    from firefly_weave.compiler.ir import SignalNode
    from firefly_weave.contracts.operations import DeadlineFact, RecordedEvidence, SignalFact
    from firefly_weave.operations.replay import EvidenceFailure, _verify_wait
    from firefly_weave.runtime.kernel import workflow

    artifact, events = timer_facts("signal")
    evidence = RecordedEvidence.model_validate_json(json.dumps(events[1].evidence))
    initial = RecordedEvidence.model_validate_json(json.dumps(events[0].evidence))
    node = next(n for n in workflow(artifact).graph.nodes if isinstance(n, SignalNode))
    earlier_node = node.model_copy(update={"id": "first", "name": "first-signal"})
    earlier_deadline = DeadlineFact(
        wait_id=uuid4(), node_id="first", deadline=initial.issued_deadlines[0].deadline, name="first-signal"
    )
    receipt = evidence.wait_receipt.model_copy(
        update={
            "pending_signals": sorted(
                [
                    *evidence.wait_receipt.pending_signals,
                    SignalFact(receipt_id=uuid4(), name="first-signal", accepted_at=NOW),
                ],
                key=lambda s: (s.accepted_at, s.receipt_id),
            )
        }
    )
    evidence = evidence.model_copy(update={"wait_receipt": receipt})
    with pytest.raises(EvidenceFailure) as caught:
        _verify_wait(
            events[1],
            evidence,
            {"first": earlier_deadline, "pause": initial.issued_deadlines[0]},
            {"first": earlier_node, "pause": node},
        )
    assert caught.value.code == "WV-REPLAY-SIGNAL-ORDER"


def recovery_stream(artifact, recorded_events, generation=2):
    from datetime import timedelta

    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    first, last = recorded_events
    prior = transition(RunState(input=3), first.model_copy(update={"evidence": None}), artifact).state
    recovery = last.model_copy(deep=True).model_copy(
        update={
            "id": uuid4(),
            "type": "recovery_scheduled",
            "data": {
                "node_id": "work",
                "generation": 1,
                "code": None,
                "next_attempt_at": (NOW + timedelta(seconds=1)).isoformat(),
            },
        }
    )
    recovery.evidence["task_receipt"]["receipt_id"] = str(recovery.id)
    timing = recovery.evidence["task_receipt"].get("timing")
    if timing:
        timing["kind"] = "recovery_decision"
    change = transition(prior, recovery.model_copy(update={"evidence": None}), artifact)
    recovery.evidence["expected_transition_digest"] = transition_digest(change)
    last = last.model_copy(deep=True).model_copy(update={"sequence": 3, "timestamp": NOW + timedelta(seconds=2)})
    last.data["generation"] = generation
    last.evidence["task_receipt"]["generation"] = generation
    timing = last.evidence["task_receipt"].get("timing")
    if timing:
        timing["checked_at"] = last.timestamp.isoformat()
    last.evidence["expected_transition_digest"] = transition_digest(
        transition(change.state, last.model_copy(update={"evidence": None}), artifact)
    )
    return first, recovery, last


@pytest.mark.parametrize("generation,status,verified", [(1, "inconsistent", 2), (2, "consistent", 3)])
def test_recovery_retires_generation(worker_runtime_fixture, recorded_events, generation, status, verified):
    from firefly_weave.operations.replay import replay

    artifact = worker_runtime_fixture["artifact"]
    report = replay(artifact, recovery_stream(artifact, recorded_events, generation))
    assert (report.status, report.last_verified_sequence) == (status, verified)


def test_missing_task_timing_is_incomplete(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    first, last = recorded_events
    last = last.model_copy(deep=True)
    last.evidence["task_receipt"].pop("timing", None)
    report = replay(worker_runtime_fixture["artifact"], (first, last))
    assert (report.status, report.last_verified_sequence) == ("incomplete", 1)


@pytest.mark.parametrize(
    "checked,deadline,event_time,status",
    [
        (29, 30, 86400, "consistent"),
        (30, 30, 86400, "inconsistent"),
        (31, 30, 86400, "inconsistent"),
        (9, 10, 86400, "consistent"),
        (10, 10, 86400, "inconsistent"),
        (0, 31, 1, "inconsistent"),
        (2, 30, 1, "inconsistent"),
        (-1, 30, 1, "inconsistent"),
    ],
)
def test_task_admission_timing(worker_runtime_fixture, recorded_events, checked, deadline, event_time, status):
    from datetime import timedelta

    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = worker_runtime_fixture["artifact"]
    first, last = recorded_events
    last = last.model_copy(deep=True).model_copy(update={"timestamp": NOW + timedelta(seconds=event_time)})
    last.evidence["task_receipt"]["timing"].update(
        checked_at=(NOW + timedelta(seconds=checked)).isoformat(),
        effective_deadline=(NOW + timedelta(seconds=deadline)).isoformat(),
    )
    prior = transition(RunState(input=3), first.model_copy(update={"evidence": None}), artifact).state
    last.evidence["expected_transition_digest"] = transition_digest(
        transition(prior, last.model_copy(update={"evidence": None}), artifact)
    )
    report = replay(artifact, (first, last))
    assert report.status == status
    assert report.last_verified_sequence == (2 if status == "consistent" else 1)


@pytest.mark.parametrize(
    "next_attempt,event_time,status",
    [
        (0, 0.5, "incomplete"),
        (29, 0, "incomplete"),
        (30, 0, "inconsistent"),
        (31, 0, "inconsistent"),
        (-1, 0, "inconsistent"),
    ],
)
def test_recovery_effective_deadline(worker_runtime_fixture, recorded_events, next_attempt, event_time, status):
    from datetime import timedelta

    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = worker_runtime_fixture["artifact"]
    first, recovery, _ = recovery_stream(artifact, recorded_events)
    recovery = recovery.model_copy(update={"timestamp": NOW + timedelta(seconds=event_time)})
    recovery.data["next_attempt_at"] = (NOW + timedelta(seconds=next_attempt)).isoformat()
    prior = transition(RunState(input=3), first.model_copy(update={"evidence": None}), artifact).state
    recovery.evidence["expected_transition_digest"] = transition_digest(
        transition(prior, recovery.model_copy(update={"evidence": None}), artifact)
    )
    report = replay(artifact, (first, recovery))
    assert (report.status, report.last_verified_sequence) == (status, 2 if status == "incomplete" else 1)


def test_successor_cannot_precede_recovery_eligibility(worker_runtime_fixture, recorded_events):
    from firefly_weave.operations.replay import replay

    artifact = worker_runtime_fixture["artifact"]
    first, recovery, last = recovery_stream(artifact, recorded_events)
    last.evidence["task_receipt"]["timing"]["checked_at"] = NOW.isoformat()
    report = replay(artifact, (first, recovery, last))
    assert (report.status, report.last_verified_sequence) == ("inconsistent", 2)


@pytest.mark.parametrize(
    "route", ["failure_recovery", "manual_retry", "completion_after_failure", "late_recovery_incident"]
)
def test_recorded_attempt_lifecycle(worker_runtime_fixture, recorded_events, route):
    from datetime import timedelta

    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = worker_runtime_fixture["artifact"]
    first, template = recorded_events
    state = transition(RunState(input=3), first.model_copy(update={"evidence": None}), artifact).state
    failure = {
        "node_id": "work",
        "generation": 1,
        "output": {"completion_id": str(uuid4()), "code": "TRANSIENT", "outcome": "failed"},
    }
    specs = [("task_failed", failure, "live_admission", 0)]
    if route == "failure_recovery":
        specs += [
            (
                "recovery_scheduled",
                {
                    "node_id": "work",
                    "generation": 1,
                    "code": None,
                    "next_attempt_at": (NOW + timedelta(seconds=1)).isoformat(),
                },
                "recovery_decision",
                1,
            )
        ]
    elif route == "manual_retry":
        specs += [
            (
                "incident_resolved",
                {
                    "node_id": "work",
                    "generation": 1,
                    "incident_key": "work:1",
                    "receipt_id": str(uuid4()),
                    "kind": "retry_safe",
                },
                None,
                1,
            )
        ]
    elif route == "late_recovery_incident":
        specs += [
            (
                "incident_opened",
                {"node_id": "work", "generation": 1, "code": "WV-TASK-RETRIES-EXHAUSTED", "next_attempt_at": None},
                "recovery_decision",
                31,
            )
        ]
    if route != "late_recovery_incident":
        specs += [
            (
                "task_completed",
                {"node_id": "work", "generation": 1 if route == "completion_after_failure" else 2, "output": 9},
                "live_admission",
                2,
            )
        ]
    events = [first]
    for sequence, (kind, data, timing_kind, seconds) in enumerate(specs, 2):
        event = template.model_copy(deep=True).model_copy(
            update={
                "id": uuid4(),
                "sequence": sequence,
                "timestamp": NOW + timedelta(seconds=seconds),
                "type": kind,
                "data": data,
            }
        )
        if timing_kind is None:
            event.evidence["task_receipt"] = None
        else:
            receipt = event.evidence["task_receipt"]
            receipt.update(receipt_id=str(event.id), generation=data["generation"])
            receipt["timing"].update(kind=timing_kind, checked_at=event.timestamp.isoformat())
        change = transition(state, event.model_copy(update={"evidence": None}), artifact)
        event.evidence["expected_transition_digest"] = transition_digest(change)
        state = change.state
        events.append(event)
    report = replay(artifact, tuple(events))
    if route == "completion_after_failure":
        assert (report.status, report.last_verified_sequence) == ("inconsistent", 2)
    elif route == "late_recovery_incident":
        assert (report.status, report.last_verified_sequence) == ("incomplete", 3)
    else:
        assert (report.status, report.last_verified_sequence) == ("consistent", 4), report
