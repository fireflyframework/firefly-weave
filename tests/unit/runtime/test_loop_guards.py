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

"""Loop safety lines: output budget accounting, runtime guards, defensive state checks and intent weights."""

import json

import pytest
from test_loop_runtime import NOW, Run, compiled, each, send

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest
from firefly_weave.compiler.ir import WorkflowIR
from firefly_weave.runtime import kernel
from firefly_weave.runtime.capacity import MAX_INTENTS
from firefly_weave.runtime.kernel import prepare
from firefly_weave.runtime.models import ControlCommand, HumanTaskIntent, LoopState, RunState


def with_guard(flow: CompiledArtifact, purpose: str, schema: dict) -> CompiledArtifact:
    """The same artifact with the schema of its ``purpose`` guard replaced, sealed with a matching digest."""
    envelope = json.loads(flow.to_bytes())
    executable = envelope["executable"]
    reference = canonical_digest(schema)
    executable["schemas"][reference] = schema
    (guard,) = [item for item in executable["guards"] if item["purpose"] == purpose]
    guard["schemaRef"] = reference
    envelope["digest"] = canonical_digest(WorkflowIR.model_validate(executable).model_dump(by_alias=True))
    return CompiledArtifact(canonical_bytes(envelope))


def outcome(run: Run) -> tuple[str, str | None]:
    return run.state.status, run.state.incident


def identity(**settings) -> CompiledArtifact:
    return compiled([each("each", [], {"ref": "/item"}, **settings)], {"ref": "/steps/each/output"})


@pytest.mark.parametrize(
    ("collect", "items"),
    [("all", ["ab", None]), ("nonNull", ["ab", None, None, "cd"])],
)
def test_collected_outputs_fit_the_budget_exactly_at_the_size_of_the_output_list(monkeypatch, collect, items):
    # ["ab",null] and ["ab","cd"] are both 11 bytes: brackets, elements and separators, without skipped nulls.
    flow = identity(collect=collect)
    monkeypatch.setattr(kernel, "LOOP_OUTPUT_BYTES", 11)
    run = Run(flow, items)
    run.start()
    assert outcome(run) == ("succeeded", None)
    assert len(json.dumps(run.state.output, separators=(",", ":"))) == 11
    monkeypatch.setattr(kernel, "LOOP_OUTPUT_BYTES", 10)
    run = Run(flow, items)
    run.start()
    assert outcome(run) == ("suspended", "WV-RUNTIME-LOOP-OUTPUT")


def test_the_items_guard_the_artifact_carries_is_applied():
    flow = with_guard(identity(), "loop_items", {"type": "array", "maxItems": 1})
    run = Run(flow, [1])
    run.start()
    assert outcome(run) == ("succeeded", None)
    run = Run(flow, [1, 2])
    run.start()
    assert outcome(run) == ("suspended", "WV-SCHEMA-INVALID_INSTANCE")


def test_the_body_output_guard_the_artifact_carries_is_applied():
    flow = with_guard(identity(), "branch_output", {"type": "string"})
    run = Run(flow, ["a"])
    run.start()
    assert outcome(run) == ("succeeded", None)
    run = Run(flow, [1])
    run.start()
    assert outcome(run) == ("suspended", "WV-SCHEMA-INVALID_INSTANCE")


def test_a_run_timing_out_while_its_loop_yields_revokes_and_terminates():
    run = Run(identity(maxItems=10_000), list(range(2500)))
    run.start()
    assert run.state.branches == {} and run.state.active == ["each~1"]
    result = run.send("timed_out", deadline=NOW.isoformat())
    assert run.state.status == "timed_out"
    assert [(c.kind, c.node_id) for c in result.commands if isinstance(c, ControlCommand)] == [
        ("revoke_leases", "@start"),
        ("terminate", "@start"),
    ]


def test_a_loop_is_never_activated_twice():
    flow = identity()
    run = Run(flow, [1])
    run.state = RunState(input=[1], loops={"each": LoopState(owner="each", join="@join:each", count=1, concurrency=1)})
    run.start()
    assert outcome(run) == ("suspended", "WV-RUNTIME-ACTIVATION")


def body_task(worker_runtime_fixture) -> CompiledArtifact:
    return compiled(
        [each("each", [send()], {"ref": "/steps/send/output"})],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )


def test_an_iteration_whose_frame_is_not_running_cannot_finish(worker_runtime_fixture):
    run = Run(body_task(worker_runtime_fixture), [1])
    run.start()
    (name,) = run.state.branches
    run.state.branches[name] = run.state.branches[name].model_copy(update={"status": "completed"})
    run.complete("send[0]", 1)
    assert outcome(run) == ("suspended", "WV-RUNTIME-ACTIVATION")


def test_a_loop_that_is_no_longer_running_cannot_join(worker_runtime_fixture):
    run = Run(body_task(worker_runtime_fixture), [1])
    run.start()
    run.state.loops["each"] = run.state.loops["each"].model_copy(update={"status": "terminated"})
    run.complete("send[0]", 1)
    assert outcome(run) == ("suspended", "WV-RUNTIME-JOIN")


def test_a_human_task_reserves_its_task_and_its_expiry_deadline():
    review = {
        "id": "review",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": {"literal": "Review"},
        "context": {"object": {}},
        "formSchema": {"type": "object"},
        "decisions": ["approve"],
        "expirySeconds": 3600,
    }
    flow = compiled(
        [each("each", [review], {"ref": "/steps/review/output/decision"}, concurrency=1000, maxItems=1000)],
        {"ref": "/steps/each/output"},
    )
    assert prepare(flow).body_intents["each"] == 2
    run = Run(flow, list(range(600)))
    result = run.start()
    assert outcome(run) == ("waiting", None)
    assert sum(isinstance(c, HumanTaskIntent) for c in result.commands) == MAX_INTENTS // 2 // 2
