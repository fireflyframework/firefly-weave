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

"""What a transaction event says: its state, the steps done, undone and pending, its cause, and a business key that
is withheld when it comes from a secret input."""

from uuid import UUID

from transaction_support import RUN, Run, add, compiled, hold, submit, transaction

from firefly_weave.runtime.settlement import EVENT_STATES, event_facts

INCIDENT = UUID("0b8f4d8e-4a3c-4f6e-9a1b-2c3d4e5f6a7b")


def facts(run: Run, incident: UUID | None = None):
    record = run.txn()
    return event_facts(
        RUN,
        "move",
        record,
        run.state.incidents,
        workflow=("move-between-systems", "1.0.0"),
        test=False,
        incident_id=incident,
    ).model_dump(mode="json")


def test_a_committed_transaction_names_what_it_did():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.done("submit", {"orderId": "o-1"})
    run.done("hold#confirm", {})
    assert run.txn().status == "completed" and "completed" in EVENT_STATES
    assert facts(run) == {
        "workflow": {"name": "move-between-systems", "version": "1.0.0"},
        "run_id": str(RUN),
        "instance_key": "move",
        "business_key": "M-1",
        "state": "completed",
        "steps": {
            "done": [
                {"step": "hold", "instance_key": "hold", "how": "reserve"},
                {"step": "add", "instance_key": "add", "how": "compensate"},
                {"step": "submit", "instance_key": "submit", "how": "irreversible"},
            ],
            "undone": [],
            "pending": [],
        },
        "test": False,
    }


def test_a_compensated_transaction_names_its_cause_and_what_it_undid():
    run = Run(compiled([transaction([hold(), add(), add("add2"), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.done("add", {"id": "a-1"})
    run.failed("add2", "UNAVAILABLE", effect="none")
    run.done("add#undo", {})
    run.done("hold#cancel", {})
    view = facts(run)
    assert (view["state"], view["cause"]) == ("compensated", {"code": "UNAVAILABLE", "step": "add2"})
    assert [item["instance_key"] for item in view["steps"]["undone"]] == ["hold", "add"]


def test_an_undo_stuck_in_doubt_names_the_incident_and_what_is_pending():
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.failed("add", "UNAVAILABLE", effect="unknown")
    run.failed("add#undo", "UNAVAILABLE", generation=10)
    view = facts(run, INCIDENT)
    assert view["state"] == "in_doubt" and view["incident_id"] == str(INCIDENT)
    assert view["cause"] == {"code": "WV-TXN-UNDO-STUCK", "step": "add", "origin_code": "UNAVAILABLE"}
    assert view["steps"]["pending"] == [
        {"step": "hold", "instance_key": "hold", "how": "reserve"},
        {"step": "add", "instance_key": "add", "how": "compensate", "phase": "undo", "attempts": 10},
    ]


def test_a_business_key_from_a_secret_input_is_withheld():
    from firefly_weave.runtime.kernel import prepare

    secret = compiled([transaction([hold(), add(), submit()])], secret=True)
    assert prepare(secret).transactions.withheld == {"move"}
    assert prepare(compiled([transaction([hold(), add(), submit()])])).transactions.withheld == frozenset()
    # A secret value never enters a run in the first place; the key is withheld should one ever reach a transaction.
    refused = Run(secret)
    refused.start()
    assert refused.state.incidents["@start:0"].code == "WV-SCHEMA-SECRET_VALUE"
    run = Run(compiled([transaction([hold(), add(), submit()])]))
    run.start()
    run.done("hold", {"holdId": "h-1"})
    run.state.transactions["move"] = run.txn().model_copy(update={"business_key_withheld": True})
    assert facts(run)["business_key"] is None
