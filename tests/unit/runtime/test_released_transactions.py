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

"""The final records of transactions whose loop iteration ended: the transition that releases them hands them on, and
neither the run's state nor anything stored, measured or digested changes with them."""

import json
import re
from functools import cache
from pathlib import Path

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from transaction_support import INPUT, Run, add, compiled, controls, hold, notify, submit, transaction

from firefly_weave.compiler.api import import_artifact
from firefly_weave.operations.exports import transition_digest
from firefly_weave.operations.replay import replay
from firefly_weave.runtime.capacity import TRANSITION_BYTES, logical_size
from firefly_weave.runtime.kernel import KernelCursor, advance, begin, transition
from firefly_weave.runtime.models import RunState, RuntimeEvent, TransactionRecord, Transition

HISTORY = Path("tests/fixtures/transactions/loop-history.yaml")


def each(steps, *, concurrency=1):
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "maxItems": 5,
        "concurrency": concurrency,
        "body": {"steps": steps, "output": {"literal": None}},
    }


def looped(steps, items: int, *, concurrency: int = 1, **options) -> Run:
    flow = compiled([each([transaction(steps, onError="continue")], concurrency=concurrency)], **options)
    run = Run(flow, {**INPUT, "items": list(range(1, items + 1))})
    run.start()
    return run


def commit(run: Run, suffix: str) -> Transition:
    run.done("hold" + suffix, {"holdId": "h"})
    run.done("add" + suffix, {"id": "a"})
    run.done("submit" + suffix, {"orderId": "o"})
    return run.done(f"hold{suffix}#confirm", {})


def undo(run: Run, suffix: str) -> Transition:
    run.done("hold" + suffix, {"holdId": "h"})
    run.failed("add" + suffix, "UNAVAILABLE")
    run.done(f"add{suffix}#undo", {})
    return run.done(f"hold{suffix}#cancel", {})


def test_the_transition_that_ends_an_iteration_hands_on_the_final_record_of_its_transaction():
    run = looped([hold(), add(), submit()], 3)
    undone = undo(run, "[0]")
    assert controls(undone, "transaction") == ["move[0]", "move[1]"]
    assert list(undone.released) == ["move[0]"] and "move[0]" not in undone.state.transactions
    record = undone.released["move[0]"]
    assert (record.status, record.ended_at, record.cause.code) == ("compensated", run.now, "UNAVAILABLE")
    assert [(item.key, item.status) for item in record.participants] == [
        ("hold[0]", "cancelled"),
        ("add[0]", "undone"),
    ]
    run.done("hold[1]", {"holdId": "h"})
    run.failed("add[1]", "UNAVAILABLE")
    doubtful = run.failed("add[1]#undo", "REJECTED", effect="none", generation=3)
    assert doubtful.released == {} and run.txn("move[1]").status == "in_doubt"
    reconciled = run.send(
        "incident_resolved",
        incident_key="add[1]#undo:3",
        node_id="add[1]#undo",
        generation=3,
        kind="accept_reconciled_result",
        output={
            "as": "completed",
            "participants": [{"instance_key": "add[1]", "outcome": "done"}],
            "blockOutput": "moved",
        },
    )
    assert {key: record.status for key, record in reconciled.released.items()} == {"move[1]": "reconciled"}
    completed = commit(run, "[2]")
    assert {key: record.status for key, record in completed.released.items()} == {"move[2]": "completed"}
    assert run.state.status == "succeeded"


def test_a_transaction_that_opens_and_ends_in_one_event_is_handed_on_too():
    shape = {"id": "shape", "kind": "transform", "value": {"ref": "/input/items/4"}}
    run = looped([shape, hold()], 2)
    [started] = run.transitions
    assert controls(started, "transaction") == ["move[0]", "move[1]"]
    assert {key: record.status for key, record in started.released.items()} == {
        "move[0]": "cancelled",
        "move[1]": "cancelled",
    }


def test_the_record_of_a_changed_transaction_is_the_states_or_the_released_one():
    run = looped([hold(), add(), submit()], 2)
    undone = undo(run, "[0]")
    assert undone.transaction("move[0]").status == "compensated"
    assert undone.transaction("move[1]").status == "open"


def test_nothing_stored_measured_or_digested_changes_with_a_released_record():
    run = looped([hold(), add(), submit()], 2)
    undo(run, "[0]")
    commit(run, "[1]")
    released = [result for result in run.transitions if result.released]
    assert len(released) == 2
    for result in released:
        # The same transition without its released records, as main's kernel returned it.
        bare = Transition.model_validate_json(result.model_dump_json())
        assert bare.released == {}
        assert result.model_dump_json() == bare.model_dump_json()
        assert result.model_dump(mode="json") == bare.model_dump(mode="json")
        assert result.state.model_dump_json() == bare.state.model_dump_json()
        assert transition_digest(result) == transition_digest(bare)
        assert logical_size(result, TRANSITION_BYTES) == logical_size(bare, TRANSITION_BYTES)
        assert "released" not in result.model_dump_json()
    assert "released" not in Transition.model_json_schema()["properties"]


def test_a_history_recorded_before_the_final_records_were_handed_on_still_replays():
    recorded = yaml.safe_load(HISTORY.read_text())
    flow = import_artifact(recorded["artifact"])
    events = tuple(RuntimeEvent.model_validate_json(json.dumps(event)) for event in recorded["events"])
    report = replay(flow, events)
    assert (report.status, report.diagnostics, report.last_verified_sequence) == ("consistent", [], len(events))
    # The same history now also says how each transaction of the loop ended.
    state, ended = RunState(input=dict(INPUT)), {}
    for event in events:
        result = transition(state, event.model_copy(update={"evidence": None}), flow)
        state = result.state
        ended.update({key: record.status for key, record in result.released.items()})
    assert ended == {"move[0]": "compensated", "move[1]": "reconciled", "move[2]": "completed"}


def test_the_state_never_grows_with_the_transactions_of_a_loop():
    sizes = {}
    for items in (2, 5):
        run = looped([hold(), add(), submit()], items, concurrency=2)
        for index in range(items):
            commit(run, f"[{index}]")
        assert run.state.status == "succeeded" and run.state.transactions == {}
        # Never more records than iterations in flight, and each transition hands on only what it released.
        assert max(len(result.state.transactions) for result in run.transitions) == 2
        handed_on = [key for result in run.transitions for key in result.released]
        assert sorted(handed_on) == sorted(set(handed_on)) == [f"move[{index}]" for index in range(items)]
        sizes[items] = len(run.state.model_dump_json()) - len(json.dumps(run.state.input))
    assert abs(sizes[5] - sizes[2]) < 64


def test_an_event_that_is_rolled_back_behind_an_incident_releases_nothing():
    # The second iteration can't be evaluated outside any transaction, so the whole event that ends the first one is
    # refused: the first transaction keeps its record in the state.
    shape = {
        "id": "shape",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/index"}, {"literal": 0}]}},
                "steps": [],
                "output": {"literal": 1},
            }
        ],
        "default": {
            "steps": [{"id": "wrong", "kind": "transform", "value": {"ref": "/input/items/4"}}],
            "output": {"literal": 2},
        },
    }
    flow = compiled([each([shape, transaction([hold(), add()], onError="continue")])])
    run = Run(flow, {**INPUT, "items": [1, 2]})
    run.start()
    run.done("hold[0]", {"holdId": "h"})
    run.done("add[0]", {"id": "a"})
    refused = run.done("hold[0]#confirm", {})
    assert run.state.status == "suspended"
    assert refused.released == {} and controls(refused, "transaction") == []
    assert run.txn("move[0]").status == "committing"


def test_a_cursor_stored_in_the_middle_of_an_event_keeps_what_the_event_released():
    run = looped([hold(), add(), submit()], 2)
    for key, output in (("hold[0]", {"holdId": "h"}), ("add[0]", {"id": "a"}), ("submit[0]", {"orderId": "o"})):
        run.done(key, output)
    before = run.state
    whole = run.done("hold[0]#confirm", {})
    event = run.events[-1]
    cursor, stored = begin(before, event, run.flow), 0
    while not cursor.done:
        cursor = advance(cursor, run.flow)
        if cursor.released:
            stored += 1
            assert cursor.released["move[0]"].status == "completed"
        cursor = KernelCursor.model_validate_json(cursor.model_dump_json())
        if cursor.done:
            break
    assert stored, "the event released its transaction before its last boundary"
    # The cursor of a finished event carries no copy of what its result hands on.
    final = advance(*unfinished(before, event, run))
    assert final.released == {} and final.result.released == whole.released
    assert "released" not in begin(before, event, run.flow).model_dump_json()


def unfinished(state: RunState, event: RuntimeEvent, run: Run):
    """The last boundary of an event, each earlier one stored and read back."""
    cursor = begin(state, event, run.flow)
    while True:
        following = advance(cursor, run.flow)
        if following.done:
            return cursor, run.flow
        cursor = KernelCursor.model_validate_json(following.model_dump_json())


OUTPUTS = {"hold": {"holdId": "h"}, "add": {"id": "a"}, "submit": {"orderId": "o"}, "notify": {}}
BODIES = st.lists(st.sampled_from(["hold", "add"]), min_size=1, max_size=3).flatmap(
    lambda kinds: st.sampled_from([(*kinds,), (*kinds, "submit"), (*kinds, "submit", "notify")])
)
# What a worker reports for the next step in flight: a result, a failure that applied nothing, or one that may have.
RESULTS = st.lists(st.sampled_from(["done", "done", "done", "none", "unknown"]), min_size=12, max_size=12)


@cache
def flows(kinds: tuple[str, ...]):
    """A transaction of these steps inside a loop, and alone."""
    built = {"hold": hold, "add": add, "submit": submit, "notify": notify}
    block = transaction([built[kind](f"{kind}{index}") for index, kind in enumerate(kinds)], onError="continue")
    return compiled([each([block])]), compiled([block])


def settle(run: Run, results: list[str], suffix: str) -> TransactionRecord:
    """Answer the steps of one transaction in order until it ended or is in doubt; returns its last record."""
    key = "move" + suffix
    for result in results:
        if key not in run.state.transactions or run.txn(key).status == "in_doubt":
            break
        flying = [name for name in run.state.active if re.fullmatch(r"[a-z]+\d" + re.escape(suffix) + "(#.+)?", name)]
        if not flying:
            break
        name = sorted(flying)[0]
        if result == "done":
            run.done(name, {} if "#" in name else OUTPUTS[re.sub(r"\d.*", "", name)])
        else:
            run.failed(name, "UNAVAILABLE", effect=result)
    for result in reversed(run.transitions):
        if key in result.released:
            return result.released[key]
        if key in result.state.transactions:
            return result.state.transactions[key]
    raise AssertionError(f"{key} has no record")


def bare(record: TransactionRecord) -> dict:
    """A record without the loop positions and the event numbers, which say only where its transaction ran."""
    plain = json.loads(re.sub(r"\[\d+\]", "", record.model_dump_json(exclude={"decided_at_sequence"})))
    plain["decided"] = record.decided_at_sequence is not None
    return plain


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(kinds=BODIES, results=st.lists(RESULTS, min_size=1, max_size=3))
def test_a_transaction_ends_with_the_same_record_inside_a_loop_and_outside_one(kinds, results):
    in_loop, alone_flow = flows(kinds)
    loop = Run(in_loop, {**INPUT, "items": list(range(1, len(results) + 1))})
    loop.start()
    for index, item in enumerate(results):
        inside = settle(loop, item, f"[{index}]")
        alone = Run(alone_flow, dict(INPUT))
        alone.start()
        outside = settle(alone, item, "")
        assert bare(inside) == bare(outside)
        if f"move[{index}]" in loop.state.transactions:
            # In doubt, or still waiting for a result: the later iterations have not started.
            break
    # Replaying the events gives the same stored transitions and the same final records.
    state = RunState(input={**INPUT, "items": list(range(1, len(results) + 1))})
    for event, recorded in zip(loop.events, loop.transitions, strict=True):
        replayed = transition(state, event, loop.flow)
        assert replayed.model_dump_json() == recorded.model_dump_json()
        assert replayed.released == recorded.released
        state = replayed.state
