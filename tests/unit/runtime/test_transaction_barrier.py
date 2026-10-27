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

"""Settlement work is never blocked: pause and unrelated incidents hold back only new forward steps."""

from transaction_support import INPUT, Run, add, compiled, hold, submit, tasks, transaction


def lookup(identifier: str = "lookup") -> dict:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "reports.lookup@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
    }


def beside(*steps) -> list:
    """A transaction in one parallel branch and a step outside it in the other."""
    return [
        {
            "id": "both",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "a": {"steps": [transaction(list(steps))], "output": {"literal": None}},
                "b": {"steps": [lookup()], "output": {"literal": None}},
            },
        },
        lookup("after"),
    ]


def suspended_by_lookup(run: Run) -> None:
    run.send("incident_opened", node_id="lookup", code="WV-TASK-DEADLINE")
    assert run.state.status == "suspended"


def test_an_undo_result_is_applied_while_another_incident_holds_the_run():
    run = Run(compiled(beside(hold(), add(), submit())))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="unknown")
    suspended_by_lookup(run)
    [release] = tasks(run.done("add#undo", {}))
    assert release.node_id == "hold#cancel"
    assert run.state.deferred_results == [] and run.state.status == "suspended"


def test_a_forward_result_of_an_open_transaction_waits_behind_the_same_barrier():
    run = Run(compiled(beside(hold(), add(), submit())))
    run.start()
    suspended_by_lookup(run)
    deferred = run.done("hold", {"holdId": "h-1"})
    assert tasks(deferred) == []
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["hold"]


def test_a_hold_deadline_undoes_the_transaction_while_paused_with_the_result_it_already_has():
    run = Run(compiled([transaction([hold(), add(), submit()]), lookup("after")]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.send("paused", reason="look", control_revision=1)
    assert run.state.manual_paused
    # The order step is reached only after add; while paused add's result is deferred, so the hold fires first.
    deferred = run.done("add", {"id": "a-1"})
    assert tasks(deferred) == []
    expired = run.fire("move#hold")
    # add's result is already here: it is applied to the rollback at once, and its undo starts while paused.
    [undo] = tasks(expired)
    assert undo.node_id == "add#undo" and run.state.deferred_results == [] and run.state.manual_paused
    [release] = tasks(run.done("add#undo", {}))
    assert release.node_id == "hold#cancel"
    run.done("hold#cancel", {})
    # The block's end, and the step after it, wait for resume.
    assert run.txn().status == "compensated" and run.state.parked == ["@join:move"]
    run.send("resumed", reason="go", control_revision=2)
    assert run.state.status == "failed"


def test_while_paused_a_transaction_finishes_its_commit_and_the_step_after_it_waits_for_resume():
    run = Run(compiled([transaction([hold(), add(), submit()]), lookup("after")]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.send("paused", reason="look", control_revision=1)
    [confirm] = tasks(run.done("submit", {"orderId": "o-1"}))
    assert confirm.node_id == "hold#confirm"
    finished = run.done("hold#confirm", {})
    # Every reservation is confirmed; the rest of the body waits for resume, so the block has not completed yet.
    assert run.statuses()["hold"] == "confirmed" and run.txn().status == "committing"
    assert tasks(finished) == [] and run.state.parked == ["@branch:move:0"]
    [after] = tasks(run.send("resumed", reason="go", control_revision=2))
    assert (after.node_id, run.txn().status, run.state.parked) == ("after", "completed", [])


def test_one_transaction_in_doubt_does_not_stop_another_from_rolling_back():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "maxItems": 5,
        "concurrency": 5,
        "body": {"steps": [transaction([hold(), add(), submit()])], "output": {"literal": None}},
    }
    run = Run(compiled([each]), {**INPUT, "items": [1, 2, 3, 4, 5]})
    run.start()
    for index in (2, 4):
        run.done(f"hold[{index}]", {"holdId": f"h-{index}"})
    run.failed("add[2]", effect="unknown")
    [undo] = tasks(run.failed("add[4]", effect="unknown"))
    assert undo.node_id == "add[4]#undo"
    stuck = run.failed("add[2]#undo", "UNAVAILABLE", generation=10)
    assert run.txn("move[2]").status == "in_doubt" and tasks(stuck) == []
    # move[4] is already going back: its undo results are settlement work, so it finishes behind the barrier.
    [release] = tasks(run.done("add[4]#undo", {}))
    ended = run.done("hold[4]#cancel", {})
    assert release.node_id == "hold[4]#cancel"
    assert run.txn("move[4]").status == "compensated"
    # The block's end waits behind the barrier; the transaction in doubt emitted nothing.
    assert run.state.parked == ["@join:move[4]"] and tasks(ended) == []
    assert run.txn("move[2]").status == "in_doubt"


def test_a_forward_result_held_behind_another_incident_is_applied_when_its_transaction_goes_back():
    run = Run(compiled(beside(hold(), add(), submit())))
    run.start()
    suspended_by_lookup(run)
    run.done("hold", {"holdId": "h-1"})
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["hold"]
    run.later(600)
    [release] = tasks(run.fire("move#hold"))
    assert release.node_id == "hold#cancel" and run.state.deferred_results == []
    assert run.state.status == "suspended" and run.txn().status == "compensating"
