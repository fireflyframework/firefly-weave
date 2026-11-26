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

"""Pure facts of transactions: whether a failed participant may have applied anything (each attempt judged by its
release, folded over attempts), whose an incident is, and what a person must say about a transaction in doubt."""

import json
from pathlib import Path

import pytest
from transaction_support import add, after_pivot_pair, compiled, hold, lookup, parallel, submit, transaction

from firefly_weave.contracts.transactions import IN_DOUBT_CODES, PARTICIPANT_STATUSES, UNDO_MODES, must_say
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.models import IncidentState, RunState
from firefly_weave.runtime.transactions import (
    closes_with_step,
    deadline_key,
    failure_effect,
    fold,
    incident_transaction,
    leftover,
    open_doubts,
    shapes,
    split_deadline,
)

ADD = {"sideEffect": "non_idempotent", "definiteFailures": ["VALIDATION"]}
CONNECTOR = {
    "implementation": {"kind": "connector", "uses": "target-system@1.0.0", "action": "add"},
    "sideEffect": "idempotency_key",
    "transactionAware": True,
}
# A worker action whose definition claims awareness: only the claiming release's own declaration counts.
WORKER = {
    "implementation": {"kind": "worker", "taskType": "add-quantity", "taskVersion": "1.0.0"},
    "sideEffect": "non_idempotent",
    "transactionAware": True,
}


def attempt(action, error, aware=False):
    return failure_effect(action, error, transaction_aware=aware)


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        # A temporary code, then a definite failure from an aware release: the first may have applied.
        (
            [attempt(ADD, {"code": "TIMEOUT"}), attempt(ADD, {"code": "REFUSED", "effect": "none"}, aware=True)],
            "unknown",
        ),
        ([attempt(ADD, {"code": "REFUSED", "effect": "none"}, aware=True)], "none"),
        ([attempt(ADD, {"code": "REFUSED", "effect": "none"})], "unknown"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "failed"})], "unknown"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "not_started"}, aware=True)], "none"),
        ([attempt(ADD, {"code": "REFUSED", "outcome": "not_started"})], "unknown"),
        ([attempt(ADD, {"code": "VALIDATION"})], "none"),
        # A lease that expired in an earlier attempt, then a definite failure.
        (["unknown", attempt(ADD, {"code": "VALIDATION"})], "unknown"),
        # Revoked before any worker claimed it.
        (["none"], "none"),
        # A connector release that declares itself aware in its action specs.
        ([attempt(CONNECTOR, {"code": "REFUSED", "effect": "none"})], "none"),
        # A worker action marked aware, from a release that declared itself unaware (or never declared).
        ([attempt(WORKER, {"code": "REFUSED", "effect": "none"})], "unknown"),
        ([attempt(WORKER, {"code": "REFUSED", "outcome": "not_started"})], "unknown"),
        # The same action from a release that declared itself aware.
        ([attempt(WORKER, {"code": "REFUSED", "effect": "none"}, aware=True)], "none"),
    ],
)
def test_a_participant_applied_nothing_only_when_every_attempt_says_so_from_a_release_that_may(attempts, expected):
    effect = "none"
    for observed in attempts:
        effect = fold(effect, observed)
    assert effect == expected


def test_once_unknown_always_unknown():
    assert fold("unknown", "none") == "unknown" and fold("none", "none") == "none" and fold("none", None) == "unknown"


@pytest.mark.parametrize(
    ("key", "kind", "generation", "text"),
    [
        ("move", "hold", 0, "move#hold"),
        ("move[2]", "settle", 0, "move[2]#settle"),
        ("move", "settle", 1, "move#settle.1"),
        ("move[0][3]", "settle", 12, "move[0][3]#settle.12"),
        ("move", "decide", 0, "move#decide"),
    ],
)
def test_a_transaction_deadline_key_names_its_transaction_kind_and_generation(key, kind, generation, text):
    assert deadline_key(key, kind, generation) == text
    assert split_deadline(text) == (key, kind)


@pytest.mark.parametrize("text", ["move#settle.0", "move#settle.01", "move#hold.1", "move#settle.x", "move#undo"])
def test_other_keys_are_not_transaction_deadlines(text):
    assert split_deadline(text) is None


def test_dispatch_effect_is_none_only_without_calls_and_changing_actions():
    from firefly_weave.contracts.transactions import dispatch_effect

    dependencies = [
        {"digest": "read", "kind": "Action", "document": {"spec": {"sideEffect": "read_only"}}},
        {"digest": "change", "kind": "Action", "document": {"spec": {"sideEffect": "non_idempotent"}}},
    ]
    assert dispatch_effect(False, ["read"], dependencies) == "none"
    assert dispatch_effect(False, ["read", "change"], dependencies) == "unknown"
    assert dispatch_effect(True, [], dependencies) == "unknown"
    assert dispatch_effect(False, [], dependencies) == "none"
    assert dispatch_effect(False, ["unknown-digest"], dependencies) == "unknown"


def facts():
    """A transaction in a loop and one outside it, each with a step that changes nothing, and a step in neither."""
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2, 3]},
        "maxItems": 3,
        "concurrency": 3,
        "body": {"steps": [transaction([hold("hold2"), add()], identifier="inner")], "output": {"literal": None}},
    }
    found = shapes(workflow(compiled([transaction([hold(), lookup(), submit()]), each, lookup("after")])))
    assert found is not None
    return found


@pytest.mark.parametrize(
    ("node_id", "code", "expected"),
    [
        ("hold#confirm", "WV-TXN-CONFIRM-STUCK", "move"),
        ("hold#cancel", "WV-TXN-CANCEL-STUCK", "move"),
        ("submit", "WV-TXN-PIVOT-UNKNOWN", "move"),
        # A step of the body that changes nothing, and a failure that isn't in doubt.
        ("lookup", "WV-TXN-AFTER-PIVOT-STUCK", "move"),
        ("lookup", "WV-TASK-FAILED", "move"),
        ("hold#confirm", "WV-TASK-FAILED", "move"),
        # Inside a loop, the transaction of the same iteration.
        ("hold2[2]#cancel", "WV-TXN-CANCEL-STUCK", "inner[2]"),
        ("add[0]", "WV-TASK-FAILED", "inner[0]"),
        # A settle timeout is the transaction's own; any other code at its key isn't.
        ("move", "WV-TXN-SETTLE-TIMEOUT", "move"),
        ("inner[1]", "WV-TXN-SETTLE-TIMEOUT", "inner[1]"),
        ("after", "WV-TXN-SETTLE-TIMEOUT", None),
        ("move", "WV-TASK-FAILED", None),
        # Outside every transaction, the run's own hold, and keys that name no instance.
        ("after", "WV-TASK-FAILED", None),
        ("@transactions", "WV-TXN-RUN-OVERSIZED", None),
        ("", "WV-TASK-FAILED", None),
        ("move[", "WV-TXN-SETTLE-TIMEOUT", None),
    ],
)
def test_an_incident_belongs_to_the_transaction_that_holds_its_step(node_id, code, expected):
    assert incident_transaction(facts(), node_id, code) == expected


def test_the_open_doubts_of_a_transaction_are_its_own_in_doubt_incidents_in_key_order():
    state = RunState(
        status="suspended",
        incidents={
            key: IncidentState(node_id=key.rpartition(":")[0], generation=int(key.rpartition(":")[2]), code=code)
            for key, code in {
                "lookup:2": "WV-TXN-AFTER-PIVOT-STUCK",
                "hold#confirm:2": "WV-TXN-CONFIRM-STUCK",
                "submit:1": "WV-TASK-FAILED",
                "hold2[0]#confirm:2": "WV-TXN-CONFIRM-STUCK",
                "hold2[1]#confirm:2": "WV-TXN-CONFIRM-STUCK",
                "@transactions:0": "WV-TXN-RUN-OVERSIZED",
            }.items()
        },
    )
    assert open_doubts(facts(), state, "move") == ["hold#confirm:2", "lookup:2"]
    assert open_doubts(facts(), state, "inner[1]") == ["hold2[1]#confirm:2"]
    assert open_doubts(facts(), state, "inner[2]") == []


@pytest.mark.parametrize(
    ("status", "active", "code", "expected"),
    [
        # In doubt: only a person ends a transaction with an incident of it still open.
        ("reconciled", [], "WV-TXN-CONFIRM-STUCK", True),
        ("force_ended", [], "WV-TXN-CONFIRM-STUCK", True),
        ("reconciled", ["hold#confirm"], "WV-TXN-CONFIRM-STUCK", True),
        ("in_doubt", [], "WV-TXN-CONFIRM-STUCK", False),
        ("committing", [], "WV-TXN-CONFIRM-STUCK", False),
        ("compensated", [], "WV-TXN-CONFIRM-STUCK", False),
        (None, [], "WV-TXN-CONFIRM-STUCK", False),
        # Not in doubt: the run no longer waits for the step, however its transaction stands.
        ("reconciled", [], "WV-TASK-FAILED", True),
        ("compensated", [], "WV-TASK-FAILED", True),
        ("compensating", [], "WV-TASK-FAILED", True),
        ("reconciled", [], "WV-SCHEMA-SECRET_VALUE", True),
        ("committing", ["hold#confirm"], "WV-TASK-FAILED", False),
        ("committing", ["hold#confirm"], "WV-SCHEMA-SECRET_VALUE", False),
        # The kernel refused an event: what that event carried is still to be decided.
        ("reconciled", [], "WV-RUNTIME-EVENT", False),
    ],
)
def test_an_incident_is_a_leftover_once_nothing_waits_for_it(status, active, code, expected):
    run = after_pivot_pair()
    assert run.state.transactions["move"].status == "in_doubt"
    transactions = {"move": run.txn().model_copy(update={"status": status})} if status is not None else {}
    state = run.state.model_copy(update={"transactions": transactions, "active": active})
    found = shapes(workflow(run.flow))
    assert found is not None
    assert leftover(found, state, IncidentState(node_id="hold#confirm", generation=2, code=code)) is expected


def test_an_incident_closes_with_its_step_unless_it_is_in_doubt_or_holds_a_refused_event():
    codes = ["WV-TASK-FAILED", "WV-TASK-RETRIES-EXHAUSTED", "WV-SCHEMA-SECRET_VALUE", "WV-RUNTIME-EVENT"]
    closing = [code for code in [*codes, *sorted(IN_DOUBT_CODES)] if closes_with_step(code)]
    assert closing == ["WV-TASK-FAILED", "WV-TASK-RETRIES-EXHAUSTED", "WV-SCHEMA-SECRET_VALUE"]


@pytest.mark.parametrize("code", ["WV-TASK-FAILED", "WV-TASK-RETRIES-EXHAUSTED"])
def test_an_incident_outside_every_transaction_is_never_a_leftover(code):
    flow = compiled([parallel(transaction([hold()]), lookup())])
    found = shapes(workflow(flow))
    assert found is not None
    assert not leftover(found, RunState(), IncidentState(node_id="lookup", generation=1, code=code))


OUTCOMES = json.loads(Path("tests/fixtures/transactions/reconcile-outcomes.json").read_text(encoding="utf-8"))


def test_the_table_of_what_a_reconciliation_must_say_names_every_combination_once():
    rows = [(row["ending"], row["mode"], row["status"], row["doubted"]) for row in OUTCOMES]
    every = {
        (ending, mode, status, doubted)
        for ending in ("completed", "compensated")
        for mode in UNDO_MODES
        for status in PARTICIPANT_STATUSES
        for doubted in (False, True)
    }
    assert len(rows) == len(set(rows)) and set(rows) == every


def test_what_a_reconciliation_must_say():
    wrong = [
        row
        for row in OUTCOMES
        if must_say(row["ending"], row["mode"], row["status"], row["doubted"]) is not row["must_say"]
    ]
    assert wrong == []
