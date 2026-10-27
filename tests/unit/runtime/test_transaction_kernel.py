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

"""Transactions in the kernel: registration before dispatch, commit and confirms, rollback in reverse order."""

from datetime import timedelta

from transaction_support import (
    RUN,
    Run,
    add,
    calls,
    compiled,
    controls,
    deadlines,
    hold,
    notify,
    reverse_by_call,
    submit,
    submit_by_call,
    tasks,
    transaction,
)

from firefly_weave.contracts.step_errors import STEP_ERROR_MESSAGES
from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES


def test_a_transaction_reserves_adds_decides_and_confirms():
    run = Run(compiled([transaction([hold(), add(), submit()])], output={"ref": "/steps/move/output"}))
    started = run.start()
    [try_hold] = tasks(started)
    assert (try_hold.node_id, try_hold.settlement) == ("hold", False)
    assert try_hold.transaction is not None
    assert try_hold.transaction.model_dump(mode="json") == {
        "id": "move",
        "branch": "hold",
        "phase": "try",
        "mode": "reserve",
    }
    assert [d.node_id for d in deadlines(started)] == ["move#hold"]
    assert run.txn().status == "open"
    [forward] = tasks(run.done("hold", {"holdId": "h-1"}))
    assert (forward.node_id, forward.transaction.phase) == ("add", "forward")
    [pivot] = tasks(run.done("add", {"id": "a-1"}))
    assert (pivot.node_id, pivot.settlement, run.txn().status) == ("submit", True, "deciding")
    decided = run.done("submit", {"orderId": "o-1"})
    [confirm] = tasks(decided)
    assert (confirm.node_id, confirm.input, confirm.settlement) == ("hold#confirm", {"holdId": "h-1"}, True)
    assert confirm.retry_policy == {"maxAttempts": 10, "initialDelaySeconds": 2, "maxDelaySeconds": 300}
    assert (run.txn().status, run.txn().decided_at_sequence) == ("committing", run.state.accepted_sequence)
    finished = run.done("hold#confirm", {})
    assert run.txn().status == "completed"
    assert run.statuses() == {"hold": "confirmed", "add": "done", "submit": "done"}
    assert (run.state.status, run.state.output) == ("succeeded", "moved")
    assert controls(finished, "transaction") == ["move"]
    assert run.state.run_id == RUN


def branches():
    return {
        "id": "branches",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [add("add")], "output": {"literal": None}},
            "b": {"steps": [add("add2")], "output": {"literal": None}},
        },
    }


def flow(*steps, **settings):
    return compiled([transaction(list(steps), **settings)])


def held_and_added(*steps):
    run = Run(flow(hold(), add(), *steps))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    return run


def test_a_failure_after_two_steps_undoes_them_in_reverse_order_and_fails_the_run_without_an_incident():
    run = held_and_added(add("add2"), submit())
    run.done("add", {"id": "a-1"})
    aborted = run.failed("add2", "UNAVAILABLE", effect="unknown")
    # The failed step may have applied, so it is undone first; then add, then the reservation is released.
    [undo] = tasks(aborted)
    assert (undo.node_id, undo.transaction.phase, undo.transaction.outcome) == ("add2#undo", "compensate", "unknown")
    assert undo.input == {"reference": "M-1", "addOperation": f"{RUN}:add2"}
    assert run.txn().status == "compensating"
    assert run.txn().cause.model_dump() == {"code": "UNAVAILABLE", "instance_key": "add2", "effect": "unknown"}
    [second] = tasks(run.done("add2#undo", {}))
    assert (second.node_id, second.transaction.outcome) == ("add#undo", "done")
    [release] = tasks(run.done("add#undo", {}))
    assert (release.node_id, release.input) == ("hold#cancel", {"reference": "M-1", "holdOperation": f"{RUN}:hold"})
    ended = run.done("hold#cancel", {})
    assert run.txn().status == "compensated"
    assert run.statuses() == {"hold": "cancelled", "add": "undone", "add2": "undone"}
    assert run.state.status == "failed"
    assert run.state.output == {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}
    assert run.state.incidents == {} and run.state.incident == "WV-TXN-ROLLED-BACK"
    assert controls(ended, "terminate")


def test_a_definite_failure_needs_no_undo_and_only_reservations_end_cancelled():
    run = Run(flow(hold(), add(), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    [release] = tasks(run.failed("add", "VALIDATION", effect="none"))
    assert release.node_id == "hold#cancel"
    assert run.statuses() == {"hold": "cancelling", "add": "failed"}
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("cancelled", "failed")


def test_a_step_whose_outcome_is_unknown_is_undone_without_an_incident():
    run = held_and_added(submit())
    aborted = run.failed("add", "WV-TASK-AMBIGUOUS", effect="unknown")
    assert [task.node_id for task in tasks(aborted)] == ["add#undo"]
    assert run.state.incidents == {} and run.state.status == "waiting"


def test_effects_fold_over_attempts_and_never_return_to_none():
    run = held_and_added(submit())
    run.send("task_failed", node_id="add", generation=1, output={"code": "TIMEOUT"}, effect="unknown")
    run.send("recovery_scheduled", node_id="add", generation=1, next_attempt_at=run.now.isoformat(), effect="unknown")
    aborted = run.failed("add", "VALIDATION", effect="none", generation=2)
    assert (run.txn().participants[1].effect, run.txn().participants[1].outcome) == ("unknown", "unknown")
    assert [task.node_id for task in tasks(aborted)] == ["add#undo"]


def test_an_undo_that_cannot_finish_puts_the_transaction_in_doubt_at_its_own_key():
    run = held_and_added(submit())
    run.failed("add", effect="unknown")
    run.send("task_failed", node_id="add#undo", generation=10, output={"code": "UNAVAILABLE"})
    assert run.state.incidents["add#undo:10"].code == "WV-TASK-FAILED"
    assert run.txn().participants[1].attempts == 10
    stuck = run.failed("add#undo", "UNAVAILABLE", generation=10)
    assert tasks(stuck) == []
    incident = run.state.incidents["add#undo:10"]
    assert (incident.code, incident.origin_code) == ("WV-TXN-UNDO-STUCK", "UNAVAILABLE")
    assert list(run.state.incidents) == ["add#undo:10"]
    assert (run.txn().status, run.txn().incident, run.state.status) == ("in_doubt", "add#undo:10", "suspended")
    assert run.statuses()["add"] == "undo_stuck" and run.statuses()["hold"] == "done"


def test_nothing_but_a_resolution_moves_a_transaction_out_of_doubt():
    run = held_and_added(submit())
    run.failed("add", effect="unknown")
    run.failed("add#undo", "UNAVAILABLE", generation=10)
    stuck = run.state.model_copy(deep=True)
    later = (run.now + timedelta(minutes=5)).isoformat()
    events = [
        ("recovery_scheduled", {"node_id": "add#undo", "generation": 10, "code": None, "next_attempt_at": later}),
        ("task_failed", {"node_id": "add#undo", "generation": 10, "output": {"code": "UNAVAILABLE"}}),
        ("task_completed", {"node_id": "add#undo", "generation": 11, "output": {}}),
        ("step_failed", {"node_id": "add#undo", "generation": 11, "code": "UNAVAILABLE", "effect": "unknown"}),
        ("incident_opened", {"node_id": "add#undo", "generation": 10, "code": "WV-TASK-DEADLINE"}),
        ("paused", {"reason": "p", "control_revision": stuck.control_revision + 1}),
        ("resumed", {"reason": "r", "control_revision": stuck.control_revision + 1}),
        ("cancelled", {"reason": "c"}),
        ("incident_resolved", {"incident_key": "add#undo:10", "kind": "retry_safe", "node_id": "add#undo"}),
        ("incident_resolved", {"incident_key": "add#undo:10", "kind": "terminate", "node_id": "add#undo"}),
    ]
    for kind, data in events:
        run.state = stuck.model_copy(deep=True)
        run.send(kind, **data)
        incident = run.state.incidents.get("add#undo:10")
        assert incident is not None and incident.code == "WV-TXN-UNDO-STUCK", kind
        assert run.txn().status == "in_doubt" and run.state.status not in {"cancelled", "failed", "succeeded"}, kind


def test_the_step_that_cant_be_undone_decides_the_direction():
    def deciding():
        run = held_and_added(submit())
        run.done("add", {"id": "a-1"})
        assert run.txn().status == "deciding"
        return run

    run = deciding()
    [undo] = tasks(run.failed("submit", "VALIDATION", effect="none"))
    assert (run.txn().status, undo.node_id) == ("compensating", "add#undo")

    run = deciding()
    unknown = run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
    assert tasks(unknown) == []
    assert (run.txn().status, run.state.incidents["submit:3"].code) == ("in_doubt", "WV-TXN-PIVOT-UNKNOWN")


def test_a_cancel_while_the_step_that_cant_be_undone_runs_waits_for_its_result():
    run = held_and_added(submit())
    run.done("add", {"id": "a-1"})
    waiting = run.send("cancelled", reason="stop")
    assert tasks(waiting) == [] and not controls(waiting, "terminate")
    assert run.txn().pending_abort.code == "WV-TXN-RUN-CANCELLED"
    assert (run.state.ending.status, run.state.status) == ("cancelled", "waiting")
    [confirm] = tasks(run.done("submit", {"orderId": "o-1"}))
    assert confirm.node_id == "hold#confirm" and run.txn().pending_abort is None
    ended = run.done("hold#confirm", {})
    assert (run.txn().status, run.state.status) == ("completed", "cancelled")
    assert controls(ended, "terminate")

    run = held_and_added(submit())
    run.done("add", {"id": "a-1"})
    run.send("cancelled", reason="stop")
    [undo] = tasks(run.failed("submit", "VALIDATION", effect="none"))
    assert (undo.node_id, run.txn().cause.code) == ("add#undo", "WV-TXN-RUN-CANCELLED")
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("compensated", "cancelled")


def test_the_step_that_cant_be_undone_is_not_dispatched_when_a_hold_would_expire_first():
    run = Run(flow(hold(holdSeconds=600), add(), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    # 600 s hold, 60 s order step, 30 s margin: after 520 s the hold no longer covers the order.
    run.later(520)
    [undo] = tasks(run.done("add", {"id": "a-1"}))
    assert undo.node_id == "add#undo"
    assert run.txn().cause.model_dump() == {
        "code": "WV-TXN-HOLD-INSUFFICIENT",
        "instance_key": "submit",
        "effect": "none",
    }
    assert "submit" not in run.statuses()


def test_a_hold_deadline_undoes_the_transaction_before_reservations_expire():
    run = Run(flow(hold(holdSeconds=600), add(), submit()))
    started = run.start()
    [due] = deadlines(started)
    assert due.deadline == run.now + timedelta(seconds=570)
    run.done("hold", {"holdId": "h-1"})
    expired = run.fire("move#hold")
    # add is still in flight: it settles first, then the reservation is released.
    assert tasks(expired) == [] and controls(expired, "settle") == ["add"]
    assert run.txn().cause.model_dump() == {"code": "WV-TXN-HOLD-EXPIRED", "instance_key": "move"}
    [release] = tasks(run.failed("add", "VALIDATION", effect="none"))
    assert release.node_id == "hold#cancel"


def test_a_later_reservation_that_holds_for_less_time_moves_the_hold_deadline_earlier():
    run = Run(flow(hold("first", holdSeconds=600), hold("second", holdSeconds=120), add(), submit()))
    [first] = deadlines(run.start())
    assert first.deadline == run.now + timedelta(seconds=570)
    run.later(10)
    [second] = deadlines(run.done("first", {"holdId": "h-1"}))
    # The earliest expiry wins: the second reservation's 120 s, less the 30 s margin, from its dispatch.
    assert (second.node_id, second.deadline) == ("move#hold", run.now + timedelta(seconds=90))
    assert run.txn().hold_by == second.deadline
    # A later reservation that holds longer never moves the deadline back.
    assert deadlines(run.later(5).done("second", {"holdId": "h-2"})) == []
    expired = run.fire("move#hold")
    assert run.txn().cause.model_dump() == {"code": "WV-TXN-HOLD-EXPIRED", "instance_key": "move"}
    assert controls(expired, "settle") == ["add"]


def test_steps_still_running_are_settled_first_and_a_settle_deadline_bounds_the_wait():
    run = Run(flow(hold(), branches(), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    aborted = run.failed("add", effect="unknown")
    # add2 is still in flight: it is made settlement work and nothing is undone yet.
    assert tasks(aborted) == []
    assert controls(aborted, "settle") == ["add2"]
    [settle] = [d for d in deadlines(aborted) if d.node_id == "move#settle"]
    assert settle.deadline == run.now + timedelta(seconds=60)
    timed_out = run.fire("move#settle")
    assert run.state.incidents["move:0"].code == "WV-TXN-SETTLE-TIMEOUT"
    assert tasks(timed_out) == [] and run.txn().status == "in_doubt"


def test_a_step_that_settles_in_time_lets_the_undo_start():
    run = Run(flow(hold(), branches(), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="none")
    [undo] = tasks(run.done("add2", {"id": "a-2"}))
    assert undo.node_id == "add2#undo"
    stale = run.fire("move#settle")
    assert tasks(stale) == [] and run.txn().status == "compensating"


def test_a_fail_step_undoes_the_transaction_and_fails_the_block_with_its_own_code():
    run = held_and_added(
        {"id": "stop", "kind": "fail", "code": "OUT_OF_RANGE", "message": "The quantity is out of range."}
    )
    [undo] = tasks(run.done("add", {"id": "a-1"}))
    assert undo.node_id == "add#undo"
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    assert (run.state.status, run.state.output) == (
        "failed",
        {"code": "OUT_OF_RANGE", "message": "The quantity is out of range."},
    )


def test_a_step_that_fails_to_evaluate_inside_a_transaction_undoes_it_without_an_incident():
    run = held_and_added({"id": "shape", "kind": "transform", "value": {"ref": "/input/items/3"}}, submit())
    aborted = run.done("add", {"id": "a-1"})
    assert [task.node_id for task in tasks(aborted)] == ["add#undo"]
    assert run.txn().cause.model_dump() == {"code": "WV-EXPR-MISSING", "instance_key": "shape", "effect": "none"}
    assert run.state.incidents == {} and run.statuses()["add"] == "undoing"


def test_steps_after_the_point_of_no_return_wait_for_every_confirm():
    run = held_and_added(submit(), notify())
    run.done("add", {"id": "a-1"})
    decided = run.done("submit", {"orderId": "o-1"})
    assert [task.node_id for task in tasks(decided)] == ["hold#confirm"]
    assert run.txn().resume == ["notify"]
    [after] = tasks(run.done("hold#confirm", {}))
    assert (after.node_id, after.settlement, after.transaction.phase) == ("notify", False, "forward")
    stuck = run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    assert run.state.incidents["notify:4"].code == "WV-TXN-AFTER-PIVOT-STUCK" and tasks(stuck) == []


def test_a_confirm_that_cannot_finish_never_turns_the_transaction_back():
    run = held_and_added(submit())
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    stuck = run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert tasks(stuck) == []
    assert (run.txn().status, run.state.incidents["hold#confirm:2"].code) == ("in_doubt", "WV-TXN-CONFIRM-STUCK")


def test_a_called_participant_is_undone_by_its_called_reversal():
    run = Run(flow(hold(), reverse_by_call("add"), add("add2"), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    [undo2] = tasks(run.failed("add2", effect="unknown"))
    [call] = calls(run.done(undo2.node_id, {}))
    assert (call.node_id, call.on_failure, call.settlement, call.input) == (
        "add#undo",
        "continue",
        True,
        {"reference": "M-1", "addOperation": f"{RUN}:add"},
    )
    [release] = tasks(run.called("add#undo", "succeeded", None))
    assert release.node_id == "hold#cancel"


def test_a_called_reversal_that_fails_is_stuck_and_never_rerun_on_its_own():
    run = Run(flow(hold(), reverse_by_call("add"), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="unknown")
    stuck = run.called("add#undo", "failed", {"code": "BROKEN", "message": "It broke."})
    assert calls(stuck) == [] and run.state.incidents["add#undo:0"].code == "WV-TXN-UNDO-STUCK"


def deciding_by_call(*, cancel: bool = False) -> tuple[Run, object]:
    run = Run(flow(hold(holdSeconds=900), add(), submit_by_call()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    dispatched = run.done("add", {"id": "a-1"})
    assert [c.node_id for c in calls(dispatched)] == ["submit"] and run.txn().status == "deciding"
    if cancel:
        run.send("cancelled", reason="stop")
    [bound] = [d for d in deadlines(dispatched) if d.node_id == "move#decide"]
    return run, bound


def test_a_called_step_that_cant_be_undone_and_never_started_is_cancelled_when_its_time_is_up():
    run, bound = deciding_by_call()
    # From dispatch: the called workflow's own 300 s, plus the 30 s margin.
    assert bound.deadline == run.now + timedelta(seconds=330) and run.txn().decide_by == bound.deadline
    fired = run.fire("move#decide", started=False)
    # No child ever started: the call is cancelled with nothing applied, and that outcome sends it back.
    assert controls(fired, "settle") == ["submit"] and run.txn().status == "deciding"
    [undo] = tasks(run.called("submit", "cancelled", {"code": "WV-TXN-ROLLED-BACK"}, effect="none"))
    assert undo.node_id == "add#undo" and run.txn().status == "compensating"


def test_a_called_step_that_cant_be_undone_and_is_still_running_when_its_time_is_up_is_in_doubt():
    run, _ = deciding_by_call(cancel=True)
    fired = run.fire("move#decide", started=True)
    assert run.txn().status == "in_doubt" and tasks(fired) == [] and controls(fired, "settle") == []
    assert run.state.incidents["submit:0"].code == "WV-TXN-PIVOT-UNKNOWN"
    # The cancel that waited for the decision stays recorded; only a person moves the transaction on.
    assert run.state.ending is not None and run.state.status not in {"cancelled", "succeeded"}


def test_a_decided_transaction_ignores_its_bound_while_deciding():
    run, _ = deciding_by_call()
    run.called("submit", "succeeded", {"orderId": "o-1"})
    assert run.txn().status == "committing"
    fired = run.fire("move#decide", started=True)
    assert run.txn().status == "committing" and tasks(fired) == [] and run.state.incidents == {}


def test_a_called_step_still_running_gets_its_full_time_limit_from_the_rollback_to_settle():
    elsewhere = {
        "id": "elsewhere",
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "elsewhere"}}},
        "compensate": add()["compensate"],
    }
    run = Run(flow(hold(holdSeconds=900), elsewhere, submit()))
    run.start()
    dispatched = run.done("hold", {"holdId": "h-1"})
    assert [c.node_id for c in calls(dispatched)] == ["elsewhere"]
    # Its child may have started only now: it has its whole 300 s from here, plus the margin.
    run.later(200)
    aborted = run.send("cancelled", reason="stop")
    [settle] = [d for d in deadlines(aborted) if d.node_id == "move#settle"]
    assert settle.deadline == run.now + timedelta(seconds=330)


def test_a_called_step_that_continues_after_its_child_failed_is_recorded_as_it_ended():
    elsewhere = {
        "id": "elsewhere",
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "elsewhere"}}},
        "onFailure": "continue",
        "compensate": add()["compensate"],
    }
    run = Run(flow(hold(holdSeconds=900), elsewhere, submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    # The body goes on with the child's failure as data, but the step is not recorded as done.
    [pivot] = tasks(run.called("elsewhere", "failed", {"code": "BROKEN", "message": "It broke."}, effect="unknown"))
    assert pivot.node_id == "submit"
    record = next(item for item in run.txn().participants if item.key == "elsewhere")
    assert (record.status, record.outcome, record.effect) == ("unknown", "unknown", "unknown")
    # A child that applied nothing is recorded as failed with nothing to undo.
    other = Run(flow(hold(holdSeconds=900), elsewhere, submit()))
    other.start()
    other.done("hold", {"holdId": "h-1"})
    other.called("elsewhere", "failed", {"code": "BROKEN", "message": "It broke."}, effect="none")
    record = next(item for item in other.txn().participants if item.key == "elsewhere")
    assert (record.status, record.effect) == ("failed", "none")


def test_a_second_confirm_that_cannot_finish_keeps_the_first_incident_as_the_transactions():
    run = Run(flow(hold("first"), hold("second"), add(), submit()))
    run.start()
    run.done("first", {"holdId": "h-1"})
    run.done("second", {"holdId": "h-2"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.failed("first#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    run.failed("second#confirm", "HOLD_EXPIRED", effect="none", generation=3)
    assert run.txn().incident == "first#confirm:2"
    assert {key: item.code for key, item in run.state.incidents.items()} == {
        "first#confirm:2": "WV-TXN-CONFIRM-STUCK",
        "second#confirm:3": "WV-TXN-CONFIRM-STUCK",
    }


def test_a_child_started_late_for_a_step_that_cant_be_undone_ends_by_its_callers_deadline():
    run = Run(compiled([transaction([hold(), add(), submit()])], timeout=300))
    cap = run.now + timedelta(seconds=40)
    started = run.send(
        "started", admission_policy="classified-v1", origin="manual", run_id=str(RUN), not_after=cap.isoformat()
    )
    assert [(d.node_id, d.deadline) for d in deadlines(started) if d.node_id == "@run"] == [("@run", cap)]
    later = Run(compiled([transaction([hold(), add(), submit()])], timeout=30))
    started = later.send(
        "started", admission_policy="classified-v1", origin="manual", run_id=str(RUN), not_after=cap.isoformat()
    )
    # Its own limit ends it earlier still.
    assert [d.deadline for d in deadlines(started) if d.node_id == "@run"] == [later.now + timedelta(seconds=30)]


def report(source: str) -> dict:
    return {"id": "report", "kind": "transform", "value": {"ref": source}}


def test_a_rolled_back_transaction_with_a_setting_continues_with_its_error():
    flow_ = compiled(
        [transaction([hold(), add(), submit()], onError="continue"), report("/steps/move/error/code")],
        output={"ref": "/steps/report/output"},
    )
    run = Run(flow_)
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "VALIDATION", effect="none")
    ended = run.done("hold#cancel", {})
    assert run.txn().status == "cancelled"
    assert [(change.node_id, change.status) for change in ended.steps if change.status == "failed"] == [
        ("move", "failed")
    ]
    assert run.state.steps["move"] == {
        "output": None,
        "error": {
            "code": "WV-TXN-ROLLED-BACK",
            "message": STEP_ERROR_MESSAGES["WV-TXN-ROLLED-BACK"],
            "outcome": "failed",
            "attempts": 1,
            "instanceKey": "move",
        },
    }
    assert (run.state.status, run.state.output) == ("succeeded", "WV-TXN-ROLLED-BACK")
    assert run.state.incidents == {} and run.state.incident is None


def test_a_rolled_back_transaction_with_an_error_path_runs_it_and_its_output_becomes_the_blocks():
    block = transaction(
        [hold(), add(), submit()],
        onError="errorOutput",
        errorPath={"steps": [report("/steps/move/error/outcome")], "output": {"literal": "undone"}},
    )
    run = Run(compiled([block], output={"ref": "/steps/move/output"}))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "VALIDATION", effect="none")
    run.done("hold#cancel", {})
    assert (run.state.status, run.state.output) == ("succeeded", "undone")
    assert run.state.steps["move"]["error"]["code"] == "WV-TXN-ROLLED-BACK"
    assert all(branch.name != "errorPath" for branch in run.state.branches.values())
    assert all(join.mode != "error" for join in run.state.joins.values())


def test_a_transaction_that_succeeds_never_runs_its_error_path():
    block = transaction(
        [hold(), add(), submit()],
        onError="errorOutput",
        errorPath={"steps": [], "output": {"literal": "undone"}},
    )
    run = Run(compiled([block], output={"ref": "/steps/move/output"}))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("completed", "succeeded", "moved")
    assert "error" not in run.state.steps["move"]


def test_a_fail_step_still_fails_the_run_when_its_transaction_has_a_setting():
    stop = {"id": "stop", "kind": "fail", "code": "OUT_OF_RANGE", "message": "The quantity is out of range."}
    run = Run(flow(hold(), add(), stop, onError="continue"))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    assert (run.state.status, run.state.output) == (
        "failed",
        {"code": "OUT_OF_RANGE", "message": "The quantity is out of range."},
    )


def test_a_definite_failure_of_a_participant_with_a_setting_does_not_abort_its_transaction():
    run = Run(flow(hold(), add(onError="continue"), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    went_on = run.failed("add", "VALIDATION", effect="none")
    assert [task.node_id for task in tasks(went_on)] == ["submit"]
    assert [(change.node_id, change.status) for change in went_on.steps][:1] == [("add", "failed")]
    assert run.statuses() == {"hold": "done", "add": "failed", "submit": "dispatched"}
    assert run.txn().status == "deciding"
    [confirm] = tasks(run.done("submit", {"orderId": "o-1"}))
    assert confirm.node_id == "hold#confirm"
    run.done("hold#confirm", {})
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")
    assert run.state.steps == {"move": {"output": "moved"}}


def test_a_failure_that_may_have_applied_still_undoes_a_transaction_whose_step_has_a_setting():
    run = Run(flow(hold(), add(onError="continue"), submit()))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    aborted = run.failed("add", "UNAVAILABLE", effect="unknown")
    assert [task.node_id for task in tasks(aborted)] == ["add#undo"]
    assert run.txn().status == "compensating"


def test_a_step_that_cant_be_undone_and_failed_definitely_reopens_its_transaction_under_its_setting():
    run = Run(flow(hold(), add(), {**submit(), "onError": "continue"}))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    assert run.txn().status == "deciding"
    went_on = run.failed("submit", "VALIDATION", effect="none")
    # Nothing was applied, so the body ends without a decision: the transaction commits what it holds.
    [confirm] = tasks(went_on)
    assert confirm.node_id == "hold#confirm"
    assert run.statuses() == {"hold": "confirming", "add": "done", "submit": "failed"}
    unknown = Run(flow(hold(), add(), {**submit(), "onError": "continue"}))
    unknown.start()
    unknown.done("hold", {"holdId": "h-1"})
    unknown.done("add", {"id": "a-1"})
    unknown.failed("submit", "TIMEOUT", effect="unknown", generation=2)
    assert (unknown.txn().status, unknown.state.incidents["submit:2"].code) == ("in_doubt", "WV-TXN-PIVOT-UNKNOWN")


def test_a_step_with_a_setting_whose_evaluation_fails_inside_a_transaction_continues_without_undoing_it():
    shape = {"id": "shape", "kind": "transform", "value": {"ref": "/input/items/3"}, "onError": "continue"}
    run = held_and_added(shape, submit())
    went_on = run.done("add", {"id": "a-1"})
    assert [task.node_id for task in tasks(went_on)] == ["submit"]
    assert run.state.incidents == {} and run.txn().status == "deciding"
