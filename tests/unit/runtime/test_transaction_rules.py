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

"""The rules a run's transactions follow: what runs stored before the second rules keep doing, and how a run moves."""

import json
from uuid import uuid4

import pytest
from transaction_support import (
    Run,
    add,
    after_pivot_pair,
    attempt_failed,
    compiled,
    finished,
    hold,
    lookup,
    notify,
    one_stuck,
    parallel,
    resolve,
    stored,
    submit,
    transaction,
    two_stuck,
)

from firefly_weave.operations.exports import safe_event
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

# What an event may say that isn't the rules the kernel knows.
UNKNOWN_RULES = [True, 1, 3, "2", 2.0, None]
KINDS = ("retry_safe", "accept_reconciled_result", "accept_reconciled_failure", "terminate")


def retried_first() -> Run:
    run = two_stuck()
    resolve(run, "hold#confirm:2", "retry_safe")
    run.done("hold#confirm", {}, generation=3)
    assert (run.state.status, run.txn().status, run.txn().incident) == ("suspended", "committing", None)
    return run


def reconciled_first() -> Run:
    run = two_stuck()
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold", "hold2"))
    assert (run.state.status, run.txn().status, run.txn().incident) == ("suspended", "reconciled", None)
    return run


def unchanged(run: Run, before) -> bool:
    return run.state == before.model_copy(update={"accepted_sequence": run.state.accepted_sequence})


def branches_after_the_pivot(*steps) -> Run:
    """The steps run side by side after the point of no return, and nothing else is left to settle."""
    run = Run(compiled([transaction([hold(), submit(), parallel(*steps)])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    return run


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("decided", [retried_first, reconciled_first])
def test_the_first_rules_leave_a_second_incident_nobody_can_resolve(decided, kind):
    run = decided()
    assert list(run.state.incidents) == ["hold2#confirm:2"]
    before = run.state.model_copy(deep=True)
    output = finished("hold2") if kind == "accept_reconciled_result" else None
    refused = resolve(run, "hold2#confirm:2", kind, output)
    assert unchanged(run, before) and refused.commands == []


def test_the_first_rules_leave_an_interim_failure_a_reconcile_stopped():
    run = one_stuck()
    attempt_failed(run, "hold2#confirm")
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold"))
    assert (run.state.status, run.state.active, list(run.state.incidents)) == ("suspended", [], ["hold2#confirm:1"])


def test_the_first_rules_leave_the_incident_of_a_refused_output_a_reconcile_stopped():
    # A read's output was refused: no recovery decides that, only a person.
    pause = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
    run = Run(compiled([transaction([hold(), parallel(submit(), lookup())]), pause]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.send("incident_opened", node_id="lookup", generation=1, code="WV-SCHEMA-SECRET_VALUE")
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    resolve(run, "hold#confirm:2", "accept_reconciled_result", finished("hold"))
    assert (run.txn().status, run.state.status, run.state.active) == ("reconciled", "suspended", [])
    assert list(run.state.incidents) == ["lookup:1"] and run.state.parked == ["pause"]


@pytest.mark.parametrize("code", ["WV-TASK-FAILED", "WV-SCHEMA-SECRET_VALUE"])
def test_the_first_rules_leave_the_incident_of_a_read_the_hold_deadline_stopped(code):
    run = Run(compiled([transaction([hold(), lookup(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    if code == "WV-TASK-FAILED":
        attempt_failed(run, "lookup")
    else:
        run.send("incident_opened", node_id="lookup", generation=1, code=code)
    run.fire("move#hold")
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status, run.state.active) == ("cancelled", "suspended", [])
    assert run.state.incidents["lookup:1"].code == code and run.state.parked == ["@join:move"]


def test_the_first_rules_lose_a_result_deferred_behind_a_second_doubt():
    run = branches_after_the_pivot(notify(), notify("notify2"), notify("notify3"))
    for key in ("notify", "notify2", "notify3"):
        run.failed(key, "UNAVAILABLE", effect="unknown", generation=4)
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["notify2", "notify3"]
    resolve(run, "notify:4", "retry_safe")
    # The second failure put the transaction in doubt again, and the third one was dropped behind it.
    assert (run.txn().status, run.txn().incident, run.state.deferred_results) == ("in_doubt", "notify2:4", [])
    assert run.state.active == ["notify"] and list(run.state.incidents) == ["notify2:4"]
    assert run.statuses()["notify3"] == "unknown"
    run.done("notify", {}, generation=5)
    resolve(run, "notify2:4", "retry_safe")
    run.done("notify2", {}, generation=5)
    # Nothing is left to happen: no step runs, no incident is open, and the run waits for a branch that never ends.
    assert (run.state.status, run.state.active, run.state.incidents) == ("waiting", [], {})
    assert (run.txn().status, run.statuses()["notify3"]) == ("committing", "unknown")


def test_the_first_rules_lose_the_successors_of_a_result_deferred_behind_a_second_doubt():
    run = branches_after_the_pivot(notify(), notify("notify2"), notify("notify3"))
    run.failed("notify", "UNAVAILABLE", effect="unknown", generation=4)
    run.failed("notify2", "UNAVAILABLE", effect="unknown", generation=4)
    run.done("notify3", {})
    resolve(run, "notify:4", "retry_safe")
    run.done("notify", {}, generation=5)
    resolve(run, "notify2:4", "retry_safe")
    run.done("notify2", {}, generation=5)
    assert (run.state.status, run.state.active, run.state.incidents) == ("waiting", [], {})
    # Its step is done, but its branch never went on.
    assert (run.txn().status, run.statuses()["notify3"]) == ("committing", "done")
    assert run.state.branches["both/b2"].status == "running"


def read_fails_before_a_step_beside_it() -> Run:
    """Two branches of the body fail before the same recovery scan, and recovery takes the read first."""
    run = Run(compiled([transaction([hold(), add(), parallel(add("add2"), lookup()), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    attempt_failed(run, "lookup")
    attempt_failed(run, "add2")
    run.failed("lookup", "UNAVAILABLE", effect="unknown")
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]
    return run


def test_the_first_rules_hold_the_run_behind_a_stopped_steps_deferred_result():
    run = read_fails_before_a_step_beside_it()
    refused = run.failed("add2", "REFUSED", effect="none")
    # The rollback stopped the read, and then its deferred failure refused the whole event.
    assert refused.commands == [] and run.txn().status == "open"
    assert {key: item.code for key, item in run.state.incidents.items()} == {
        "add2:1": "WV-TASK-FAILED",
        "add2:0": "WV-RUNTIME-EVENT",
    }
    assert run.state.active == ["add2", "lookup"]
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]


def pivot_beside_a_read() -> Run:
    """The step that can't be undone may have happened, and a read beside it is still in flight.

    A parallel branch may hold that step when no other branch holds a step the transaction would undo or confirm.
    """
    run = Run(compiled([transaction([hold(), parallel(submit(), lookup())])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("submit", "TIMEOUT", effect="unknown", generation=3)
    assert run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN" and run.state.active == ["lookup"]
    return run


def test_the_first_rules_leave_the_interim_failure_of_a_read_when_the_step_beside_it_didnt_happen():
    run = pivot_beside_a_read()
    attempt_failed(run, "lookup")
    resolve(run, "submit:3", "accept_reconciled_failure")
    run.done("hold#cancel", {})
    assert (run.txn().status, run.state.status, run.state.active) == ("cancelled", "suspended", [])
    assert list(run.state.incidents) == ["lookup:1"]


def test_the_first_rules_refuse_that_the_step_didnt_happen_behind_a_reads_deferred_result():
    run = pivot_beside_a_read()
    run.done("lookup", {"count": 1})
    assert [event.data["node_id"] for event in run.state.deferred_results] == ["lookup"]
    resolve(run, "submit:3", "accept_reconciled_failure")
    assert run.txn().status == "in_doubt" and run.state.incidents["submit:3"].code == "WV-TXN-PIVOT-UNKNOWN"
    assert run.state.incidents["submit:0"].code == "WV-RUNTIME-EVENT"


def test_a_stuck_confirm_waits_beside_a_stuck_step_only_when_the_step_was_already_in_flight():
    # Otherwise the body waits for every confirm, so no step after the point of no return is in flight with one.
    run = after_pivot_pair()
    assert (run.txn().status, run.txn().incident) == ("in_doubt", "lookup:3")
    run = one_stuck()
    assert run.txn().resume == ["@branch:move:0"] and run.state.active == ["hold2#confirm"]


def two_transactions(run_rules: int = 1) -> Run:
    """Two transactions in flight side by side, whose keys PostgreSQL gives back in another order."""
    blocks = (transaction([hold()], identifier="aa"), transaction([hold("hold2")], identifier="b"))
    run = Run(compiled([parallel(*blocks)]))
    run.start(run_rules)
    assert (list(run.state.transactions), list(stored(run.state).transactions)) == (["aa", "b"], ["b", "aa"])
    return run


ORDER = pytest.mark.xfail(strict=True, reason="the first rules take the transactions in the order the state has them")


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("task_completed", {"node_id": "hold", "generation": 1, "output": {"holdId": "h-1"}}),
        pytest.param("cancelled", {"reason": "stop"}, marks=ORDER),
        pytest.param("cancelled", {"reason": "stop", "skip_undo": True}, marks=ORDER),
    ],
)
def test_a_stored_state_gives_the_same_transition_as_the_state_in_memory(kind, data):
    run = two_transactions()
    event = RuntimeEvent(id=uuid4(), type=kind, timestamp=run.now, sequence=run.state.accepted_sequence + 1, data=data)
    assert transition(stored(run.state), event, run.flow) == transition(run.state, event, run.flow)


def test_a_stored_state_without_rules_keeps_its_bytes():
    run = two_stuck()
    kept = run.state.model_dump_json()
    assert "transaction_rules" not in kept
    read = RunState.model_validate_json(kept)
    assert (read.transaction_rules, read.model_dump_json()) == (1, kept)
    moved = RunState.model_validate_json(json.dumps({**json.loads(kept), "transaction_rules": 2}))
    assert moved.transaction_rules == 2 and json.loads(moved.model_dump_json())["transaction_rules"] == 2


def test_a_run_starts_under_the_second_rules_only_when_its_event_says_so():
    for rules in (1, 2):
        run = Run(compiled([transaction([hold(), submit()])]))
        run.start(rules)
        assert (run.state.transaction_rules, run.state.status) == (rules, "waiting")
        run.done("hold", {"holdId": "h-1"})
        assert run.state.transaction_rules == rules


@pytest.mark.parametrize("rules", UNKNOWN_RULES)
def test_a_start_with_rules_the_kernel_does_not_know_is_refused(rules):
    run = Run(compiled([transaction([hold(), submit()])]))
    held = run.send("started", origin="manual", run_id=str(uuid4()), transaction_rules=rules)
    assert [item.code for item in run.state.incidents.values()] == ["WV-RUNTIME-EVENT"] and held.commands == []
    assert (run.state.transaction_rules, run.state.run_id, run.state.transactions) == (1, None, {})


def test_a_start_of_a_workflow_without_a_transaction_takes_no_rules():
    run = Run(compiled([lookup()]))
    run.start(2)
    assert [item.code for item in run.state.incidents.values()] == ["WV-RUNTIME-EVENT"]
    assert run.state.transaction_rules == 1 and "transaction_rules" not in run.state.model_dump_json()


def test_a_resolution_moves_a_run_to_the_second_rules_for_good():
    run = two_stuck()
    resolve(run, "hold#confirm:2", "retry_safe", rules=2)
    assert run.state.transaction_rules == 2 and "hold#confirm" in run.state.active
    # An event without rules never takes it back.
    run.done("hold#confirm", {}, generation=3)
    assert run.state.transaction_rules == 2


def test_the_resolution_of_an_incident_outside_a_transaction_moves_its_run_too():
    run = Run(compiled([parallel(transaction([hold(), submit()]), lookup())]))
    run.start()
    run.send("incident_opened", node_id="lookup", generation=1, code="WV-TASK-RETRIES-EXHAUSTED")
    resolve(run, "lookup:1", "retry_safe", rules=2)
    assert (run.state.transaction_rules, run.state.status, run.state.incidents) == (2, "waiting", {})


def test_a_refused_resolution_changes_no_rules():
    run = two_stuck()
    before = run.state.model_copy(deep=True)
    refused = resolve(run, "hold#confirm:2", "accept_reconciled_result", {"as": "completed"}, rules=2)
    assert unchanged(run, before) and refused.commands == [] and run.state.transaction_rules == 1


@pytest.mark.parametrize("rules", UNKNOWN_RULES)
def test_a_resolution_with_rules_the_kernel_does_not_know_changes_nothing(rules):
    run = two_stuck()
    before = run.state.model_copy(deep=True)
    refused = resolve(run, "hold#confirm:2", "retry_safe", transaction_rules=rules)
    assert unchanged(run, before) and refused.commands == []


@pytest.mark.parametrize("rules", [*UNKNOWN_RULES, 2])
def test_a_resolution_outside_a_transaction_with_rules_it_cant_take_changes_nothing(rules):
    # The rules the kernel knows fit no workflow without a transaction; the others fit none at all.
    flow = [lookup()] if type(rules) is int and rules == 2 else [parallel(transaction([hold()]), lookup())]
    run = Run(compiled(flow))
    run.start()
    run.send("incident_opened", node_id="lookup", generation=1, code="WV-TASK-RETRIES-EXHAUSTED")
    before = run.state.model_copy(deep=True)
    refused = resolve(run, "lookup:1", "retry_safe", transaction_rules=rules)
    assert unchanged(run, before) and refused.commands == []


def test_an_exported_event_keeps_its_rules():
    run = two_stuck(run_rules=2)
    resolve(run, "hold#confirm:2", "retry_safe")
    started, resolved = run.events[0], run.events[-1]
    assert (started.data["transaction_rules"], resolved.data["transaction_rules"]) == (2, 2)
    assert resolved.data["transaction"] == "move"
    assert safe_event(started, run.flow).data == started.data
    withheld = {"reason", "evidence_reference"}
    assert safe_event(resolved, run.flow).data == {k: v for k, v in resolved.data.items() if k not in withheld}
