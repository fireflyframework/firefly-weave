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

"""Pure run transitions preserve deterministic events, barriers, and completion."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot


def artifact(steps=None, output=None, input_schema=None):
    result = compile_source(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "test", "version": "1.0.0"},
                "spec": {
                    "inputSchema": input_schema or {},
                    "outputSchema": {},
                    "steps": steps or [{"id": "copy", "kind": "transform", "value": {"ref": "/input"}}],
                    "output": output or {"ref": "/steps/copy/output"},
                },
            }
        ),
        format="json",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok, result.to_bytes()
    return result.artifact


def event(kind="started", sequence=1, data=None):
    from firefly_weave.runtime.models import RuntimeEvent

    return RuntimeEvent(
        id=uuid4(), type=kind, data=data or {}, timestamp=datetime(2026, 9, 29, tzinfo=UTC), sequence=sequence
    )


def test_pure_transition_copies_input_and_completes():
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    initial = RunState(input={"value": 42})
    result = transition(initial, event(), artifact())
    assert result.state.status == "succeeded"
    assert result.state.output == {"value": 42}
    assert initial.status == "queued"
    assert result.state.accepted_sequence == 1
    assert result.steps


def test_missing_expression_suspends_without_commands():
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    flow = artifact([{"id": "copy", "kind": "transform", "value": {"ref": "/input/missing"}}])
    result = transition(RunState(input={}), event(), flow)
    assert result.state.status == "suspended"
    assert result.state.incident == "WV-EXPR-MISSING"
    assert not result.commands


def test_signal_completion_and_schema_guard():
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    flow = artifact(
        [
            {
                "id": "approval",
                "kind": "signal",
                "name": "approved",
                "timeoutSeconds": 30,
                "payloadSchema": {"type": "boolean"},
            }
        ],
        {"ref": "/steps/approval/output"},
    )
    waiting = transition(RunState(input={}), event(), flow)
    assert waiting.state.status == "waiting"
    assert len(waiting.commands) == 1
    failed = transition(waiting.state, event("signal_received", 2, {"node_id": "approval", "output": "bad"}), flow)
    assert failed.state.status == "suspended"
    done = transition(waiting.state, event("signal_received", 2, {"node_id": "approval", "output": True}), flow)
    assert done.state.output is True
    assert done.state.status == "succeeded"


def test_sequence_and_terminal_events_rejected():
    from firefly_weave.runtime.kernel import KernelError, transition
    from firefly_weave.runtime.models import RunState

    with pytest.raises(KernelError):
        transition(RunState(), event(sequence=2), artifact())
    done = transition(RunState(), event(), artifact())
    with pytest.raises(KernelError):
        transition(done.state, event(sequence=2), artifact())


def test_action_intent_identity_and_completion(worker_runtime_fixture):
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    flow = worker_runtime_fixture["artifact"]
    waiting = transition(RunState(input=12), event(), flow)
    command = waiting.commands[0]
    assert command.action_reference == "echo-action@2.0.0"
    assert (command.task_type, command.task_version) == ("echo", "1.2.0")
    assert command.action_digest == next(d["digest"] for d in flow.executable["dependencies"] if d["kind"] == "Action")
    assert command.input == 12
    done = transition(waiting.state, event("task_completed", 2, {"node_id": "work", "output": 13}), flow)
    assert done.state.status == "succeeded"
    assert done.state.output == 13
    failed = transition(waiting.state, event("task_completed", 2, {"node_id": "work", "output": "invalid"}), flow)
    assert failed.state.status == "suspended"
    assert failed.commands == []


def test_historical_payloads_have_independent_limits():
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    flow = artifact(
        [{"id": name, "kind": "transform", "value": {"ref": "/input"}} for name in ("first", "second")],
        {"literal": True},
    )
    result = transition(RunState(input="x" * 600_000), event(), flow)
    assert result.state.status == "succeeded"
    assert len(result.state.steps["first"]["output"]) == 600_000
    assert len(result.state.steps["second"]["output"]) == 600_000


def test_resource_exhaustion_is_deterministic_incident():
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    flow = artifact([{"id": "copy", "kind": "transform", "value": {"array": [{"ref": "/input"}, {"ref": "/input"}]}}])
    result = transition(RunState(input="x" * 600_000), event(), flow)
    assert result.state.incident == "WV-EXPR-RESOURCE_LIMIT"
    assert result.state.status == "suspended"
    assert not result.commands
    assert result.state.steps == {}


@pytest.mark.parametrize(
    "duration", [9_007_199_254_740_991, 1_000_000_000_000], ids=["timedelta-overflow", "datetime-overflow"]
)
@pytest.mark.parametrize("deadline_kind", ["workflow", "action", "signal"])
def test_deadline_overflow_suspends_without_partial_commands(duration, deadline_kind, worker_runtime_fixture):
    from datetime import timedelta

    from firefly_weave.contracts.definitions import load_definition
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import Deadline, RunState

    catalog = CatalogSnapshot.empty()
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "deadline", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "timeoutSeconds": 30,
            "steps": [{"id": "copy", "kind": "transform", "value": {"ref": "/input"}}],
            "output": {"literal": True},
        },
    }
    if deadline_kind == "workflow":
        document["spec"]["timeoutSeconds"] = duration
    elif deadline_kind == "signal":
        document["spec"]["steps"].append(
            {"id": "signal", "kind": "signal", "name": "signal", "timeoutSeconds": duration, "payloadSchema": {}}
        )
    else:
        action = worker_runtime_fixture["action"].model_dump(by_alias=True)
        action["spec"]["timeoutSeconds"] = duration
        task = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
        task["timeoutSeconds"] = duration
        catalog = CatalogSnapshot.from_definitions([load_definition(action)], tasks=[task])
        document["spec"]["steps"].append(
            {"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
        )
    compiled = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert compiled.ok, compiled.to_bytes()
    initial = RunState(input=7)
    result = transition(initial, event(), compiled.artifact)
    assert result.state.status == "suspended"
    assert result.state.incident == "WV-RUNTIME-DEADLINE-RANGE"
    assert result.state.accepted_sequence == 1
    expected = (
        []
        if deadline_kind == "workflow"
        else [Deadline(node_id="@run", deadline=event().timestamp + timedelta(seconds=30))]
    )
    assert result.commands == expected
    assert result.steps == []
    assert result.state.steps == {}
    assert result.state.active == []
    assert result.state.waits == {}
    assert initial.status == "queued"


def test_task_failure_preserves_recovery_state(worker_runtime_fixture):
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = worker_runtime_fixture["artifact"]
    started = transition(
        RunState(input=3), RuntimeEvent(id=uuid4(), type="started", timestamp=datetime.now(UTC), sequence=1), artifact
    )
    failed = transition(
        started.state,
        RuntimeEvent(
            id=uuid4(),
            type="task_failed",
            data={"node_id": "work", "output": {"code": "LOST", "outcome": "unknown"}},
            timestamp=datetime.now(UTC),
            sequence=2,
        ),
        artifact,
    )
    assert failed.state.status == "suspended" and failed.state.incident == "WV-TASK-FAILED"
    assert failed.state.active == ["work"] and failed.state.input == 3 and failed.state.accepted_sequence == 2
    assert failed.commands == [] and failed.steps == []


@pytest.mark.parametrize(
    "event_type,status,code",
    [
        ("timed_out", "timed_out", None),
        ("incident_opened", "suspended", "WV-TASK-AMBIGUOUS"),
        ("recovery_scheduled", "waiting", None),
    ],
)
def test_internal_recovery_facts_are_deterministic(worker_runtime_fixture, event_type, status, code):
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = worker_runtime_fixture["artifact"]
    now = datetime(2026, 9, 30, tzinfo=UTC)
    started = transition(
        RunState(input=3), RuntimeEvent(id=uuid4(), type="started", timestamp=now, sequence=1), artifact
    )
    accepted = RuntimeEvent(
        id=uuid4(),
        type=event_type,
        timestamp=now,
        sequence=2,
        data={"node_id": "work", "deadline": now.isoformat(), "code": code, "next_attempt_at": now.isoformat()},
    )
    actual = transition(started.state, accepted, artifact)
    replay = transition(started.state, RuntimeEvent.model_validate_json(accepted.model_dump_json()), artifact)
    assert replay == actual
    assert actual.state.status == status and actual.state.incident == code
    assert actual.state.accepted_sequence == 2
    assert actual.commands == []
