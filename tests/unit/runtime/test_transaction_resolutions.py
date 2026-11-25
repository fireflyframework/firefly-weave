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

"""Resolving a transaction in doubt in the kernel: retry, mark as reconciled, it happened or not, force end."""

import json
from pathlib import Path

import pytest
from transaction_support import (
    INPUT,
    START,
    UNSET,
    Run,
    add,
    after_pivot_pair,
    attempt_failed,
    calls,
    compiled,
    completed_once,
    controls,
    deadlines,
    finished,
    hold,
    lookup,
    notify,
    one_stuck,
    parallel,
    record,
    resolve,
    submit,
    submit_by_call,
    tasks,
    transaction,
    two_stuck,
)

from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES, unsaid_message
from firefly_weave.runtime.kernel import KernelError, check_reconciliation, unsaid_steps, workflow
from firefly_weave.runtime.models import ParticipantRecord, TransactionRecord
from firefly_weave.runtime.transactions import doubted_steps, shapes

ROLLED_BACK = {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}


def reads_the_block():
    """A transaction whose output is the reservation, and a step after it that reads that output."""
    block = transaction([hold(), add(), submit()], output={"ref": "/steps/hold/output"})
    after = {"id": "after", "kind": "transform", "value": {"ref": "/steps/move/output/holdId"}}
    return compiled([block, after], output={"ref": "/steps/after/output"})


def undo_stuck():
    """A transaction whose undo of ``add`` couldn't finish after ten attempts, once ``add2`` failed and was undone."""
    run = Run(compiled([transaction([hold(), add(), add("add2"), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    issued = tasks(run.done("add", {"id": "a-1"}))
    failing = issued[0].node_id if issued else "add"
    [undo] = tasks(run.failed(failing, "UNAVAILABLE", effect="unknown"))
    while undo.node_id != "add#undo":
        [undo] = tasks(run.done(undo.node_id, {}))
    run.failed("add#undo", "UNAVAILABLE", generation=10)
    assert run.txn().status == "in_doubt"
    return run


def pivot_unknown(*after):
    run = Run(compiled([transaction([hold(), add(), submit(), *after])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
    assert run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN"
    return run


def confirm_stuck(flow=None):
    run = Run(flow or compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert run.state.incidents["hold#confirm:2"].code == "WV-TXN-CONFIRM-STUCK"
    return run


def test_retry_the_undo_runs_the_same_phase_again_and_the_rollback_resumes_where_it_stopped():
    run = undo_stuck()
    retried = resolve(run, "add#undo:10", "retry_safe")
    # The task row is made ready again by the service, so the same operation key is used: no new intent.
    assert tasks(retried) == []
    assert "add#undo" in run.state.active and run.state.incidents == {}
    assert (run.txn().status, run.txn().incident, run.statuses()["add"]) == ("compensating", None, "undoing")
    [release] = tasks(run.done("add#undo", {}, generation=11))
    assert release.node_id == "hold#cancel"
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("compensated", "failed", ROLLED_BACK)


def test_mark_as_reconciled_as_undone_ends_the_block_failed_without_running_the_rest():
    run = undo_stuck()
    ended = resolve(
        run,
        "add#undo:10",
        "accept_reconciled_result",
        {"as": "compensated", "participants": [{"instance_key": "add", "outcome": "undone"}]},
    )
    # The reservation's release is not sent: the person took over what was left.
    assert tasks(ended) == []
    assert (run.txn().status, run.txn().reconciled_as) == ("reconciled", "compensated")
    assert run.statuses() == {"hold": "done", "add": "undone", "add2": "undone"}
    assert (run.state.status, run.state.output, run.state.incidents) == ("failed", ROLLED_BACK, {})


def test_mark_as_reconciled_as_completed_needs_the_block_output_and_later_steps_read_it():
    run = confirm_stuck(reads_the_block())
    record = {
        "as": "completed",
        "participants": [{"instance_key": "hold", "outcome": "confirmed"}],
        "blockOutput": {"holdId": "h-reconciled"},
    }
    resolve(run, "hold#confirm:2", "accept_reconciled_result", record)
    assert (run.txn().status, run.txn().reconciled_as) == ("reconciled", "completed")
    assert run.statuses()["hold"] == "confirmed"
    assert (run.state.status, run.state.output) == ("succeeded", "h-reconciled")


@pytest.mark.parametrize(
    "record",
    [
        {"as": "completed", "participants": [{"instance_key": "hold", "outcome": "confirmed"}]},
        {"as": "completed", "participants": [{"instance_key": "hold", "outcome": "confirmed"}], "blockOutput": 5},
        {"as": "compensated", "participants": [{"instance_key": "hold", "outcome": "undone"}], "blockOutput": {}},
        {"as": "completed", "participants": [], "blockOutput": {"holdId": "h"}},
        {"as": "completed", "participants": [{"instance_key": "add", "outcome": "confirmed"}], "blockOutput": {}},
        {"as": "done", "participants": [{"instance_key": "hold", "outcome": "confirmed"}]},
        {"as": "completed", "participants": [{"instance_key": "nowhere", "outcome": "done"}], "blockOutput": {}},
        # Only a reservation can be confirmed; everything else about this record fits.
        {
            "as": "completed",
            "participants": [
                {"instance_key": "hold", "outcome": "confirmed"},
                {"instance_key": "add", "outcome": "confirmed"},
            ],
            "blockOutput": {"holdId": "h"},
        },
        "completed",
    ],
)
def test_a_reconciliation_that_does_not_fit_the_transaction_changes_nothing(record):
    run = confirm_stuck(reads_the_block())
    before = run.state.model_copy(deep=True)
    refused = resolve(run, "hold#confirm:2", "accept_reconciled_result", record)
    assert tasks(refused) == [] and refused.commands == []
    assert run.state.incidents == before.incidents and run.txn() == before.transactions["move"]


def test_it_happened_confirms_the_reservations_then_runs_the_steps_after_the_point_of_no_return():
    run = pivot_unknown(notify())
    went_ahead = resolve(run, "submit:3", "accept_reconciled_result", {"orderId": "o-found"})
    [confirm] = tasks(went_ahead)
    assert confirm.node_id == "hold#confirm"
    assert (run.txn().status, run.statuses()["submit"], run.txn().resume) == ("committing", "done", ["notify"])
    [after] = tasks(run.done("hold#confirm", {}))
    assert after.node_id == "notify"
    run.done("notify", {})
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


def test_it_happened_gives_a_called_step_that_cant_be_undone_the_output_a_person_found():
    # The called workflow was still running when its time was up; a person found it went through.
    block = transaction([hold(holdSeconds=900), add(), submit_by_call()], output={"ref": "/steps/submit/output"})
    run = Run(compiled([block], output={"ref": "/steps/move/output"}))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    assert [call.node_id for call in calls(run.done("add", {"id": "a-1"}))] == ["submit"]
    run.fire("move#decide", started=True)
    assert run.state.incidents["submit:0"].code == "WV-TXN-PIVOT-UNKNOWN"
    [confirm] = tasks(resolve(run, "submit:0", "accept_reconciled_result", {"orderId": "o-found"}))
    assert confirm.node_id == "hold#confirm"
    assert (run.txn().status, run.statuses()["submit"]) == ("committing", "done")
    run.done("hold#confirm", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("completed", "succeeded", {"orderId": "o-found"})


def test_it_happened_needs_an_output_the_step_could_have_returned():
    run = pivot_unknown()
    resolve(run, "submit:3", "accept_reconciled_result", {"unexpected": True})
    assert run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN" and run.txn().status == "in_doubt"


def test_it_didnt_happen_undoes_what_was_done_before_it():
    run = pivot_unknown()
    [undo] = tasks(resolve(run, "submit:3", "accept_reconciled_failure"))
    assert undo.node_id == "add#undo"
    assert (run.txn().status, run.statuses()["submit"]) == ("compensating", "failed")
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("compensated", "failed")


def test_it_didnt_happen_goes_back_even_when_the_step_has_an_on_error_setting():
    # A person's answer about the step that can't be undone belongs to its transaction; the step's setting never
    # takes it, so nothing reads an error at the step and what was done before it is undone.
    run = Run(compiled([transaction([hold(), add(), {**submit(), "onError": "continue"}])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
    assert run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN"
    went_back = resolve(run, "submit:3", "accept_reconciled_failure")
    assert [task.node_id for task in tasks(went_back)] == ["add#undo"]
    assert (run.state.incidents, run.txn().status, run.statuses()["submit"]) == ({}, "compensating", "failed")
    assert "submit" not in run.state.steps
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status, run.state.output) == ("compensated", "failed", ROLLED_BACK)


def test_only_the_step_that_cant_be_undone_can_be_said_to_have_happened_or_not():
    run = pivot_unknown()
    resolve(run, "submit:3", "retry_safe")
    assert run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN"
    run = undo_stuck()
    resolve(run, "add#undo:10", "accept_reconciled_failure")
    assert run.state.incidents["add#undo:10"].code == "WV-TXN-UNDO-STUCK"


def test_force_end_stops_the_transaction_and_ends_the_run_canceled():
    run = undo_stuck()
    ended = resolve(run, "add#undo:10", "terminate")
    assert tasks(ended) == [] and controls(ended, "terminate")
    assert (run.txn().status, run.state.status, run.state.incidents) == ("force_ended", "cancelled", {})


def test_retry_after_a_settle_timeout_waits_again_with_a_new_deadline():
    branches = {
        "id": "branches",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [add("add")], "output": {"literal": None}},
            "b": {"steps": [add("add2")], "output": {"literal": None}},
        },
    }
    run = Run(compiled([transaction([hold(), branches, submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="unknown")
    run.fire("move#settle")
    assert run.state.incidents["move:0"].code == "WV-TXN-SETTLE-TIMEOUT"
    run.later(5)
    again = resolve(run, "move:0", "retry_safe")
    # The first settle deadline was consumed when it fired: the new one has a key of its own, so it fires again.
    assert [d.node_id for d in deadlines(again)] == ["move#settle.1"]
    [settle] = deadlines(again)
    assert settle.deadline > run.now and (run.txn().status, run.txn().generation) == ("compensating", 1)
    [undo] = tasks(run.done("add2", {"id": "a-2"}))
    assert undo.node_id == "add2#undo"


def test_a_settle_deadline_armed_again_by_a_retry_fires_again():
    branches = {
        "id": "branches",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [add("add")], "output": {"literal": None}},
            "b": {"steps": [add("add2")], "output": {"literal": None}},
        },
    }
    run = Run(compiled([transaction([hold(), branches, submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="unknown")
    run.fire("move#settle")
    resolve(run, "move:0", "retry_safe")
    run.fire("move#settle.1")
    assert run.state.incidents["move:1"].code == "WV-TXN-SETTLE-TIMEOUT"
    assert (run.txn().status, run.txn().incident) == ("in_doubt", "move:1")


def test_retry_a_step_after_the_point_of_no_return_runs_it_again_and_the_transaction_completes():
    run = pivot_unknown(notify())
    resolve(run, "submit:3", "accept_reconciled_result", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    assert run.state.incidents["notify:4"].code == "WV-TXN-AFTER-PIVOT-STUCK"
    resolve(run, "notify:4", "retry_safe")
    assert "notify" in run.state.active and run.txn().status == "committing"
    run.done("notify", {}, generation=5)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


def test_a_late_result_of_work_a_person_ended_changes_nothing():
    pause = {"id": "pause", "kind": "wait", "durationSeconds": 60}
    run = Run(compiled([transaction([hold(), hold("hold2"), add(), submit()]), pause]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("hold2", {"holdId": "h-2"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert "hold2#confirm" in run.state.active
    record = {
        "as": "completed",
        "participants": [{"instance_key": "hold", "outcome": "confirmed"}],
        "blockOutput": "moved",
    }
    ended = resolve(run, "hold#confirm:2", "accept_reconciled_result", record)
    assert controls(ended, "settle") == ["hold2#confirm"] and run.state.active == ["pause"]
    late = run.done("hold2#confirm", {})
    assert late.steps == [] and run.state.incidents == {} and run.state.active == ["pause"]


def waits_for_a_failure(run: Run, key: str) -> bool:
    from firefly_weave.operations.incidents import failure_in_progress

    return failure_in_progress(workflow(run.flow), run.state, key)


def test_a_reconcile_waits_for_the_failed_attempt_of_a_step_of_its_transaction_in_its_own_iteration():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "maxItems": 2,
        "concurrency": 2,
        "body": {"steps": [transaction([hold(), add()])], "output": {"literal": None}},
    }
    run = Run(compiled([each]))
    run.start()
    assert not waits_for_a_failure(run, "move[1]")
    attempt_failed(run, "hold[1]")
    assert waits_for_a_failure(run, "move[1]")
    assert not waits_for_a_failure(run, "move[0]")


def test_a_reconcile_waits_for_the_failed_attempt_of_a_confirm_and_not_for_the_confirm_already_stuck():
    run = Run(compiled([transaction([hold(), hold("hold2"), add(), submit()])]))
    run.start()
    for key, output in (("hold", {"holdId": "h-1"}), ("hold2", {"holdId": "h-2"}), ("add", {"id": "a-1"})):
        run.done(key, output)
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert not waits_for_a_failure(run, "move")
    attempt_failed(run, "hold2#confirm")
    assert waits_for_a_failure(run, "move")


@pytest.mark.parametrize("inside", [True, False])
def test_a_reconcile_waits_for_the_failed_attempt_of_a_step_that_changes_nothing_only_inside_its_transaction(inside):
    read = {"steps": [lookup()], "output": {"literal": None}}
    moves = {"steps": [transaction([hold(), add()])], "output": {"literal": None}}
    inner = {"steps": [hold(), add()], "output": {"literal": None}}
    fork = {
        "id": "fork",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {"a": inner if inside else moves, "b": read},
    }
    run = Run(compiled([transaction([fork])] if inside else [fork]))
    run.start()
    attempt_failed(run, "lookup")
    assert waits_for_a_failure(run, "move") is inside


def test_a_reconcile_never_waits_for_the_failed_attempt_of_a_step_the_rollback_stopped():
    moves = {"steps": [transaction([hold(holdSeconds=600), lookup(), add()])], "output": {"literal": None}}
    wait = {"steps": [{"id": "pause", "kind": "wait", "durationSeconds": 3600}], "output": {"literal": None}}
    run = Run(compiled([{"id": "fork", "kind": "parallel", "concurrency": 2, "branches": {"a": moves, "b": wait}}]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    attempt_failed(run, "lookup")
    assert waits_for_a_failure(run, "move")
    run.fire("move#hold")
    run.failed("hold#cancel", "REJECTED", effect="none", generation=10)
    # Recovery never decides the stopped step, so its incident stays open while the other branch still waits.
    assert (run.txn().status, run.state.active) == ("in_doubt", ["pause"])
    assert sorted(run.state.incidents) == ["hold#cancel:10", "lookup:1"]
    assert not waits_for_a_failure(run, "move")


def test_a_reconcile_never_waits_for_an_incident_that_recovery_leaves_to_a_person():
    branches = {
        "a": {"steps": [notify()], "output": {"literal": None}},
        "b": {"steps": [lookup()], "output": {"literal": None}},
    }
    after = {"id": "fork", "kind": "parallel", "concurrency": 2, "branches": branches}
    run = Run(compiled([transaction([hold(), add(), submit(), after])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    # The read's output was refused: that incident is final, and the step stays active behind it.
    run.send("incident_opened", node_id="lookup", generation=1, code="WV-RUNTIME-OUTPUT")
    assert (run.txn().status, run.state.active) == ("in_doubt", ["lookup"])
    assert sorted(run.state.incidents) == ["lookup:1", "notify:4"]
    assert not waits_for_a_failure(run, "move")


def test_a_reservation_marked_undone_is_recorded_as_released():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "REFUSED", effect="none")
    run.failed("hold#cancel", "UNAVAILABLE", generation=10)
    assert run.state.incidents["hold#cancel:10"].code == "WV-TXN-CANCEL-STUCK"
    record = {"as": "compensated", "participants": [{"instance_key": "hold", "outcome": "undone"}]}
    resolve(run, "hold#cancel:10", "accept_reconciled_result", record)
    assert (run.txn().status, run.statuses()["hold"]) == ("reconciled", "cancelled")


CONFIRMS = ("hold", "hold2")


@pytest.mark.parametrize("results", ["between the retries", "in the order of the retries", "in the other order"])
@pytest.mark.parametrize(("first", "second"), [CONFIRMS, CONFIRMS[::-1]])
def test_retrying_one_of_two_stuck_confirms_leaves_the_other_open(first, second, results):
    run = two_stuck(run_rules=2)
    retried = resolve(run, f"{first}#confirm:2", "retry_safe")
    assert tasks(retried) == [] and run.state.active == [f"{first}#confirm"]
    assert (run.txn().status, run.txn().incident) == ("in_doubt", f"{second}#confirm:2")
    assert (run.state.status, list(run.state.incidents)) == ("suspended", [f"{second}#confirm:2"])
    if results == "between the retries":
        run.done(f"{first}#confirm", {}, generation=3)
        assert (run.txn().status, run.statuses()[first], run.txn().resume) == (
            "in_doubt",
            "confirmed",
            ["@branch:move:0"],
        )
    resolve(run, f"{second}#confirm:2", "retry_safe")
    assert (run.txn().status, run.txn().incident, run.state.incidents) == ("committing", None, {})
    left = [second] if results == "between the retries" else [first, second]
    for key in reversed(left) if results == "in the other order" else left:
        assert run.txn().status == "committing"
        run.done(f"{key}#confirm", {}, generation=3)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")
    assert completed_once(run, "@join:move") and completed_once(run, "after")


def test_a_retried_confirm_that_fails_again_stays_beside_the_other():
    run = two_stuck(run_rules=2)
    resolve(run, "hold#confirm:2", "retry_safe")
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=3)
    assert (run.txn().status, run.txn().incident) == ("in_doubt", "hold2#confirm:2")
    assert sorted(run.state.incidents) == ["hold#confirm:3", "hold2#confirm:2"]
    for key in sorted(run.state.incidents):
        resolve(run, key, "retry_safe")
        run.done(key.partition(":")[0], {}, generation=4)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


def test_a_transaction_in_doubt_names_the_same_incident_until_that_one_is_decided():
    run = Run(compiled([transaction([hold(), hold("hold2"), hold("hold3"), submit()])]))
    run.start(2)
    for key in ("hold", "hold2", "hold3"):
        run.done(key, {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    for key in ("hold2", "hold", "hold3"):
        run.failed(f"{key}#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    named = []
    for key in ("hold3#confirm:2", "hold2#confirm:2", "hold#confirm:2"):
        resolve(run, key, "retry_safe")
        named.append(run.txn().incident)
    assert named == ["hold2#confirm:2", "hold#confirm:2", None]


@pytest.mark.parametrize("key", ["hold#confirm:2", "hold2#confirm:2"])
def test_marking_either_incident_reconciled_closes_both(key):
    run = two_stuck(run_rules=2)
    ended = resolve(run, key, "accept_reconciled_result", finished(*CONFIRMS))
    assert tasks(ended) == [] and (run.txn().status, run.txn().incident) == ("reconciled", None)
    assert [run.statuses()[step] for step in CONFIRMS] == ["confirmed", "confirmed"]
    assert (run.state.status, run.state.incidents, run.state.incident) == ("succeeded", {}, None)
    assert completed_once(run, "after")


@pytest.mark.parametrize("key", ["hold#confirm:2", "hold2#confirm:2"])
def test_force_end_of_either_incident_ends_the_run(key):
    run = two_stuck(run_rules=2)
    ended = resolve(run, key, "terminate")
    assert tasks(ended) == [] and controls(ended, "terminate")
    assert (run.txn().status, run.txn().incident) == ("force_ended", None)
    assert (run.state.status, run.state.incidents) == ("cancelled", {})


def test_a_reconcile_closes_the_incidents_of_its_own_steps_only():
    block = transaction([hold(), hold("hold2"), submit()])
    run = Run(compiled([parallel(block, lookup()), lookup("after")]))
    run.start(2)
    for key, output in (("hold", {"holdId": "h-1"}), ("hold2", {"holdId": "h-2"}), ("submit", {"orderId": "o-1"})):
        run.done(key, output)
    attempt_failed(run, "lookup")
    attempt_failed(run, "hold2#confirm")
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished(*CONFIRMS))
    assert (run.txn().status, run.state.status, list(run.state.incidents)) == ("reconciled", "suspended", ["lookup:1"])
    assert run.state.incident == "WV-TASK-FAILED" and run.state.active == ["lookup"]
    run.send("recovery_scheduled", node_id="lookup", generation=1, next_attempt_at=run.now.isoformat())
    run.done("lookup", {"count": 1}, generation=2)
    assert (run.state.status, run.state.active) == ("waiting", ["after"])


def beside_another(block) -> Run:
    """The transaction beside another one in flight, which has to be undone before the run ends."""
    other = transaction([hold("hold3"), lookup("lookup3")], identifier="other")
    return Run(compiled([parallel(block, other, identifier="fork")]))


def settle_timeout(run_rules: int) -> Run:
    run = beside_another(transaction([hold(), parallel(add("add"), add("add2")), submit()]))
    run.start(run_rules)
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", effect="unknown")
    attempt_failed(run, "add2")
    run.fire("move#settle")
    assert run.state.incidents["move:0"].code == "WV-TXN-SETTLE-TIMEOUT"
    return run


def stuck_beside_a_read_the_rollback_stopped(*steps) -> Run:
    """A run of the first rules: its hold expired while a read waited for recovery, and then the rollback got stuck."""
    run = beside_another(transaction([*steps, lookup(), submit()]))
    run.start()
    for step in steps:
        run.done(step["id"], {"holdId": "h-1"} if step["id"] == "hold" else {"id": "a-1"})
    attempt_failed(run, "lookup")
    [phase] = tasks(run.fire("move#hold"))
    run.failed(phase.node_id, "REJECTED", effect="none", generation=10)
    return run


@pytest.mark.parametrize(
    ("make", "code", "undone"),
    [
        (lambda: settle_timeout(2), "WV-TXN-SETTLE-TIMEOUT", ["hold", "add", "add2"]),
        (lambda: settle_timeout(1), "WV-TXN-SETTLE-TIMEOUT", ["hold", "add", "add2"]),
        (lambda: stuck_beside_a_read_the_rollback_stopped(hold()), "WV-TXN-CANCEL-STUCK", ["hold"]),
        (lambda: stuck_beside_a_read_the_rollback_stopped(hold(), add()), "WV-TXN-UNDO-STUCK", ["hold", "add"]),
    ],
)
def test_a_reconcile_as_undone_closes_an_interim_failure_of_its_steps(make, code, undone):
    run = make()
    [key] = [key for key, item in run.state.incidents.items() if item.code == code]
    assert len(run.state.incidents) == 2
    resolve(run, key, "accept_reconciled_result", record("compensated", dict.fromkeys(undone, "undone")), rules=2)
    # The block ended as undone, so the run goes back through its other transaction, behind no barrier.
    assert (run.txn().status, run.state.incidents, run.state.status) == ("reconciled", {}, "waiting")
    run.failed("hold3", "REFUSED", effect="none")
    assert (run.txn("other").status, run.state.status, run.state.output) == ("cancelled", "failed", ROLLED_BACK)


def test_a_reconcile_closes_the_incident_of_a_refused_output_of_its_step():
    pause = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
    run = Run(compiled([transaction([hold(), parallel(submit(), lookup())]), pause]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.send("incident_opened", node_id="lookup", generation=1, code="WV-SCHEMA-SECRET_VALUE")
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold"))
    assert (run.txn().status, run.state.incidents) == ("reconciled", {})
    assert (run.state.status, run.state.active) == ("waiting", ["pause"])


def test_a_stuck_confirm_is_retried_before_the_stuck_step_beside_it():
    run = after_pivot_pair(run_rules=2)
    resolve(run, "hold#confirm:2", "retry_safe")
    assert (run.txn().status, run.txn().incident) == ("in_doubt", "lookup:3")
    run.done("hold#confirm", {}, generation=3)
    assert run.txn().status == "in_doubt" and run.txn().resume == ["@branch:both:0"]
    resolve(run, "lookup:3", "retry_safe")
    # Committing again with nothing to confirm: what waited in the body goes on.
    assert (run.txn().status, run.txn().resume, run.state.active) == ("committing", [], ["lookup"])
    run.done("lookup", {"count": 1}, generation=4)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")
    assert completed_once(run, "@branch:both:0")


def test_a_stuck_step_is_retried_before_the_stuck_confirm_beside_it_and_its_result_waits():
    run = after_pivot_pair(run_rules=2)
    resolve(run, "lookup:3", "retry_safe")
    assert (run.txn().status, run.txn().incident) == ("in_doubt", "hold#confirm:2")
    run.done("lookup", {"count": 1}, generation=4)
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]
    resolve(run, "hold#confirm:2", "retry_safe")
    assert (run.txn().status, run.state.deferred_results, run.state.incidents) == ("committing", [], {})
    run.done("hold#confirm", {}, generation=3)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


def test_a_step_retried_after_the_confirm_beside_it_finished_lets_the_body_go_on():
    run = after_pivot_pair(run_rules=2, confirm="finishes")
    assert (run.txn().status, run.txn().incident, run.txn().resume) == ("in_doubt", "lookup:3", ["@branch:both:0"])
    resolve(run, "lookup:3", "retry_safe")
    run.done("lookup", {"count": 1}, generation=4)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


def deciding(*beside) -> Run:
    """The step that can't be undone is in flight, and so are the steps beside it."""
    run = Run(compiled([transaction([hold(), parallel(submit(), *beside)])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    assert run.txn().status == "deciding"
    return run


def test_a_step_that_failed_while_the_step_beside_it_decided_is_stuck_once_that_one_succeeds():
    run = deciding(lookup(), lookup("lookup2"))
    run.failed("lookup2", "UNAVAILABLE", effect="unknown", generation=3)
    run.failed("lookup", "REJECTED", effect="none", generation=1)
    # The result of the step that can't be undone decides what becomes of them.
    assert (run.txn().status, run.state.incidents, run.state.active) == ("deciding", {}, ["submit"])
    [confirm] = tasks(run.done("submit", {"orderId": "o-1"}))
    assert confirm.node_id == "hold#confirm"
    assert {key: (item.code, item.origin_code) for key, item in run.state.incidents.items()} == {
        "lookup2:3": ("WV-TXN-AFTER-PIVOT-STUCK", "UNAVAILABLE"),
        "lookup:1": ("WV-TXN-AFTER-PIVOT-STUCK", "REJECTED"),
    }
    assert (run.txn().status, run.txn().incident, run.txn().failed_beside) == ("in_doubt", "lookup2:3", [])
    run.done("hold#confirm", {})
    for key, generation in (("lookup:1", 2), ("lookup2:3", 4)):
        resolve(run, key, "retry_safe")
        run.done(key.partition(":")[0], {"count": 1}, generation=generation)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded")


@pytest.mark.parametrize("told", ["it failed", "it didn't happen", "force end"])
def test_a_step_that_failed_while_the_step_beside_it_decided_leaves_nothing_once_that_one_goes_back(told):
    run = deciding(lookup())
    run.failed("lookup", "UNAVAILABLE", effect="unknown", generation=3)
    if told == "it failed":
        run.failed("submit", "REFUSED", effect="none")
    else:
        run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
        assert [item.node_id for item in run.txn().failed_beside] == ["lookup"]
        resolve(run, "submit:3", "terminate" if told == "force end" else "accept_reconciled_failure")
    assert (run.state.incidents, run.txn().failed_beside) == ({}, [])
    if told == "force end":
        assert (run.txn().status, run.state.status) == ("force_ended", "cancelled")
        return
    # It goes back for the failure that came first.
    assert run.txn().cause is not None and run.txn().cause.instance_key == "lookup"
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status) == ("cancelled", "failed")


def test_a_step_that_failed_while_the_step_beside_it_was_unknown_is_stuck_once_that_one_happened():
    run = deciding(lookup())
    run.failed("lookup", "UNAVAILABLE", effect="unknown", generation=3)
    run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
    assert list(run.state.incidents) == ["submit:3"]
    resolve(run, "submit:3", "accept_reconciled_result", {"orderId": "o-found"})
    assert (list(run.state.incidents), run.txn().status) == (["lookup:3"], "in_doubt")


def test_a_fail_step_reached_while_the_step_beside_it_decided_is_stuck_once_that_one_succeeds():
    stop = {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "Stopped."}
    branches = {"b0": {"steps": [submit()], "output": {"literal": None}}}
    branches["b1"] = {"steps": [lookup(), stop], "output": {"literal": None}}
    fork = {"id": "both", "kind": "parallel", "concurrency": 2, "branches": branches}
    run = Run(compiled([transaction([hold(), fork])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.done("lookup", {"count": 1})
    run.done("submit", {"orderId": "o-1"})
    assert run.state.incidents["stop:0"].code == "WV-TXN-AFTER-PIVOT-STUCK"
    resolve(run, "stop:0", "terminate")
    assert (run.txn().status, run.state.status) == ("force_ended", "cancelled")


def test_either_incident_of_a_stuck_confirm_beside_a_stuck_step_decides_the_transaction():
    for key in ("hold#confirm:2", "lookup:3"):
        run = after_pivot_pair(run_rules=2)
        resolve(run, key, "accept_reconciled_result", finished("hold"))
        assert (run.txn().status, run.state.status, run.state.incidents) == ("reconciled", "succeeded", {})


def in_a_loop(run_rules: int = 2) -> Run:
    """Three iterations, each with a transaction past its point of no return: two confirms of the second one can't
    finish, and one of the third."""
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "maxItems": 5,
        "concurrency": 5,
        "body": {"steps": [transaction([hold(), hold("hold2"), submit()])], "output": {"literal": None}},
    }
    run = Run(compiled([each]), {**INPUT, "items": [1, 2, 3]})
    run.start(run_rules)
    for index in range(3):
        run.done(f"hold[{index}]", {"holdId": "h-1"})
        run.done(f"hold2[{index}]", {"holdId": "h-2"})
        run.done(f"submit[{index}]", {"orderId": "o-1"})
    for key in ("hold[1]#confirm", "hold2[1]#confirm", "hold[2]#confirm"):
        run.failed(key, "HOLD_EXPIRED", effect="none", generation=2)
    return run


def test_only_the_transaction_of_the_incident_moves():
    run = in_a_loop()
    before = run.txn("move[2]").model_copy(deep=True)
    other = run.txn("move[0]").model_copy(deep=True)
    resolve(run, "hold2[1]#confirm:2", "accept_reconciled_result", finished("hold[1]", "hold2[1]"))
    assert run.txn("move[1]").status == "reconciled" and list(run.state.incidents) == ["hold[2]#confirm:2"]
    assert (run.txn("move[2]"), run.txn("move[0]")) == (before, other)
    assert run.state.active == ["hold[0]#confirm", "hold2[0]#confirm", "hold2[2]#confirm"]
    resolve(run, "hold[2]#confirm:2", "retry_safe")
    # The barrier lifted: the reconciled iteration was released, and the other two finish on their own.
    assert sorted(run.state.transactions) == ["move[0]", "move[2]"] and run.state.incidents == {}
    for key in ("hold[0]#confirm", "hold2[0]#confirm", "hold2[2]#confirm"):
        run.done(key, {})
    run.done("hold[2]#confirm", {}, generation=3)
    assert run.state.status == "succeeded"


def test_a_force_end_of_one_iteration_leaves_the_other_in_doubt():
    run = in_a_loop()
    resolve(run, "hold[1]#confirm:2", "terminate")
    assert run.txn("move[1]").status == "force_ended" and run.txn("move[2]").status == "in_doubt"
    assert list(run.state.incidents) == ["hold[2]#confirm:2"]
    assert run.state.ending is not None and run.state.ending.status == "cancelled"


@pytest.mark.parametrize("said", ["other", "move[0]", "hold", "", UNSET, None, 1])
def test_the_event_must_name_the_transaction_of_its_incident(said):
    run = two_stuck(run_rules=2)
    before = run.state.model_copy(deep=True)
    for key in ("hold#confirm:2", "hold2#confirm:2"):
        for kind in ("retry_safe", "terminate"):
            refused = resolve(run, key, kind, transaction=said)
            assert refused.commands == []
            assert run.state == before.model_copy(update={"accepted_sequence": run.state.accepted_sequence})


def test_a_held_run_has_no_transaction_to_name():
    def held() -> Run:
        run = Run(compiled([transaction([hold(), submit()])]))
        run.start(2)
        run.send("incident_opened", node_id="@transactions", code="WV-TXN-RUN-OVERSIZED")
        return run

    run = held()
    resolve(run, "@transactions:0", "terminate", transaction="move")
    assert list(run.state.incidents) == ["@transactions:0"] and run.txn().status == "open"
    resolve(run, "@transactions:0", "terminate")
    assert (run.txn().status, run.state.status) == ("force_ended", "cancelled")


def retried_first() -> Run:
    """A run of the first rules: the first of two stuck confirms was retried and finished, and the second is left."""
    run = two_stuck()
    resolve(run, "hold#confirm:2", "retry_safe")
    run.done("hold#confirm", {}, generation=3)
    return run


def reconciled_first() -> Run:
    """A run of the first rules: a person ended the transaction, and its second incident is left."""
    run = two_stuck()
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished(*CONFIRMS))
    return run


def interim_left() -> Run:
    """A run of the first rules: a person ended the transaction, and a failure recovery had not decided is left."""
    run = one_stuck()
    attempt_failed(run, "hold2#confirm")
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold"))
    assert list(run.state.incidents) == ["hold2#confirm:1"] and run.state.status == "suspended"
    return run


@pytest.mark.parametrize("make", [two_stuck, retried_first])
@pytest.mark.parametrize(
    ("kind", "transaction_ends", "run_ends"),
    [
        ("accept_reconciled_result", "reconciled", "succeeded"),
        ("terminate", "force_ended", "cancelled"),
    ],
)
def test_a_decision_on_a_second_incident_moves_a_run_of_the_first_rules_on(make, kind, transaction_ends, run_ends):
    run = make()
    output = finished(*CONFIRMS) if kind == "accept_reconciled_result" else UNSET
    resolve(run, "hold2#confirm:2", kind, output, rules=2)
    assert (run.txn().status, run.state.status, run.state.incidents) == (transaction_ends, run_ends, {})


def test_a_retry_of_a_second_incident_moves_a_run_of_the_first_rules_on():
    run = retried_first()
    resolve(run, "hold2#confirm:2", "retry_safe", rules=2)
    assert (run.txn().status, run.state.active, run.state.incidents) == ("committing", ["hold2#confirm"], {})
    run.done("hold2#confirm", {}, generation=3)
    assert (run.txn().status, run.state.status) == ("completed", "succeeded") and completed_once(run, "after")


@pytest.mark.parametrize(("make", "key"), [(reconciled_first, "hold2#confirm:2"), (interim_left, "hold2#confirm:1")])
def test_a_retry_of_what_an_earlier_decision_left_runs_nothing_again_and_the_run_goes_on(make, key):
    run = make()
    before = run.txn().model_copy(deep=True)
    gone = resolve(run, key, "retry_safe", rules=2)
    assert tasks(gone) == [] and controls(gone, "transaction") == [] and run.txn() == before
    assert (run.state.status, run.state.incidents) == ("succeeded", {}) and completed_once(run, "after")


@pytest.mark.parametrize("kind", ["accept_reconciled_result", "accept_reconciled_failure"])
@pytest.mark.parametrize(("make", "key"), [(reconciled_first, "hold2#confirm:2"), (interim_left, "hold2#confirm:1")])
def test_what_an_earlier_decision_left_takes_no_statement_about_another_system(make, key, kind):
    run = make()
    before = run.state.model_copy(deep=True)
    output = finished("hold2") if kind == "accept_reconciled_result" else UNSET
    refused = resolve(run, key, kind, output, rules=2)
    assert refused.commands == []
    assert run.state == before.model_copy(update={"accepted_sequence": run.state.accepted_sequence})


def test_ending_the_run_from_what_an_earlier_decision_left_keeps_the_decision():
    run = reconciled_first()
    resolve(run, "hold2#confirm:2", "terminate", rules=2)
    assert (run.txn().status, run.state.status, run.state.incidents) == ("reconciled", "cancelled", {})


def test_ending_the_run_from_what_an_earlier_decision_left_undoes_its_other_transactions_first():
    first = transaction([hold(), hold("hold2"), submit()])
    second = transaction([hold("hold3"), lookup()], identifier="other")
    run = Run(compiled([parallel(first, second)]))
    run.start()
    for key, output in (("hold", {"holdId": "h-1"}), ("hold2", {"holdId": "h-2"}), ("submit", {"orderId": "o-1"})):
        run.done(key, output)
    run.done("hold3", {"holdId": "h-3"})
    for key in ("hold#confirm", "hold2#confirm"):
        run.failed(key, "HOLD_EXPIRED", effect="none", generation=2)
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished(*CONFIRMS))
    assert list(run.state.incidents) == ["hold2#confirm:2"] and run.txn("other").status == "open"
    [release] = tasks(resolve(run, "hold2#confirm:2", "terminate", rules=2))
    assert release.node_id == "hold3#cancel" and run.txn("other").status == "compensating"
    run.done("hold3#cancel", {})
    assert (run.txn("other").status, run.txn().status, run.state.status) == ("cancelled", "reconciled", "cancelled")


def stuck_release() -> Run:
    """The reservation's release can't finish, and the step that failed applied nothing."""
    run = Run(compiled([transaction([hold(), add()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "REFUSED", effect="none")
    run.failed("hold#cancel", "REJECTED", effect="none", generation=10)
    return run


def stuck_undo() -> Run:
    """One undo can't finish, the one before it did, and the reservation was not released yet."""
    run = Run(compiled([transaction([hold(), add(), add("add2"), add("add3")])]))
    run.start(2)
    for key, output in (("hold", {"holdId": "h-1"}), ("add", {"id": "a-1"}), ("add2", {"id": "a-2"})):
        run.done(key, output)
    run.failed("add3", "REFUSED", effect="none")
    run.done("add2#undo", {})
    run.failed("add#undo", "REJECTED", effect="none", generation=10)
    return run


def stuck_after_the_pivot(effect: str) -> Run:
    run = Run(compiled([transaction([hold(), add(), submit(), notify()])]))
    run.start(2)
    for key, output in (("hold", {"holdId": "h-1"}), ("add", {"id": "a-1"}), ("submit", {"orderId": "o-1"})):
        run.done(key, output)
    run.done("hold#confirm", {})
    run.failed("notify", "UNAVAILABLE", effect=effect, generation=4)
    return run


UNDONE = dict.fromkeys(("hold", "add", "submit"), "undone")
TO_SAY = {
    "undo-finished": (stuck_undo, "completed", {"hold": "confirmed", "add": "done", "add2": "done"}),
    "undo-undone": (stuck_undo, "compensated", {"hold": "undone", "add": "undone"}),
    "settle-finished": (
        lambda: settle_timeout(2),
        "completed",
        {"hold": "confirmed", "add": "done", "add2": "done"},
    ),
    "settle-undone": (
        lambda: settle_timeout(2),
        "compensated",
        {"hold": "undone", "add": "undone", "add2": "undone"},
    ),
    "confirm-finished": (lambda: one_stuck(2), "completed", {"hold": "confirmed", "hold2": "confirmed"}),
    "confirm-undone": (lambda: one_stuck(2), "compensated", {**UNDONE, "hold2": "undone"}),
    "after-unknown-finished": (lambda: stuck_after_the_pivot("unknown"), "completed", {"notify": "done"}),
    "after-unknown-undone": (lambda: stuck_after_the_pivot("unknown"), "compensated", {**UNDONE, "notify": "undone"}),
    "after-failed-finished": (lambda: stuck_after_the_pivot("none"), "completed", {"notify": "done"}),
    "after-failed-undone": (lambda: stuck_after_the_pivot("none"), "compensated", UNDONE),
    "release-finished": (stuck_release, "completed", {"hold": "confirmed"}),
    "release-undone": (stuck_release, "compensated", {"hold": "undone"}),
}


@pytest.mark.parametrize("case", TO_SAY)
def test_a_reconciliation_names_every_step_whose_end_is_unknown(case):
    make, ending, steps = TO_SAY[case]
    run = make()
    key = run.txn().incident
    for left_out in steps:
        before = run.state.model_copy(deep=True)
        short = {name: outcome for name, outcome in steps.items() if name != left_out}
        refused = resolve(run, key, "accept_reconciled_result", record(ending, short))
        assert refused.commands == [], left_out
        assert run.state == before.model_copy(update={"accepted_sequence": run.state.accepted_sequence}), left_out
    resolve(run, key, "accept_reconciled_result", record(ending, steps))
    assert (run.txn().status, run.txn().reconciled_as, run.state.incidents.get(key)) == ("reconciled", ending, None)


OUTCOMES = json.loads(Path("tests/fixtures/transactions/reconcile-outcomes.json").read_text(encoding="utf-8"))


def standing(**participants: tuple[str, str]) -> TransactionRecord:
    """A transaction in doubt whose steps stand at the given mode and status, in this order."""
    items = [ParticipantRecord(key=key, mode=mode, status=status) for key, (mode, status) in participants.items()]
    return TransactionRecord(status="in_doubt", opened_at=START, participants=items)


def test_a_record_with_nothing_to_say_leaves_out_exactly_what_the_table_asks_for():
    wrong = []
    for row in OUTCOMES:
        held = standing(step=(row["mode"], row["status"]))
        doubted = frozenset({"step"}) if row["doubted"] else frozenset()
        if unsaid_steps(held, record(row["ending"], {}), doubted) != (["step"] if row["must_say"] else []):
            wrong.append(row)
    assert wrong == []


def test_what_a_record_leaves_out_comes_in_the_order_the_steps_were_registered():
    unknown = ("compensate", "unknown")
    held = standing(z=unknown, a=("compensate", "done"), m=("compensate", "dispatched"), b=unknown)
    assert unsaid_steps(held, record("completed", {"m": "done"}), frozenset()) == ["z", "b"]
    with pytest.raises(KernelError, match="^WV-TXN-RECONCILE-INCOMPLETE$"):
        check_reconciliation(held, record("completed", {"m": "done"}), complete=True, doubted=frozenset())
    # A record of another shape leaves nothing out: its shape is what's wrong.
    assert unsaid_steps(held, {"as": "completed", "participants": []}, frozenset()) == []
    with pytest.raises(KernelError, match="^WV-RUNTIME-OUTPUT$"):
        check_reconciliation(held, {"as": "completed", "participants": []}, complete=True, doubted=frozenset())


def test_the_steps_to_say_are_named_up_to_twenty_and_then_counted():
    assert unsaid_message(["hold", "add2"]) == "Say what happened to: hold, add2."
    keys = [f"add[{index}]" for index in range(23)]
    assert unsaid_message(keys[:20]) == f"Say what happened to: {', '.join(keys[:20])}."
    assert unsaid_message(keys) == f"Say what happened to: {', '.join(keys[:20])}, and 3 more."


def test_a_step_is_doubted_while_an_incident_in_doubt_of_its_transaction_is_at_it_or_at_its_phase():
    for run, expected in (
        (two_stuck(2), {"hold", "hold2"}),
        (stuck_after_the_pivot("none"), {"notify"}),
        (after_pivot_pair(2), {"hold", "lookup"}),
        (settle_timeout(2), {"move"}),
    ):
        facts = shapes(workflow(run.flow))
        assert facts is not None
        assert doubted_steps(facts, run.state, "move") == expected
        assert doubted_steps(facts, run.state, "other") == frozenset()


def test_a_step_that_finished_while_the_dialog_was_open_may_be_named():
    run = one_stuck(2)
    run.done("hold2#confirm", {})
    assert run.statuses()["hold2"] == "confirmed"
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished(*CONFIRMS))
    assert (run.txn().status, run.statuses()["hold2"], run.state.status) == ("reconciled", "confirmed", "succeeded")


def test_a_reservation_marked_finished_is_confirmed():
    said = record("completed", {"hold": "confirmed", "hold2": "done"})
    run = two_stuck(2)
    before = run.state.model_copy(deep=True)
    refused = resolve(run, "hold#confirm:2", "accept_reconciled_result", said)
    assert refused.commands == []
    assert run.state == before.model_copy(update={"accepted_sequence": run.state.accepted_sequence})
    # The first rules took it, and what they took still applies.
    run = two_stuck()
    resolve(run, "hold#confirm:2", "accept_reconciled_result", said)
    assert (run.txn().status, run.statuses()["hold2"]) == ("reconciled", "done")


def test_a_step_whose_setting_handled_its_failure_is_not_asked_about():
    run = Run(compiled([transaction([hold(), {**add(), "onError": "continue"}, submit()])]))
    run.start(2)
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "REFUSED", effect="none")
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert run.statuses()["add"] == "failed"
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold"))
    assert (run.txn().status, run.state.status) == ("reconciled", "succeeded")


def test_recorded_reconciliations_still_apply():
    # A run of the first rules asks about stuck steps only, whatever else its steps did.
    run = undo_stuck()
    resolve(run, "add#undo:10", "accept_reconciled_result", record("completed", {"add": "done"}))
    assert (run.txn().status, run.statuses()) == ("reconciled", {"hold": "done", "add": "done", "add2": "undone"})
    run = settle_timeout(1)
    resolve(run, "move:0", "accept_reconciled_result", record("compensated", {}))
    assert run.txn().status == "reconciled"
