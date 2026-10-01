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

"""Duration waits resume through accepted timers under the runtime state machine."""

from datetime import timedelta

from test_kernel import artifact, event

from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState


def test_duration_wait_continues_only_at_issued_deadline_and_replays_exactly():
    flow = artifact([{"id": "pause", "kind": "wait", "durationSeconds": 10}], {"literal": "done"})
    started = event()
    waiting = transition(RunState(input={}), started, flow)
    assert waiting.state.status == "waiting"
    due = started.timestamp + timedelta(seconds=10)
    assert waiting.state.waits == {"pause": due}
    elapsed = event("wait_elapsed", 2, {"node_id": "pause", "deadline": due.isoformat(), "wait_id": "durable-id"})
    early = transition(waiting.state, elapsed, flow)
    assert early.state.status == "suspended"
    elapsed = elapsed.model_copy(update={"timestamp": due})
    done = transition(waiting.state, elapsed, flow)
    assert done.state.status == "succeeded"
    assert done.state.output == "done"
    assert transition(waiting.state, elapsed, flow) == done


def test_duration_overflow_is_incident_without_deadline():
    flow = artifact([{"id": "pause", "kind": "wait", "durationSeconds": 9000000000000000}], {"literal": None})
    result = transition(RunState(), event(), flow)
    assert result.state.status == "suspended"
    assert result.state.incident == "WV-RUNTIME-DEADLINE-RANGE"
    assert not result.commands


def test_nested_duration_wait_respects_capacity_and_suspension_barrier():
    from firefly_weave.runtime.models import IncidentState

    def branch(name):
        return {"steps": [{"id": name, "kind": "wait", "durationSeconds": 10}], "output": {"literal": name}}

    flow = artifact(
        [{"id": "fork", "kind": "parallel", "concurrency": 1, "branches": {"a": branch("a"), "b": branch("b")}}],
        {"ref": "/steps/fork/output"},
    )
    waiting = transition(RunState(), event(), flow).state
    assert waiting.active == ["a"]
    waiting = waiting.model_copy(update={"status": "suspended", "incident": "TEST"})
    waiting.incidents["a:0"] = IncidentState(node_id="a", generation=0, code="TEST")
    due = waiting.waits["a"]
    elapsed = event(
        "wait_elapsed", 2, {"node_id": "a", "deadline": due.isoformat(), "wait_id": "durable-id"}
    ).model_copy(update={"timestamp": due})
    deferred = transition(waiting, elapsed, flow).state
    assert deferred.status == "suspended" and deferred.active == ["a"]
    assert deferred.deferred_results == [elapsed]
    resolved = transition(
        deferred,
        event("incident_resolved", 3, {"incident_key": "a:0", "kind": "retry_safe"}).model_copy(
            update={"timestamp": due}
        ),
        flow,
    ).state
    assert resolved.active == ["b"]
    assert resolved.waits["b"] == due + timedelta(seconds=10)
    done = transition(
        resolved,
        event(
            "wait_elapsed", 4, {"node_id": "b", "deadline": resolved.waits["b"].isoformat(), "wait_id": "second"}
        ).model_copy(update={"timestamp": resolved.waits["b"]}),
        flow,
    ).state
    assert done.status == "succeeded" and done.output == {"a": "a", "b": "b"}


def test_duration_overflow_preserves_only_valid_overall_deadline():
    import json

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.runtime.models import Deadline

    result = compile_source(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "overflow", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {},
                    "outputSchema": {},
                    "timeoutSeconds": 1,
                    "steps": [
                        {"id": "partial", "kind": "transform", "value": {"literal": 7}},
                        {
                            "id": "fork",
                            "kind": "parallel",
                            "concurrency": 2,
                            "branches": {
                                "valid": {
                                    "steps": [{"id": "valid-wait", "kind": "wait", "durationSeconds": 1}],
                                    "output": {"literal": 1},
                                },
                                "overflow": {
                                    "steps": [
                                        {"id": "overflow-wait", "kind": "wait", "durationSeconds": 9000000000000000}
                                    ],
                                    "output": {"literal": 2},
                                },
                            },
                        },
                    ],
                    "output": {"literal": None},
                },
            }
        ),
        format="json",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok, result.to_bytes()
    started = event()
    suspended = transition(RunState(), started, result.artifact)
    assert suspended.state.status == "suspended"
    assert suspended.state.incident == "WV-RUNTIME-DEADLINE-RANGE"
    assert suspended.commands == [Deadline(node_id="@run", deadline=started.timestamp + timedelta(seconds=1))]
    assert suspended.steps == [] and suspended.state.steps == {}
    assert suspended.state.active == [] and suspended.state.waits == {}
    assert suspended.state.branches == {} and suspended.state.joins == {}
