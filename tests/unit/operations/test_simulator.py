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

"""The debugger steps the production reducer and never acquires an executor."""

import json
from datetime import UTC, datetime

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def flow(steps=None, output=None, **spec):
    result = compile_source(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "debug", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {},
                    "outputSchema": {},
                    "steps": steps or [{"id": "copy", "kind": "transform", "value": {"ref": "/input"}}],
                    "output": output or {"ref": "/steps/copy/output"},
                    **spec,
                },
            }
        ),
        format="json",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok, result.to_bytes()
    return result.artifact


def test_missing_mock_fails_closed(worker_runtime_fixture):
    from firefly_weave.operations.debug.simulator import Simulator

    simulator = Simulator(worker_runtime_fixture["artifact"], mocks={}, input=3, now=NOW)
    view = simulator.continue_until_breakpoint()
    assert view.status == "failed"
    assert view.diagnostics[0].code == "WV-DEBUG-MISSING_MOCK"


def test_each_boundary_serializes_and_matches_production():
    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = flow()
    simulator = Simulator(artifact, mocks={}, input={"x": 1}, now=NOW)
    assert simulator.inspect().events == []
    assert simulator.inspect().variables["steps"] == {}
    kinds = []
    while simulator.inspect().status not in {"succeeded", "failed"}:
        view = simulator.next()
        kinds.append(view.boundary.kind)
        simulator = Simulator.restore(simulator.serialize())
    assert kinds == ["event", "node", "node", "node"]
    assert view.boundary.node_kind == "end"
    assert len(view.events) == 1
    production = transition(RunState(input={"x": 1}), view.events[0], artifact)
    assert view.variables == production.state.model_dump(mode="json")


def test_breakpoint_resume_and_action_completion_boundary(worker_runtime_fixture):
    from firefly_weave.operations.debug.simulator import Simulator

    simulator = Simulator(worker_runtime_fixture["artifact"], mocks={"action:echo-action@2.0.0": 9}, input=3, now=NOW)
    simulator.set_breakpoints({"work"})
    paused = simulator.continue_until_breakpoint()
    assert paused.status == "paused" and paused.selected_node == "work"
    assert paused.active_nodes == [] and len(paused.events) == 1
    action = simulator.next()
    assert action.boundary.node_id == "work" and action.active_nodes == ["work"]
    completion = simulator.next()
    assert completion.boundary.kind == "event" and len(completion.events) == 2
    assert simulator.continue_until_breakpoint().variables["output"] == 9


@pytest.mark.parametrize(
    "mocks", [{"echo-action@2.0.0": 1}, {"node:nope": 1}, {"action:absent@1.0.0": 1}, {"node:work": "bad"}]
)
def test_invalid_mocks_are_rejected_without_values(worker_runtime_fixture, mocks):
    from firefly_weave.operations.debug.simulator import Simulator

    with pytest.raises(ValueError, match="WV-DEBUG-MOCK"):
        Simulator(worker_runtime_fixture["artifact"], mocks=mocks, input=3, now=NOW)


def test_virtual_wait_signal_before_activation_and_strict_deadline():
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(
        [
            {"id": "timer", "kind": "wait", "durationSeconds": 10},
            {
                "id": "approval",
                "kind": "signal",
                "name": "approved",
                "timeoutSeconds": 5,
                "payloadSchema": {"type": "boolean"},
            },
        ],
        {"ref": "/steps/approval/output"},
    )
    simulator = Simulator(artifact, mocks={}, input={}, now=NOW)
    assert simulator.signal("approved", True).events == []
    waiting = simulator.continue_until_breakpoint()
    assert waiting.status == "waiting" and waiting.active_nodes == ["timer"]
    assert simulator.advance_time(9).events == waiting.events
    assert simulator.continue_until_breakpoint().active_nodes == ["timer"]
    simulator.advance_time(1)
    assert simulator.continue_until_breakpoint().variables["output"] is True


def test_driver_rolls_back_partial_steps_and_retains_overall_deadline():
    from uuid import UUID

    from firefly_weave.runtime.kernel import KernelCursor, advance, begin
    from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent

    artifact = flow(
        [
            {"id": "partial", "kind": "transform", "value": {"literal": 7}},
            {"id": "broken", "kind": "transform", "value": {"ref": "/input/missing"}},
        ],
        {"literal": True},
        timeoutSeconds=30,
    )
    cursor = begin(
        RunState(input={}), RuntimeEvent(id=UUID(int=1), type="started", timestamp=NOW, sequence=1), artifact
    )
    while not cursor.done:
        cursor = advance(KernelCursor.model_validate_json(cursor.model_dump_json()), artifact)
    assert cursor.result.state.status == "suspended"
    assert cursor.result.state.steps == {} and cursor.result.steps == []
    assert [x.node_id for x in cursor.result.commands if isinstance(x, Deadline)] == ["@run"]


def test_offline_network_and_connector_factories_never_used(monkeypatch, worker_runtime_fixture):
    import socket

    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import ScopedSecrets
    from firefly_weave.operations.debug.simulator import Simulator

    def forbidden(*args, **kwargs):
        raise AssertionError("Simulation acquired infrastructure")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(ConnectorRegistry, "__init__", forbidden)
    monkeypatch.setattr(ScopedSecrets, "__init__", forbidden)
    simulator = Simulator(worker_runtime_fixture["artifact"], mocks={"node:work": 8}, input=3, now=NOW)
    assert simulator.continue_until_breakpoint().variables["output"] == 8


def test_nested_branch_breakpoint_resume_roundtrip():
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(
        [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "left": {
                        "steps": [
                            {
                                "id": "choice",
                                "kind": "switch",
                                "cases": [
                                    {
                                        "when": {"literal": True},
                                        "steps": [{"id": "inner", "kind": "transform", "value": {"literal": 7}}],
                                        "output": {"ref": "/steps/inner/output"},
                                    }
                                ],
                                "default": {
                                    "steps": [{"id": "fallback", "kind": "transform", "value": {"literal": 0}}],
                                    "output": {"literal": 0},
                                },
                            }
                        ],
                        "output": {"ref": "/steps/choice/output"},
                    },
                    "right": {
                        "steps": [{"id": "other", "kind": "transform", "value": {"literal": 9}}],
                        "output": {"ref": "/steps/other/output"},
                    },
                },
            }
        ],
        {"ref": "/steps/fork/output"},
    )
    simulator = Simulator(artifact, mocks={}, input={}, now=NOW)
    inner = next(n.id for n in simulator.ir.graph.nodes if n.id.endswith("inner"))
    simulator.set_breakpoints({inner})
    paused = simulator.continue_until_breakpoint()
    assert paused.selected_node == inner and paused.status == "paused"
    assert all(inner not in b.get("steps", {}) for b in paused.variables["branches"].values())
    restored = Simulator.restore(simulator.serialize())
    done = restored.continue_until_breakpoint()
    assert done == simulator.continue_until_breakpoint()
    assert done.variables["output"] == {"left": 7, "right": 9}
    assert {b.node_kind for b in done.boundaries} >= {"start", "end", "join", "branch-output"}


@pytest.mark.parametrize("seconds", [-1, True, 1.5, 10**20])
def test_bad_time_is_atomic(seconds):
    from firefly_weave.operations.debug.simulator import Simulator

    simulator = Simulator(flow(), mocks={}, input={}, now=NOW)
    before = simulator.serialize()
    with pytest.raises(ValueError, match="WV-DEBUG-"):
        simulator.advance_time(seconds)
    assert simulator.serialize() == before


def test_explicit_time_only_and_equal_signal_loses():
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(
        [
            {
                "id": "approval",
                "kind": "signal",
                "name": "approved",
                "timeoutSeconds": 5,
                "payloadSchema": {"type": "boolean"},
            }
        ],
        {"ref": "/steps/approval/output"},
    )
    simulator = Simulator(artifact, mocks={}, input={}, now=NOW)
    simulator.continue_until_breakpoint()
    before = simulator.inspect().events
    assert simulator.advance_time(5).events == before
    assert simulator.signal("approved", True).events == before
    assert simulator.continue_until_breakpoint().status == "timed_out"
    assert simulator.inspect().now == NOW.replace(second=5)


def test_overall_timeout_precedes_earlier_receipt():
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(
        [{"id": "approval", "kind": "signal", "name": "approved", "timeoutSeconds": 50, "payloadSchema": {}}],
        {"literal": True},
        timeoutSeconds=5,
    )
    simulator = Simulator(artifact, mocks={}, input={}, now=NOW)
    simulator.signal("approved", True)
    simulator.advance_time(5)
    simulator.continue_until_breakpoint()
    # The run starts at its synthetic start timestamp, not at wall/command time.
    assert simulator.inspect().status == "timed_out"
    assert simulator.inspect().events[-1].data["node_id"] == "@run"


def test_secret_input_and_mock_rejected_and_computed_output_never_inspected():
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(inputSchema={"type": "object", "properties": {"secret": {"x-secret": True}}})
    with pytest.raises(ValueError, match="SECRET"):
        Simulator(artifact, mocks={}, input={"secret": "private"}, now=NOW)
    artifact = flow(outputSchema={"properties": {"value": {"x-secret": True}}}, timeoutSeconds=20)
    simulator = Simulator(artifact, mocks={}, input={"value": "unclassified-at-ingress"}, now=NOW)
    view = simulator.continue_until_breakpoint()
    assert view.status == "suspended" and view.variables["steps"] == {}
    assert view.variables["output"] is None
    assert view.variables["incident"] == "WV-SCHEMA-SECRET_VALUE"


def test_each_operator_budget_is_enforced_atomically():
    from dataclasses import replace

    from firefly_weave.operations.debug.models import DebugLimits
    from firefly_weave.operations.debug.simulator import Simulator

    for field, value, operation in [
        ("node_reductions", 0, lambda s: s.continue_until_breakpoint()),
        ("commands", 0, lambda s: s.next()),
        ("virtual_seconds", 0, lambda s: s.advance_time(1)),
    ]:
        simulator = Simulator(flow(), mocks={}, input={}, now=NOW, limits=replace(DebugLimits(), **{field: value}))
        before = simulator.serialize()
        with pytest.raises(ValueError, match="WV-DEBUG-LIMIT"):
            operation(simulator)
        assert simulator.serialize() == before
    with pytest.raises(ValueError, match="WV-DEBUG-LIMIT"):
        Simulator(flow(), mocks={}, input={}, now=NOW, limits=replace(DebugLimits(), session_bytes=100))


def test_node_override_wins_and_unknown_breakpoint_rejected(worker_runtime_fixture):
    from firefly_weave.operations.debug.simulator import Simulator

    simulator = Simulator(
        worker_runtime_fixture["artifact"], mocks={"action:echo-action@2.0.0": 9, "node:work": 4}, input=3, now=NOW
    )
    with pytest.raises(ValueError, match="WV-DEBUG-BREAKPOINT"):
        simulator.set_breakpoints({"unknown"})
    assert simulator.continue_until_breakpoint().variables["output"] == 4


def test_cli_parity(tmp_path, worker_runtime_fixture):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli
    from firefly_weave.operations.debug.simulator import Simulator

    request = {
        "artifact": json.loads(worker_runtime_fixture["artifact"].to_bytes()),
        "mocks": {"node:work": 7},
        "input": 3,
        "now": NOW.isoformat(),
    }
    path = tmp_path / "simulation.json"
    path.write_text(json.dumps(request))
    result = CliRunner().invoke(cli, ["workflow", "simulate", str(path), "--output", "json"])
    assert result.exit_code == 0, result.output
    simulator = Simulator(worker_runtime_fixture["artifact"], mocks=request["mocks"], input=3, now=NOW)
    assert json.loads(result.output) == simulator.continue_until_breakpoint().model_dump(mode="json")


def test_list_mock_is_one_json_value(worker_runtime_fixture):
    from firefly_weave.contracts.definitions import load_definition
    from firefly_weave.operations.debug.simulator import Simulator

    fixture = worker_runtime_fixture
    action = fixture["action"].model_dump(by_alias=True)
    action["spec"]["outputSchema"] = {"type": "array", "items": {"type": "integer"}}
    task = fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    task["outputSchema"] = action["spec"]["outputSchema"]
    document = json.loads(fixture["source"])
    document["spec"]["outputSchema"] = action["spec"]["outputSchema"]
    compiled = compile_source(
        json.dumps(document),
        format="json",
        catalog=CatalogSnapshot.from_definitions([load_definition(action)], tasks=[task]),
    )
    assert compiled.ok, compiled.to_bytes()
    simulator = Simulator(compiled.artifact, mocks={"node:work": [1, 2]}, input=3, now=NOW)
    view = simulator.continue_until_breakpoint()
    assert view.variables["output"] == [1, 2] and len(view.events) == 2


def test_pending_receipt_fact_continue_limits_and_time_validation():
    from dataclasses import replace

    from firefly_weave.operations.debug.models import DebugLimits
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow(
        [{"id": "wait", "kind": "signal", "name": "go", "timeoutSeconds": 4, "payloadSchema": {}}], {"literal": True}
    )
    for field in ("facts", "pending_signals"):
        limits = replace(DebugLimits(), **{field: 1})
        simulator = Simulator(artifact, mocks={}, input={}, now=NOW, limits=limits)
        if field == "pending_signals":
            simulator.signal("go", True)
        before = simulator.serialize()
        with pytest.raises(ValueError, match="WV-DEBUG-LIMIT"):
            simulator.signal("go", True)
        assert simulator.serialize() == before
    simulator = Simulator(flow(), mocks={}, input={}, now=NOW, limits=replace(DebugLimits(), continue_boundaries=1))
    assert len(simulator.continue_until_breakpoint().boundaries) == 1
    assert len(simulator.continue_until_breakpoint().boundaries) == 2
    with pytest.raises(ValueError, match="WV-DEBUG-TIME"):
        Simulator(flow(), mocks={}, input={}, now=NOW.replace(tzinfo=None))
    simulator = Simulator(flow(), mocks={}, input={}, now=datetime.max.replace(tzinfo=UTC))
    with pytest.raises(ValueError, match="WV-DEBUG-TIME"):
        simulator.advance_time(1)


def test_suspended_driver_restores_deferred_duration_without_new_sequence(worker_runtime_fixture):
    from uuid import UUID

    from firefly_weave.runtime.kernel import KernelCursor, advance, begin, transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = worker_runtime_fixture["artifact"]
    started = RuntimeEvent(id=UUID(int=1), type="started", timestamp=NOW, sequence=1)
    waiting = transition(RunState(input=3), started, artifact).state
    failed = RuntimeEvent(id=UUID(int=2), type="task_failed", timestamp=NOW, sequence=2, data={"node_id": "work"})
    suspended = transition(waiting, failed, artifact).state
    completed = RuntimeEvent(
        id=UUID(int=3), type="task_completed", timestamp=NOW, sequence=3, data={"node_id": "work", "output": 8}
    )
    cursor = advance(begin(suspended, completed, artifact), artifact)
    cursor = KernelCursor.model_validate_json(cursor.model_dump_json())
    assert cursor.done and cursor.result.state.status == "suspended"
    assert cursor.result.state.accepted_sequence == 3
    assert len(cursor.result.state.deferred_results) == 1 and cursor.result.steps == []


def test_debug_public_contracts_export_offline():
    from firefly_weave.contracts.schema_export import contract_models

    assert {"debug-create", "debug-command", "debug-view", "debug-session"} <= contract_models().keys()


def test_classified_mock_rejected_before_session_storage(worker_runtime_fixture):
    from firefly_weave.contracts.definitions import load_definition
    from firefly_weave.operations.debug.simulator import Simulator

    fixture = worker_runtime_fixture
    action = fixture["action"].model_dump(by_alias=True)
    schema = {"type": "object", "properties": {"private": {"x-secret": True}}}
    action["spec"]["outputSchema"] = schema
    task = fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    task["outputSchema"] = schema
    document = json.loads(fixture["source"])
    document["spec"]["outputSchema"] = schema
    compiled = compile_source(
        json.dumps(document),
        format="json",
        catalog=CatalogSnapshot.from_definitions([load_definition(action)], tasks=[task]),
    )
    assert compiled.ok, compiled.to_bytes()
    with pytest.raises(ValueError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(compiled.artifact, mocks={"node:work": {"private": "never-retained"}}, input=3, now=NOW)
    assert (
        Simulator(compiled.artifact, mocks={"node:work": {}}, input=3, now=NOW)
        .continue_until_breakpoint()
        .variables["output"]
        == {}
    )


def test_unicode_cli_library_and_restore_share_trace(tmp_path):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli
    from firefly_weave.operations.debug.simulator import Simulator

    artifact = flow()
    request = {"artifact": json.loads(artifact.to_bytes()), "mocks": {}, "input": "🙂é", "now": NOW.isoformat()}
    path = tmp_path / "unicode.json"
    path.write_text(json.dumps(request))
    simulator = Simulator(artifact, mocks={}, input=request["input"], now=NOW)
    simulator.next()
    serialized = simulator.serialize()
    assert "🙂é" in serialized
    simulator = Simulator.restore(serialized)
    expected = simulator.continue_until_breakpoint().model_dump(mode="json")
    result = CliRunner().invoke(cli, ["workflow", "simulate", str(path), "--output", "json"])
    assert result.exit_code == 0 and json.loads(result.output) == expected


def test_production_drain_prepares_once_without_copying_each_growing_transition(monkeypatch):
    from uuid import UUID

    from firefly_weave.runtime import kernel
    from firefly_weave.runtime.models import RunState, RuntimeEvent, Transition

    artifact = flow(
        [{"id": f"copy{i}", "kind": "transform", "value": {"literal": 1}} for i in range(1000)],
        {"literal": True},
    )
    counts = {"preparations": 0, "deep_transitions": 0}
    real_workflow, real_copy = kernel.workflow, Transition.model_copy

    def measured_workflow(value):
        counts["preparations"] += 1
        return real_workflow(value)

    def measured_copy(self, **kwargs):
        if kwargs.get("deep"):
            counts["deep_transitions"] += 1
        return real_copy(self, **kwargs)

    monkeypatch.setattr(kernel, "workflow", measured_workflow)
    monkeypatch.setattr(Transition, "model_copy", measured_copy)
    initial = RunState(input={})
    event = RuntimeEvent(id=UUID(int=1), type="started", sequence=1, timestamp=NOW)
    result = kernel.transition(initial, event, artifact)
    assert result.state.status == "succeeded" and result.state.output is True
    assert len(result.steps) == 1000 and initial.steps == {} and initial.accepted_sequence == 0
    assert counts == {"preparations": 1, "deep_transitions": 0}


def test_simulator_command_reuses_preparation_and_keeps_its_entry_checkpoint(monkeypatch):
    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.runtime import kernel
    from firefly_weave.runtime.models import Transition

    artifact = flow(
        [{"id": f"copy{i}", "kind": "transform", "value": {"literal": i}} for i in range(10)],
        {"ref": "/steps/copy9/output"},
    )
    simulator = Simulator(artifact, mocks={}, input={}, now=NOW)
    simulator.set_breakpoints({"copy5"})
    simulator.continue_until_breakpoint()
    simulator = Simulator.restore(simulator.serialize())
    previous = simulator.data
    serialized = previous.model_dump_json()
    counts = {"preparations": 0, "deep_transitions": 0}
    real_workflow, real_copy = kernel.workflow, Transition.model_copy

    def measured_workflow(value):
        counts["preparations"] += 1
        return real_workflow(value)

    def measured_copy(self, **kwargs):
        if kwargs.get("deep"):
            counts["deep_transitions"] += 1
        return real_copy(self, **kwargs)

    monkeypatch.setattr(kernel, "workflow", measured_workflow)
    monkeypatch.setattr(Transition, "model_copy", measured_copy)
    view = simulator.continue_until_breakpoint()
    assert view.status == "succeeded" and view.variables["output"] == 9
    assert previous.model_dump_json() == serialized
    assert counts == {"preparations": 0, "deep_transitions": 0}


def test_public_cursor_advance_does_not_mutate_prior_boundaries_or_origin():
    from uuid import UUID

    from firefly_weave.runtime.kernel import advance, begin
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = flow(
        [
            {"id": "copy", "kind": "transform", "value": {"ref": "/input"}},
            {"id": "bad", "kind": "transform", "value": {"ref": "/input/missing"}},
        ],
        {"literal": True},
        timeoutSeconds=30,
    )
    initial = RunState(input={"payload": [1]})
    event = RuntimeEvent(id=UUID(int=1), type="started", sequence=1, timestamp=NOW)
    cursor = begin(initial, event, artifact)
    history = []
    while not cursor.done:
        snapshot = cursor.model_dump_json()
        history.append((cursor, snapshot))
        cursor = advance(cursor, artifact)
        assert all(prior.model_dump_json() == serialized for prior, serialized in history)
    assert initial.model_dump_json() == RunState(input={"payload": [1]}).model_dump_json()
    assert cursor.result.state.status == "suspended" and cursor.result.state.steps == {}
    assert [command.node_id for command in cursor.result.commands] == ["@run"]
