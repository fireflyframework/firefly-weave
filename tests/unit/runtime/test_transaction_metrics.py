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

"""Counting transactions and their phases with bounded labels, and the run log lines that say what is undone."""

from transaction_support import Run, add, compiled, hold, submit, transaction

from firefly_weave.contracts.transactions import IN_DOUBT_CODES, TRANSACTION_MESSAGES
from firefly_weave.operations.telemetry import ERRORS, OPERATIONS_ALLOWED, attributes
from firefly_weave.runtime.settlement import log_line, phase_measure, transaction_measure


def test_every_label_value_is_bounded():
    for operation in (
        "transaction.commit",
        "transaction.rollback",
        "transaction.resolve",
        "transaction.confirm",
        "transaction.cancel",
        "transaction.compensate",
        "transaction.in_doubt",
        "transaction.compensating",
    ):
        assert operation in OPERATIONS_ALLOWED
    assert {code for code in TRANSACTION_MESSAGES if code.startswith("WV-TXN-")} <= ERRORS
    assert IN_DOUBT_CODES <= ERRORS
    assert {"UNAVAILABLE", "TIMEOUT", "RATE_LIMITED", "TRANSIENT"} <= ERRORS
    assert attributes("transaction.commit", "ok", "SOMETHING_ELSE")["error_code"] == "other"


def drive(kind: str) -> Run:
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    if kind == "completed":
        run.done("add", {"id": "a-1"})
        run.done("submit", {"orderId": "o-1"})
        run.done("hold#confirm", {})
    elif kind == "compensated":
        run.failed("add", "UNAVAILABLE", effect="unknown")
        run.done("add#undo", {})
        run.done("hold#cancel", {})
    elif kind == "cancelled":
        run.failed("add", "REFUSED", effect="none")
        run.done("hold#cancel", {})
    elif kind == "in_doubt":
        run.failed("add", "UNAVAILABLE", effect="unknown")
        run.failed("add#undo", "UNAVAILABLE", generation=10)
    return run


def test_one_counter_per_terminal_or_in_doubt_change():
    expected = {
        "completed": ("transaction.commit", "ok", "none"),
        "compensated": ("transaction.rollback", "ok", "UNAVAILABLE"),
        "cancelled": ("transaction.rollback", "ok", "other"),
        "in_doubt": ("transaction.rollback", "unknown", "WV-TXN-UNDO-STUCK"),
    }
    for kind, labels in expected.items():
        run = drive(kind)
        measure = transaction_measure(run.txn(), run.state.incidents)
        assert measure is not None and (measure.operation, measure.status, measure.error_code) == labels, kind
        assert (measure.duration is None) == (kind == "in_doubt")
    record = drive("in_doubt").txn()
    assert transaction_measure(record.model_copy(update={"status": "reconciled"}), {}).operation == (
        "transaction.resolve"
    )
    forced = transaction_measure(record.model_copy(update={"status": "force_ended"}), {})
    assert (forced.operation, forced.status) == ("transaction.resolve", "cancelled")
    assert transaction_measure(drive("open").txn(), {}) is None


def test_each_phase_result_counts_once():
    assert phase_measure("hold#confirm", "task_completed", {}) == ("transaction.confirm", "ok")
    assert phase_measure("hold#cancel", "step_failed", {"effect": "none"}) == ("transaction.cancel", "failed")
    assert phase_measure("add#undo", "step_failed", {"effect": "unknown"}) == ("transaction.compensate", "unknown")
    assert phase_measure("add#undo", "call_completed", {"status": "failed"}) == ("transaction.compensate", "failed")
    assert phase_measure("add", "task_completed", {}) is None


def test_the_run_log_says_what_is_undone_and_why_it_is_in_doubt():
    run = Run(compiled([transaction([hold(), add(), add("add2"), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("add2", {"id": "a-2"})
    before = run.txn()
    run.failed("submit", "UNAVAILABLE", effect="none")
    assert log_line("move", before, run.txn(), run.state.incidents) == (
        "move: undoing 3 steps after submit failed (UNAVAILABLE)"
    )
    stuck = drive("in_doubt")
    record = stuck.txn()
    assert log_line("move", record.model_copy(update={"status": "compensating"}), record, stuck.state.incidents) == (
        "move: in doubt: undo of add couldn't finish after 10 tries"
    )
