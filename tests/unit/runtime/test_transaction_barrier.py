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

"""Settlement work is never blocked: pause and unrelated incidents hold back only new forward steps. What a barrier held
back is applied when it lifts, or waits for the next one; work that stopped meanwhile leaves nothing behind."""

import pytest
from transaction_support import (
    INPUT,
    Run,
    add,
    attempt_failed,
    compiled,
    completed_once,
    controls,
    hold,
    lookup,
    notify,
    parallel,
    resolve,
    submit,
    tasks,
    transaction,
)

from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES

ROLLED_BACK = {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}
# How a read's attempt ends with an incident: recovery has not decided it yet, or only a person can.
INCIDENTS = ("WV-TASK-FAILED", "WV-SCHEMA-SECRET_VALUE")


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


def opens(run: Run, key: str, code: str) -> None:
    """A step's attempt ends with an incident of its own."""
    if code == "WV-TASK-FAILED":
        attempt_failed(run, key)
    else:
        run.send("incident_opened", node_id=key, generation=1, code=code)
    assert run.state.incidents[f"{key}:1"].code == code and run.state.status == "suspended"


@pytest.mark.parametrize("code", INCIDENTS)
def test_a_rollback_closes_the_incident_of_a_step_it_stops(code):
    run = Run(compiled([transaction([hold(), lookup(), submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    opens(run, "lookup", code)
    expired = run.fire("move#hold")
    assert [task.node_id for task in tasks(expired)] == ["hold#cancel"] and "lookup" in controls(expired, "settle")
    # Nothing is left to decide, so nothing holds the run back.
    assert (run.state.status, run.state.incidents, run.state.incident) == ("waiting", {}, None)
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("cancelled", "failed", ROLLED_BACK)


def test_a_rollback_leaves_the_incident_of_a_step_it_still_waits_for():
    run = Run(compiled([transaction([hold(), parallel(add(), lookup()), submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    opens(run, "add", "WV-TASK-FAILED")
    opens(run, "lookup", "WV-TASK-FAILED")
    run.fire("move#hold")
    assert (list(run.state.incidents), run.state.status, run.state.active) == (["add:1"], "suspended", ["add"])
    run.failed("add", "REFUSED", effect="none")
    assert (run.state.incidents, run.state.status) == ({}, "waiting")
    for key in ("add#undo", "hold#cancel"):
        run.done(key, {})
    assert (run.txn().status, run.state.status) == ("compensated", "failed")


def test_a_rollback_leaves_the_incident_of_an_event_the_kernel_refused():
    run = Run(compiled([transaction([hold(), lookup(), submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    opens(run, "lookup", "WV-RUNTIME-EVENT")
    run.fire("move#hold")
    assert (list(run.state.incidents), run.state.status) == (["lookup:1"], "suspended")


@pytest.mark.parametrize("code", INCIDENTS)
@pytest.mark.parametrize("ending", ["cancelled", "timed_out"])
def test_an_ending_run_closes_the_incidents_of_the_steps_it_stops(ending, code):
    run = Run(compiled([parallel(transaction([hold(), lookup()]), lookup("outside"))], timeout=3600))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    opens(run, "lookup", code)
    opens(run, "outside", code)
    if ending == "cancelled":
        run.send("cancelled", reason="stop")
    else:
        run.later(3600)
        run.send("timed_out", node_id="@run", deadline=run.now.isoformat(), wait_id="w")
    assert (run.state.status, run.state.incidents, run.state.incident) == ("waiting", {}, None)
    assert run.state.active == ["hold#cancel"]
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("cancelled", ending)


def test_an_ending_run_still_waits_for_a_transaction_in_doubt():
    other = transaction([hold("hold2"), lookup()], identifier="other")
    run = Run(compiled([parallel(transaction([hold(), submit()]), other)]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    run.send("cancelled", reason="stop")
    assert (run.state.status, list(run.state.incidents)) == ("suspended", ["hold#confirm:2"])
    run.failed("hold2", "REFUSED", effect="none")
    assert (run.txn("other").status, run.state.status) == ("cancelled", "suspended")
    resolve(run, "hold#confirm:2", "terminate")
    assert (run.txn().status, run.state.status) == ("force_ended", "cancelled")


def test_when_the_step_didnt_happen_the_read_beside_it_leaves_nothing_open():
    for held in ("an interim failure", "a deferred result"):
        run = Run(compiled([transaction([hold(), parallel(submit(), lookup())])]))
        run.start(2)
        run.done("hold", {"holdId": "h-1"})
        run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
        if held == "an interim failure":
            attempt_failed(run, "lookup")
        else:
            run.done("lookup", {"count": 1})
            assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]
        [release] = tasks(resolve(run, "submit:3", "accept_reconciled_failure"))
        assert release.node_id == "hold#cancel"
        assert (run.state.status, run.state.incidents, run.state.deferred_results) == ("waiting", {}, [])
        run.done("hold#cancel", {})
        assert (run.txn().status, run.state.status) == ("cancelled", "failed")


def after_the_pivot(*steps) -> Run:
    """The steps run side by side after the point of no return, and nothing else is left to settle."""
    run = Run(compiled([transaction([hold(), submit(), parallel(*steps)])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    return run


@pytest.mark.parametrize("third", ["fails", "succeeds"])
@pytest.mark.parametrize("second", ["notify2", "notify3"])
def test_a_result_deferred_behind_a_second_doubt_waits_for_it(second, third):
    run = after_the_pivot(notify(), notify("notify2"), notify("notify3"))
    last = {"notify2": "notify3", "notify3": "notify2"}[second]
    run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    run.failed(second, "UNAVAILABLE", effect="unknown", generation=4)
    if third == "fails":
        run.failed(last, "UNAVAILABLE", effect="unknown", generation=4)
    else:
        run.done(last, {})
    resolve(run, "notify:4", "retry_safe")
    # The second failure put the transaction in doubt again; what came after it still waits.
    assert (run.txn().status, run.txn().incident) == ("in_doubt", f"{second}:4")
    assert [event.data["node_id"] for event in run.state.deferred_results] == [last]
    assert run.statuses()[last] == "dispatched"
    run.done("notify", {}, generation=5)
    assert [event.data["node_id"] for event in run.state.deferred_results] == [last, "notify"]
    resolve(run, f"{second}:4", "retry_safe")
    if third == "fails":
        assert list(run.state.incidents) == [f"{last}:4"]
        resolve(run, f"{last}:4", "retry_safe")
        run.done(last, {}, generation=5)
    run.done(second, {}, generation=5)
    assert (run.txn().status, run.state.status, run.state.deferred_results) == ("completed", "succeeded", [])
    assert all(completed_once(run, key) for key in ("notify", "notify2", "notify3", "@join:both"))


def test_results_deferred_again_keep_their_order():
    run = after_the_pivot(*(notify(key) for key in ("notify", "notify2", "notify3", "notify4")))
    for key in ("notify", "notify3", "notify4", "notify2"):
        run.failed(key, "UNAVAILABLE", effect="unknown", generation=4)
    resolve(run, "notify:4", "retry_safe")
    assert list(run.state.incidents) == ["notify3:4"]
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["notify4", "notify2"]


def test_a_deferred_result_of_a_stopped_step_is_dropped():
    run = Run(compiled([transaction([hold(), add(), parallel(add("add2"), lookup()), submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    attempt_failed(run, "lookup")
    attempt_failed(run, "add2")
    run.failed("lookup", "UNAVAILABLE", effect="unknown")
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]
    [undo] = tasks(run.failed("add2", "REFUSED", effect="none"))
    assert undo.node_id == "add2#undo" and run.state.deferred_results == []
    assert (run.txn().status, run.state.status, run.state.incidents) == ("compensating", "waiting", {})
    for key in ("add2#undo", "add#undo", "hold#cancel"):
        run.done(key, {})
    assert (run.txn().status, run.state.status) == ("compensated", "failed")


def test_settlement_work_passes_a_barrier_that_returns():
    forward = transaction([hold(), submit(), parallel(notify(), notify("notify2"))])
    other = transaction([parallel(add("x"), add("y"), identifier="pair")], identifier="other")
    run = Run(compiled([parallel(forward, other, identifier="fork")]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    # Behind the barrier: the other transaction goes back, this one is in doubt again, and then a result settles.
    run.failed("y", "REFUSED", effect="none")
    run.failed("notify2", "UNAVAILABLE", effect="unknown", generation=4)
    run.done("x", {"id": "a-1"})
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["y", "notify2", "x"]
    lifted = resolve(run, "notify:4", "retry_safe")
    assert list(run.state.incidents) == ["notify2:4"] and run.state.deferred_results == []
    assert [task.node_id for task in tasks(lifted)] == ["x#undo"] and run.txn("other").status == "compensating"


def test_a_paused_run_whose_incidents_closed_waits_for_resume():
    run = Run(compiled([transaction([hold(), lookup(), submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    opens(run, "lookup", "WV-TASK-FAILED")
    run.send("paused", reason="look", control_revision=1)
    [release] = tasks(run.fire("move#hold"))
    assert (run.state.status, run.state.incidents, run.state.manual_paused) == ("waiting", {}, True)
    run.done(release.node_id, {})
    assert run.txn().status == "cancelled" and run.state.parked == ["@join:move"]
    run.send("resumed", reason="go", control_revision=2)
    assert (run.state.status, run.state.output) == ("failed", ROLLED_BACK)


def two_moves(*beside, paused: bool = True) -> Run:
    """Two transactions side by side: ``move`` with the steps ``beside``, and one whose only change is ``a9``."""
    other = transaction([add("a9")], identifier="other")
    run = Run(compiled([parallel(transaction(list(beside)), other, identifier="fork")]))
    run.start(2)
    return run


def fails_in_a_resume(run: Run, finishes: str, output) -> None:
    """The last step of one transaction finishes and the only change of the other applies nothing, in one event."""
    run.send("paused", reason="p", control_revision=1)
    run.done(finishes, output)
    run.failed("a9", "REFUSED", effect="none")
    run.send("resumed", reason="p", control_revision=2)


def test_a_run_failure_keeps_the_join_of_a_transaction_that_finished():
    run = two_moves(hold(), submit(), notify())
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    fails_in_a_resume(run, "notify", {})
    assert (run.state.status, run.txn().status) == ("failed", "completed")


def test_a_run_failure_keeps_the_join_of_a_transaction_that_finished_behind_a_failed_release():
    other = transaction([hold("h9"), add("a9")], identifier="other")
    run = Run(compiled([parallel(transaction([hold(), submit(), notify()]), other, identifier="fork")]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    run.done("h9", {"holdId": "h-9"})
    run.failed("a9", "REFUSED", effect="none")
    attempt_failed(run, "h9#cancel")
    run.done("notify", {})
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["notify"]
    resolve(run, "h9#cancel:1", "accept_reconciled_result", {})
    assert (run.state.status, run.txn().status, run.txn("other").status) == ("failed", "completed", "cancelled")


@pytest.mark.parametrize(
    ("step", "output", "undoes", "ended"),
    [
        (add, {"id": "a-1"}, "add#undo", "compensated"),
        (hold, {"holdId": "h-1"}, "hold#cancel", "cancelled"),
    ],
)
def test_a_run_failure_drops_the_join_of_a_transaction_it_undoes(step, output, undoes, ended):
    run = two_moves(step())
    fails_in_a_resume(run, undoes.partition("#")[0], output)
    assert (run.state.status, run.state.active, run.state.incidents) == ("waiting", [undoes], {})
    run.done(undoes, {})
    assert (run.state.status, run.txn().status, run.state.incidents) == ("failed", ended, {})


def test_a_run_failure_keeps_the_join_of_a_loop_iteration_that_finished():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "maxItems": 2,
        "concurrency": 2,
        "body": {"steps": [transaction([hold(), submit(), notify()])], "output": {"literal": None}},
    }
    other = transaction([add("a9")], identifier="other")
    run = Run(compiled([parallel(each, other, identifier="fork")]))
    run.start(2)
    for index in (0, 1):
        run.done(f"hold[{index}]", {"holdId": "h-1"})
        run.done(f"submit[{index}]", {"orderId": "o-1"})
        run.done(f"hold[{index}]#confirm", {})
    run.send("paused", reason="p", control_revision=1)
    run.done("notify[1]", {})
    run.failed("a9", "REFUSED", effect="none")
    run.send("resumed", reason="p", control_revision=2)
    # The other iteration still runs its last step, and the run ends once it has.
    assert (run.state.status, run.state.active) == ("waiting", ["notify[0]"])
    assert run.txn("move[1]").status == "completed"
    run.done("notify[0]", {})
    assert run.state.status == "failed"
