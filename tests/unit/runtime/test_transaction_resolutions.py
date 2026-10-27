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

from typing import Any
from uuid import uuid4

import pytest
from transaction_support import Run, add, compiled, controls, deadlines, hold, notify, submit, tasks, transaction

from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES
from firefly_weave.runtime.models import Transition

ROLLED_BACK = {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}
UNSET = object()


def resolve(run: Run, incident_key: str, kind: str, output: Any = UNSET) -> Transition:
    incident = run.state.incidents[incident_key]
    data: dict[str, Any] = {
        "incident_key": incident_key,
        "receipt_id": str(uuid4()),
        "node_id": incident.node_id,
        "generation": incident.generation,
        "kind": kind,
        "reason": "Checked both systems.",
        "evidence_reference": "ticket-17",
        "actor_id": str(uuid4()),
    }
    if output is not UNSET:
        data["output"] = output
    return run.send("incident_resolved", **data)


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
