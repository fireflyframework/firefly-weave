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

"""Loops inside and around transactions, and a run that ends only after its transactions settle."""

from transaction_support import (
    INPUT,
    Run,
    add,
    compiled,
    controls,
    deadlines,
    hold,
    submit,
    tasks,
    transaction,
)


def each(steps, *, concurrency=2):
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "maxItems": 5,
        "concurrency": concurrency,
        "body": {"steps": steps, "output": {"literal": None}},
    }


def test_a_failed_item_undoes_the_done_items_in_reverse_dispatch_order():
    looped = add("add")
    looped["with"] = {"object": {"quantity": {"ref": "/item"}, "reference": {"ref": "/input/moveId"}}}
    looped["compensate"]["with"]["object"]["reference"] = {
        "op": {
            "name": "concat",
            "args": [{"ref": "/input/moveId"}, {"literal": "-"}, {"ref": "/participant/operationKey"}],
        }
    }
    run = Run(compiled([transaction([hold(), each([looped]), submit()])]), {**INPUT, "items": [1, 2, 3, 4, 5]})
    run.start()
    started = run.done("hold", {"holdId": "h-1"})
    assert [task.node_id for task in tasks(started)] == ["add[0]", "add[1]"]
    run.done("add[1]", {"id": "a-1"})
    run.done("add[0]", {"id": "a-0"})
    run.done("add[2]", {"id": "a-2"})
    # add[3] fails while add[4] is in flight; add[4] settles first, then everything is undone in reverse order.
    assert tasks(run.failed("add[3]", effect="none")) == []
    order = [task.node_id for task in tasks(run.done("add[4]", {"id": "a-4"}))]
    while following := tasks(run.done(order[-1], {})):
        order.append(following[0].node_id)
    # Reverse registration order: add[4] was dispatched after add[2], and add[1] before add[0] completed.
    assert order == ["add[4]#undo", "add[2]#undo", "add[1]#undo", "add[0]#undo", "hold#cancel"]
    assert run.statuses() == {
        "hold": "cancelled",
        "add[1]": "undone",
        "add[0]": "undone",
        "add[2]": "undone",
        "add[3]": "failed",
        "add[4]": "undone",
    }


def test_undo_steps_of_items_read_the_item_they_belong_to():
    looped = add("add")
    looped["with"] = {"object": {"quantity": {"ref": "/item"}, "reference": {"ref": "/input/moveId"}}}
    looped["compensate"]["with"]["object"]["reference"] = {
        "op": {"name": "concat", "args": [{"ref": "/input/moveId"}, {"literal": "/"}, {"ref": "/index"}]}
    }
    run = Run(compiled([transaction([hold(), each([looped], concurrency=1), submit()])]), {**INPUT, "items": [7, 8]})
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add[0]", {"id": "a-0"})
    [undo] = tasks(run.failed("add[1]", effect="unknown"))
    assert (undo.node_id, undo.input["reference"]) == ("add[1]#undo", "M-1/1")
    [earlier] = tasks(run.done("add[1]#undo", {}))
    assert (earlier.node_id, earlier.input["reference"]) == ("add[0]#undo", "M-1/0")


def test_a_cancelled_run_undoes_its_open_transaction_then_ends_cancelled():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    cancelled = run.send("cancelled", reason="stop")
    assert controls(cancelled, "settle") == ["add"] and not controls(cancelled, "terminate")
    assert (run.state.ending.status, run.txn().cause.code) == ("cancelled", "WV-TXN-RUN-CANCELLED")
    [undo] = tasks(run.done("add", {"id": "a-1"}))
    assert undo.node_id == "add#undo"
    run.done("add#undo", {})
    ended = run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("compensated", "cancelled")
    assert controls(ended, "terminate")


def test_a_run_without_open_transactions_ends_at_once_as_before():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    assert run.state.status == "succeeded"
    run = Run(
        compiled([transaction([hold(), add(), submit()]), {"id": "pause", "kind": "wait", "durationSeconds": 60}])
    )
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    cancelled = run.send("cancelled", reason="stop")
    assert run.state.status == "cancelled" and controls(cancelled, "terminate")


def test_a_run_timeout_undoes_first_and_then_ends_timed_out():
    run = Run(compiled([transaction([hold(), add(), submit()])], timeout=300))
    started = run.start()
    run_deadline = next(d for d in deadlines(started) if d.node_id == "@run")
    run.done("hold", {"holdId": "h-1"})
    run.now = run_deadline.deadline
    timed_out = run.send("timed_out", node_id="@run", deadline=run_deadline.deadline.isoformat(), wait_id="w")
    assert (run.state.ending.status, run.txn().cause.code) == ("timed_out", "WV-TXN-RUN-TIMED-OUT")
    assert controls(timed_out, "settle") == ["add"]
    [release] = tasks(run.failed("add", "VALIDATION", effect="none"))
    run.done(release.node_id, {})
    assert (run.txn().status, run.state.status) == ("cancelled", "timed_out")


def test_a_failure_outside_a_transaction_undoes_the_transaction_beside_it_before_the_run_fails():
    fork = {
        "id": "both",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [transaction([hold(), add(), submit()])], "output": {"literal": None}},
            "b": {
                "steps": [
                    {"id": "pause", "kind": "wait", "durationSeconds": 60},
                    {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "Stopped."},
                ],
                "output": {"literal": None},
            },
        },
    }
    run = Run(compiled([fork]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    stopped = run.fire("pause")
    assert run.state.ending.model_dump(mode="json") == {
        "status": "failed",
        "code": "STOPPED",
        "output": {"code": "STOPPED", "message": "Stopped."},
    }
    assert controls(stopped, "settle") == ["add"] and not controls(stopped, "terminate")
    [release] = tasks(run.failed("add", "VALIDATION", effect="none"))
    run.done(release.node_id, {})
    assert (run.state.status, run.state.output) == ("failed", {"code": "STOPPED", "message": "Stopped."})


def test_a_forward_result_deferred_behind_a_pause_is_applied_when_its_transaction_goes_back():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.send("paused", reason="look", control_revision=1)
    run.done("hold", {"holdId": "h-1"})
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["hold"]
    run.later(600)
    expired = run.fire("move#hold")
    # Weave already has the reservation's result: it is settlement work now, so the reservation is released while
    # the run stays paused, and no settle deadline is needed.
    [release] = tasks(expired)
    assert release.node_id == "hold#cancel" and release.settlement
    assert run.state.deferred_results == [] and run.state.manual_paused
    assert run.statuses()["hold"] == "cancelling" and run.txn().settle_by is None
    run.done("hold#cancel", {})
    assert run.txn().status == "cancelled"
