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


"""A transaction whose block can never complete: every way through its body reaches a Fail step.

Nothing follows such a block, so an On error setting of Continue has nothing to go on to: a rollback ends the run as
it does without the setting, and a person can't say the block completed while the run would have to go on after it.
"""

from functools import cache
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, reject, settings
from hypothesis import strategies as st
from transaction_support import INPUT, START, Run, add, compiled, hold, tasks, transaction

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode, TransactionNode
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES, split_phase
from firefly_weave.operations.debug.models import ScriptedError
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition, workflow
from firefly_weave.runtime.models import RunState, RuntimeEvent
from firefly_weave.runtime.transactions import STUCK

ROLLED_BACK = {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}
STOPPED = {"code": "STOPPED", "message": "The move was stopped."}
FAIL = {"id": "stop", "kind": "fail", **STOPPED}
PATH = {"steps": [], "output": {"literal": "undone"}}
LATER = {"id": "later", "kind": "transform", "value": {"literal": "later"}}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


def wait(identifier: str = "pause", seconds: int = 60) -> dict[str, Any]:
    return {"id": identifier, "kind": "wait", "durationSeconds": seconds}


def lookup(identifier: str = "look", **extra: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "reports.lookup@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
        **extra,
    }


def parallel(identifier: str = "both", /, **branches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "parallel",
        "concurrency": len(branches),
        "branches": {name: {"steps": steps, "output": {"literal": name}} for name, steps in branches.items()},
    }


def switch(identifier: str, taken: list[dict[str, Any]], default: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "switch",
        "cases": [{"when": {"literal": True}, "steps": taken, "output": {"literal": "taken"}}],
        "default": {"steps": default, "output": {"literal": "default"}},
    }


# Bodies in which add runs first and no way through ends at the block's output.
BODIES = {
    "ends in a fail step": [add(), FAIL],
    "a fail step in one parallel branch": [parallel(a=[add()], b=[wait(), FAIL])],
    "a fail step in the other parallel branch": [parallel(a=[wait(), FAIL], b=[add()])],
    "a fail step in every branch of a decision": [add(), switch("choose", [FAIL], [{**FAIL, "id": "other"}])],
    "a fail step in a parallel of a decision": [
        switch("choose", [parallel(a=[add()], b=[wait(), FAIL])], [{**FAIL, "id": "other"}])
    ],
    "a step after the fail step": [add(), FAIL, lookup("never")],
    "two steps and a fail step": [add(), hold(), FAIL],
}

# Where the transaction stands in the workflow. An action before it, or beside it, is still the run's own.
PLACES = {
    "alone": lambda block: [block],
    "before a step": lambda block: [block, LATER],
    "in a branch of a decision": lambda block: [switch("outer", [block], []), LATER],
    "in a branch of a parallel": lambda block: [parallel("outer", x=[block], y=[wait("beside")]), LATER],
    "in an error path": lambda block: [
        lookup(onError="errorOutput", errorPath={"steps": [block], "output": {"literal": "the block's place"}}),
        LATER,
    ],
}


@cache
def flow(body: str, place: str = "alone", *, continues: bool = True) -> CompiledArtifact:
    artifact = compiled(PLACES[place](transaction(BODIES[body], **({"onError": "continue"} if continues else {}))))
    [block] = [node for node in workflow(artifact).graph.nodes if isinstance(node, TransactionNode)]
    assert block.on_error == ("continue" if continues else None), "the compiled block keeps its setting"
    assert all(edge.source != block.join for edge in workflow(artifact).graph.edges), "nothing follows the block"
    return artifact


def opened(artifact: CompiledArtifact) -> Run:
    """Start the run and bring it to the open transaction, whose first step is in flight."""
    run = Run(artifact)
    run.start()
    if "look" in run.state.active:
        run.failed("look", "REJECTED", effect="none")
    assert run.txn().status == "open" and "add" in run.state.active
    return run


@pytest.mark.parametrize("place", list(PLACES))
@pytest.mark.parametrize("body", list(BODIES))
def test_a_rollback_with_nothing_to_go_on_to_fails_the_run_as_without_the_setting(body, place):
    run = opened(flow(body, place))
    ended = run.failed("add", "REJECTED", effect="none")
    assert (run.txn().status, run.txn().cause.code) == ("cancelled", "REJECTED")
    assert (run.state.status, run.state.incident, run.state.output) == ("failed", "WV-TXN-ROLLED-BACK", ROLLED_BACK)
    assert run.state.active == [] and run.state.incidents == {}
    # The block's failure was not handled: no step change says so, and no error object is left behind.
    assert [change for change in ended.steps if change.node_id == "move"] == []
    plain = opened(flow(body, place, continues=False))
    assert plain.failed("add", "REJECTED", effect="none").state == run.state


def test_the_last_undo_ends_the_run_when_nothing_follows_the_block():
    run = opened(flow("two steps and a fail step"))
    run.done("add", {"id": "a-1"})
    [undo] = tasks(run.failed("hold", "REJECTED", effect="none"))
    assert undo.node_id == "add#undo"
    run.done("add#undo", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("compensated", "failed", ROLLED_BACK)


def test_a_block_undone_while_the_run_is_paused_ends_the_run_on_resume():
    run = opened(flow("two steps and a fail step"))
    run.done("add", {"id": "a-1"})
    run.failed("hold", "REJECTED", effect="none")
    run.send("paused", reason="look", control_revision=1)
    run.done("add#undo", {})
    assert (run.txn().status, run.state.parked, run.state.status) == ("compensated", ["@join:move"], "waiting")
    run.send("resumed", reason="go", control_revision=2)
    assert (run.state.status, run.state.output, run.state.parked) == ("failed", ROLLED_BACK, [])


def test_an_expired_hold_fails_the_run_when_nothing_follows_the_block():
    run = Run(compiled([transaction([hold(holdSeconds=60), wait(seconds=600), FAIL], onError="continue")]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    [release] = tasks(run.fire("move#hold"))
    assert release.node_id == "hold#cancel"
    run.done("hold#cancel", {})
    assert (run.txn().cause.code, run.state.status, run.state.output) == ("WV-TXN-HOLD-EXPIRED", "failed", ROLLED_BACK)


def test_a_block_whose_business_key_fails_holds_the_run_as_without_the_setting():
    # The key is too long for a business key: the block never starts.
    block = {
        **transaction([add(), FAIL], onError="continue"),
        "businessKey": {"op": {"name": "concat", "args": [{"ref": "/input/moveId"}, {"literal": "x" * 300}]}},
    }
    run = Run(compiled([block]))
    run.start()
    assert (run.state.status, run.state.incident) == ("suspended", "WV-SCHEMA-INVALID_INSTANCE")
    assert [incident.node_id for incident in run.state.incidents.values()] == ["move"]
    plain = Run(compiled([{key: value for key, value in block.items() if key != "onError"}]))
    plain.start()
    assert plain.state == run.state


def test_a_simulation_shows_the_failed_run_and_no_handled_failure():
    refused = {"node:add": ScriptedError(code="REFUSED", effect="none")}
    simulator = Simulator(flow("ends in a fail step"), mocks={}, input=dict(INPUT), now=START, errors=refused)
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.variables["transactions"]["move"]["status"]) == ("failed", "cancelled")
    assert simulator.data.failures == {}


def test_only_continue_on_a_block_with_nothing_after_it_is_left_out():
    def output(body: list[dict[str, Any]], **setting: Any) -> tuple[str, Any]:
        run = Run(compiled([transaction(body, **setting), LATER], output={"ref": "/steps/later/output"}))
        run.start()
        run.failed("add", "REJECTED", effect="none")
        return run.state.status, run.state.output

    ends, can_end = [add(), FAIL], [add(), switch("choose", [FAIL], [])]
    assert output(ends, onError="continue") == ("failed", ROLLED_BACK)
    # An error path is followed by the next step, and so is a block one way through which ends at its output.
    assert output(ends, onError="errorOutput", errorPath=PATH) == ("succeeded", "later")
    assert output(can_end, onError="continue") == ("succeeded", "later")


def reconcile(run: Run, ending: str) -> None:
    """A person says how the transaction in doubt ended and what happened to each of its stuck steps."""
    doubted = next(record for record in run.state.transactions.values() if record.status == "in_doubt")
    key = doubted.incident
    incident = run.state.incidents[key]
    stuck = [item for item in doubted.participants if item.status in STUCK]
    outcomes = {"compensated": "undone", "completed": "done"}
    record = {
        "as": ending,
        "participants": [{"instance_key": item.key, "outcome": outcomes[ending]} for item in stuck],
        **({"blockOutput": "moved"} if ending == "completed" else {}),
    }
    run.send(
        "incident_resolved",
        incident_key=key,
        receipt_id=str(uuid4()),
        node_id=incident.node_id,
        generation=incident.generation,
        kind="accept_reconciled_result",
        output=record,
        actor_id="person",
    )


@pytest.mark.parametrize(
    "setting",
    [{}, {"onError": "continue"}, {"onError": "errorOutput", "errorPath": PATH}],
    ids=["stop", "continue", "errorOutput"],
)
def test_a_person_can_say_the_block_completed_only_when_the_run_is_ending(setting):
    artifact = compiled([transaction(BODIES["two steps and a fail step"], **setting)])
    run = opened(artifact)
    for step in IN_DOUBT:
        tell(run, *step)
    before = run.state.model_copy(deep=True)
    tell(run, "say", "completed")
    # Like any record that doesn't fit: the incident stays open, and every other decision is still possible.
    assert run.state == before.model_copy(update={"accepted_sequence": before.accepted_sequence + 1})
    tell(run, "say", "compensated")
    assert (run.txn().status, run.txn().reconciled_as) == ("reconciled", "compensated")
    assert (run.state.status, run.state.output) == ("failed", ROLLED_BACK)
    ending = opened(artifact)
    for step in [*ENDING, ("say", "completed")]:
        tell(ending, *step)
    assert (ending.txn().reconciled_as, ending.state.status) == ("completed", "cancelled")


OUTPUTS = {"add": {"id": "a-1"}, "hold": {"holdId": "h-1"}}


def tell(run: Run, kind: str, key: str = "") -> None:
    """One thing that happens to a run: a step's result, a deadline, a pause, a cancel or a person's decision."""
    if kind == "done":
        run.done(key, {} if split_phase(key) is not None else OUTPUTS.get(node_of(key), {"count": 1}))
    elif kind == "failed":
        run.failed(key, "REJECTED", effect="none", generation=1)
    elif kind == "lost":
        run.failed(key, "REJECTED", effect="unknown", generation=1)
    elif kind == "fire":
        run.fire(key)
    elif kind == "cancelled":
        run.send("cancelled", actor_id="person", reason="stop")
    elif kind == "say":
        reconcile(run, key)
    elif kind in {"succeeded", "stopped"}:
        # A called run's result: what it applied is unknown when it stopped.
        output = None if kind == "succeeded" else STOPPED
        run.called(key, "succeeded" if kind == "succeeded" else "failed", output, effect="unknown")
    else:
        run.send(kind, reason="look", control_revision=run.state.control_revision + 1)


IN_DOUBT = [("done", "add"), ("failed", "hold"), ("failed", "add#undo")]
ENDING = [("done", "add"), ("cancelled",), ("failed", "hold"), ("failed", "add#undo")]
# Histories of a block that continues with nothing after it, recorded while the end of its rollback still raised, with
# the digest of their transitions. The first is all that a run stopped there had recorded.
RECORDED = {
    "its last undo runs": (
        [("done", "add"), ("failed", "hold")],
        "8df8ef1be1a5e8edde3beba927c1d51928b8ac751f283c41a2a1c896bd8e664d",
    ),
    "canceled while its last undo ran": (
        [("done", "add"), ("failed", "hold"), ("cancelled",), ("done", "add#undo")],
        "542124d36324d44eb59455ccc32997b64a1dcf25ed4969ddc81b65a5a3f54eab",
    ),
    "in doubt, then said undone": (
        [*IN_DOUBT, ("say", "compensated")],
        "7aae0e6346f5984b62019dbc3c667b15158c006408ed478e6a4fb0e64a6bfcd6",
    ),
    "canceled, then said completed": (
        [*ENDING, ("say", "completed")],
        "024b687d155a67f11d16ab09d4843070aef3d8ae47b5660e973a8e2eee86b9c1",
    ),
    "its fail step ended it": (
        [("done", "add"), ("done", "hold"), ("done", "hold#cancel"), ("done", "add#undo")],
        "b82437eb041225f896d018a436b68fe5bca9d6d4d1cf68c2f620fea5ad8895d0",
    ),
}


def test_recorded_histories_keep_their_bytes_and_a_run_stopped_there_goes_on():
    runs = {}
    for name, (story, digest) in RECORDED.items():
        run = runs[name] = opened(flow("two steps and a fail step"))
        for step in story:
            tell(run, *step)
        assert canonical_digest([transition_digest(result) for result in run.transitions]) == digest, name
    stopped = runs["its last undo runs"]
    assert stopped.state.status == "waiting"
    tell(stopped, "done", "add#undo")
    assert (stopped.state.status, stopped.state.output) == ("failed", ROLLED_BACK)


def replayed(artifact: CompiledArtifact, events: list[RuntimeEvent]) -> list[str]:
    state, transitions = RunState(input=dict(INPUT)), []
    for event in events:
        result = transition(state, event, artifact)
        transitions.append(result.model_dump_json())
        state = result.state
    return transitions


def call(identifier: str, **extra: Any) -> dict[str, Any]:
    """A called run the step waits for."""
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "mode": "wait",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "a-1"}}},
        **extra,
    }


CONTINUE = {"onError": "continue"}
SETTINGS = st.sampled_from([CONTINUE, {}, {"onError": "errorOutput", "errorPath": PATH}, CONTINUE])
# The ways a block's body, or the branch beside it, goes on after its first step. Some always reach a fail step.
TAILS = st.sampled_from(
    [
        [FAIL],
        [switch("choose", [FAIL], [{**FAIL, "id": "other"}])],
        [parallel(a=[wait()], b=[wait("slow"), FAIL])],
        [parallel(a=[wait("slow"), FAIL], b=[wait()])],
        [wait()],
        [switch("choose", [FAIL], [])],
        [parallel(a=[wait()], b=[lookup()])],
    ]
)
BESIDE = st.sampled_from([[], [wait("beside")], [{**FAIL, "id": "elsewhere"}]])


@st.composite
def workflows(draw) -> CompiledArtifact:
    """A transaction in up to two containers, with every setting, beside called runs, waits and fail steps."""
    participants = [add(), hold(), call("called", compensate=add()["compensate"])]
    steps = draw(st.lists(st.sampled_from(participants), unique_by=lambda step: step["id"], min_size=1))
    block = transaction([*steps, *draw(TAILS)], **draw(SETTINGS))
    flow_ = [block, *draw(st.sampled_from([[], [LATER], [call("after")], [{**FAIL, "id": "last"}]]))]
    for depth, container in enumerate(
        draw(st.lists(st.sampled_from(["decision", "parallel", "loop", "path"]), max_size=2))
    ):
        outer = f"outer{depth}"
        beside = [{**step, "id": f"{step['id']}{depth}"} for step in draw(BESIDE)]
        if container == "decision":
            flow_ = [switch(outer, flow_, beside)]
        elif container == "parallel":
            flow_ = [parallel(outer, x=flow_, y=[*beside, call(f"{outer}-call", **draw(SETTINGS))])]
        elif container == "path":
            path = {"steps": flow_, "output": {"literal": None}}
            flow_ = [lookup(outer, onError="errorOutput", errorPath=path)]
        else:
            body = {"steps": flow_, "output": {"literal": None}}
            loop = {"kind": "forEach", "items": {"literal": [1, 2]}, "maxItems": 2, "concurrency": 2, "body": body}
            flow_ = [{"id": outer, **loop}]
    try:
        return compiled(flow_)
    except AssertionError:
        # The compiler refuses it, such as a loop whose body never completes.
        reject()


def choices(run: Run) -> list[tuple[str, str]]:
    """What can happen to a run next: each result it waits for, each person's decision, a pause or a cancel."""
    nodes = {node.id: node for node in workflow(run.flow).graph.nodes}
    state, found = run.state, []
    held = {event.data.get("node_id") for event in state.deferred_results}
    for key in sorted(set(state.active) - held):
        node = nodes[node_of(key)]
        if split_phase(key) is not None:
            found += [(kind, key) for kind in ("done", "failed")]
        elif isinstance(node, CallWorkflowNode):
            found += [(kind, key) for kind in ("succeeded", "stopped")]
        elif not isinstance(node, ActionNode):
            found.append(("fire", key))
        else:
            found += [(kind, key) for kind in ("done", "failed", "failed", "lost")]
    # Mostly results, and more failures than successes: a pause or a cancel ends most stories early.
    found *= 3
    if state.status == "suspended" and any(record.status == "in_doubt" for record in state.transactions.values()):
        found += [("say", "compensated"), ("say", "completed")]
    found.append(("resumed" if state.manual_paused else "paused", ""))
    if state.ending is None:
        found.append(("cancelled", ""))
    return found


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_whatever_happens_continue_with_nothing_after_the_block_reduces_exactly_like_no_setting(data):
    place, body = data.draw(st.sampled_from(sorted(PLACES))), data.draw(st.sampled_from(sorted(BODIES)))
    run = opened(flow(body, place))
    for _ in range(30):
        if run.state.status in TERMINAL:
            break
        tell(run, *data.draw(st.sampled_from(choices(run))))
    recorded = [result.model_dump_json() for result in run.transitions]
    assert replayed(flow(body, place, continues=False), run.events) == recorded
    assert replayed(run.flow, run.events) == recorded


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
@given(workflows(), st.data())
def test_no_event_of_a_run_with_a_transaction_escapes_the_kernel(artifact, data):
    run = Run(artifact)
    run.start()
    for _ in range(25):
        if run.state.status in TERMINAL:
            break
        tell(run, *data.draw(st.sampled_from(choices(run))))
    assert replayed(artifact, run.events) == [result.model_dump_json() for result in run.transitions]
