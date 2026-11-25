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

"""Execute on the Studio host and transactions: pins of confirms, releases and undos, failures with their effect, and
what the answer says about each transaction and its confirms, releases and undos."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "operations" / "debug"))

from simulation_support import STOP, callee, each, fixture, strict  # noqa: E402
from transaction_support import add, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.contracts.transactions import IN_DOUBT_CODES  # noqa: E402
from firefly_weave.operations.debug.transactions import SESSION_RUN_ID  # noqa: E402
from firefly_weave.studio import local_contracts, simulation  # noqa: E402
from firefly_weave.studio.host import make_studio_app  # noqa: E402
from firefly_weave.studio.local_contracts import ExecuteRequest, ScriptedFailure  # noqa: E402
from firefly_weave.studio.service import StudioOptions  # noqa: E402
from firefly_weave.studio.simulation import SimulationRefused, execute, simulator_for, steps_of  # noqa: E402

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "moveId": {"type": "string"},
        "quantity": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["moveId", "quantity"],
}
# What the steps of a move return unless they take a simulated failure.
MOVED = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}
# submit is refused and applied nothing, so the steps before it are undone.
REFUSED = {"node:submit": {"code": "REFUSED", "effect": "none"}}
# The fixture's workflow that undoes add when it is undone by a call.
UNDO = "reverse-target-addition"
# A step that needs test data: nothing a simulation could make up.
LOOKUP = {
    "id": "lookup",
    "kind": "action",
    "uses": "reports.lookup@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/reference"}}},
}


def source(*steps: dict[str, Any]) -> str:
    return json.dumps(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "move-between-systems", "version": "1.0.0"},
            "spec": {
                "inputSchema": INPUT_SCHEMA,
                "outputSchema": {},
                "steps": list(steps),
                "output": {"literal": None},
            },
        }
    )


def move(*steps: dict[str, Any], **settings: Any) -> str:
    """A workflow of one transaction: hold, add and submit, or the steps given."""
    return source(transaction(list(steps) or [hold(), add(), submit()], **settings))


def move_request(workflow: str, **fields: Any) -> ExecuteRequest:
    """A request for ``workflow`` over the fixture catalog, with the results of its own steps and ``mocks``."""
    present = {step["id"] for step in steps_of(json.loads(workflow)["spec"]["steps"])}
    failing = fields.get("errors", {})
    moved = {key: value for key, value in MOVED.items() if key not in failing and key.removeprefix("node:") in present}
    body = {
        "source": workflow,
        "format": "json",
        "definitions": fixture()["definitions"],
        "catalog": "none",
        "input": {"moveId": "M-1", "quantity": 2},
        "now": "2026-11-24T10:00:00Z",
        "target": {"mode": "workflow"},
        **fields,
        "mocks": moved | fields.get("mocks", {}),
    }
    return ExecuteRequest.model_validate_json(json.dumps(body))


def answered(body: ExecuteRequest) -> dict[str, Any]:
    """The host's answer, as it serializes it."""
    return execute(body, None).model_dump(mode="json", by_alias=True, exclude_unset=True)


def run_move(workflow: str, **fields: Any) -> dict[str, Any]:
    return answered(move_request(workflow, **fields))


def undo_by_call_request(*steps: dict[str, Any], **fields: Any) -> ExecuteRequest:
    """A move whose submit is refused, undone by a called workflow of ``steps`` that the request brings along."""
    undo, _ = callee(UNDO, list(steps), **fields)
    others = [item for item in fixture()["definitions"] if item["metadata"]["name"] != UNDO]
    return move_request(
        move(hold(), reverse_by_call(), submit()),
        definitions=[*others, undo],
        workflows={f"{UNDO}@1.0.0": {"mocks": {}}},
        errors=REFUSED,
    )


def simulated(body: ExecuteRequest) -> Any:
    """The debug view of the simulation that Execute workflow runs."""
    document = json.loads(body.source)
    _, simulator, _ = simulator_for(body, None, document, whole=document, sliced=False)
    assert simulator is not None
    return simulator.continue_until_breakpoint()


# --- pins of confirms, releases and undos, and failures with their effect --------------------------------------

STRICT_CONFIRM = strict(fixture(), "source-system.confirm-hold", ["confirmedAt"])["definitions"]
CONFIRMED = {"confirmedAt": "2026-11-24T00:00:00Z"}


def test_a_phase_pin_that_fits_its_phase_is_used():
    # The pin fits the confirm and not the hold it confirms, whose output requires holdId. The undo's pin fits neither
    # the undo nor its step, and the undo never runs: it is left out alone.
    pins = {"node:hold#confirm": CONFIRMED, "node:add#undo": {"unexpected": 1}}
    body = move_request(move(), definitions=STRICT_CONFIRM, mocks=pins)
    assert execute(body, None).status == "completed"
    view = simulated(body)
    assert [record.state for record in view.transactions] == ["completed"]
    assert [event.data["output"] for event in view.events if event.data.get("node_id") == "hold#confirm"] == [CONFIRMED]


def test_a_phase_pin_that_no_longer_fits_is_left_out_and_the_step_asks_for_data():
    answer = run_move(move(), definitions=STRICT_CONFIRM, mocks={"node:hold#confirm": {}})
    assert answer["status"] == "blocked"
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "hold",
        "instanceKey": "hold#confirm",
        "message": "The pinned output of the confirm of hold doesn't fit its output fields. Edit it, or unpin it.",
    }


def test_a_phase_without_test_data_blocks_and_names_itself():
    answer = run_move(move(), definitions=STRICT_CONFIRM)
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "hold",
        "instanceKey": "hold#confirm",
        "message": "The confirm of hold needs test data.",
    }


def test_a_misfitting_pin_of_a_step_in_a_transaction_asks_to_be_edited_instead_of_refusing_the_run():
    answer = run_move(move(), mocks={"node:hold": {"holdId": 7}})
    assert answer["status"] == "blocked"
    assert (answer["blocked"]["instanceKey"], answer["blocked"]["nodeId"]) == ("", "hold")
    assert "doesn't fit its output fields" in answer["blocked"]["message"]


def test_a_phase_pin_marked_secret_is_refused_with_its_pointer_only():
    definitions = fixture()["definitions"]
    undo = next(item for item in definitions if item["metadata"]["name"] == "reverse-target-addition")
    undo["spec"]["outputSchema"] = {
        "type": "object",
        "properties": {"undone": {"type": "string", "x-secret": True}},
    }
    with pytest.raises(SimulationRefused) as refused:
        run_move(
            move(hold(), reverse_by_call(), submit()),
            definitions=definitions,
            mocks={"node:add#undo": {"undone": "s3cr3t"}},
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-SCHEMA-SECRET_VALUE")
    assert refused.value.paths == ["/mocks/node:add#undo"]
    assert "s3cr3t" not in repr(refused.value.__dict__)


def test_a_secret_in_a_pin_of_a_step_in_a_transaction_is_refused_with_its_pointer():
    definitions = fixture()["definitions"]
    held = next(item for item in definitions if item["metadata"]["name"] == "source-system.hold-quantity")
    held["spec"]["outputSchema"]["properties"]["holdId"]["x-secret"] = True
    with pytest.raises(SimulationRefused) as refused:
        run_move(move(), definitions=definitions)
    assert (refused.value.code, refused.value.paths) == ("WV-SCHEMA-SECRET_VALUE", ["/mocks/node:hold"])


def test_a_later_undo_actions_mock_never_blocks_executing_an_earlier_step():
    removed = {"action:target-system.remove-quantity@1.0.0": {}}
    answer = run_move(move(), mocks=removed, target={"mode": "step", "stepId": "hold"})
    assert answer["status"] == "completed"
    assert [entry["nodeId"] for entry in answer["trace"]] == ["move", "hold"]


def test_the_host_scripts_failures_like_a_session():
    answer = run_move(move(), errors={"node:add": {"code": "UNAVAILABLE"}})
    assert answer["status"] == "failed"
    undone = [item for item in answer["diagnostics"] if item["code"] == "WV-TXN-ROLLED-BACK"]
    assert [item["message"] for item in undone] == ["The transaction was undone."]


def test_a_failure_takes_its_effect():
    body = move_request(move(), errors={"node:submit": {"code": "REFUSED", "effect": "none"}})
    # Nothing was applied, so the steps before it are undone and the step that can't be undone isn't in doubt.
    [undone] = simulated(body).transactions
    assert undone.state == "compensated"
    assert [(item.instance_key, item.status, item.effect) for item in undone.participants] == [
        ("hold", "cancelled", "none"),
        ("add", "undone", "none"),
        ("submit", "failed", "none"),
    ]
    in_doubt = simulated(move_request(move(), errors={"node:submit": {"code": "REFUSED", "effect": "unknown"}}))
    assert [record.state for record in in_doubt.transactions] == ["in_doubt"]


def test_an_undo_takes_a_simulated_failure():
    errors = {"node:submit": {"code": "REFUSED", "effect": "none"}, "node:add#undo": {"code": "DOWN", "attempts": 10}}
    [left] = simulated(move_request(move(), errors=errors)).transactions
    assert (left.state, left.incident_key) == ("in_doubt", "add#undo:10")


def test_a_failure_with_no_effect_and_an_unknown_outcome_is_refused_by_the_simulation_too():
    # The request's model refuses it first; whatever gets past it is refused before anything runs.
    unchecked = ScriptedFailure.model_construct(code="REFUSED", outcome="unknown", attempts=1, effect="none")
    body = move_request(move(), errors={"node:submit": {"code": "REFUSED"}})
    with pytest.raises(SimulationRefused) as refused:
        execute(body.model_copy(update={"errors": {"node:submit": unchecked}}), None)
    assert (refused.value.status, refused.value.code) == (422, "WV-DEBUG-MOCK-OUTPUT")


# --- the answer: transactions, and their confirms, releases and undos -------------------------------------------

STUCK = {**REFUSED, "node:add#undo": {"code": "UNAVAILABLE", "attempts": 10}}
IN_DOUBT = "A transaction is in doubt, so the simulated run stopped."


def keys(answer: dict[str, Any]) -> list[str]:
    return [phase["instanceKey"] for phase in answer.get("phases", [])]


def participants(answer: dict[str, Any]) -> dict[str, str]:
    [record] = answer["transactions"]
    return {item["instanceKey"]: item["status"] for item in record["participants"]}


def incidents(answer: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["code"], item["message"]) for item in answer["diagnostics"] if item["code"] in IN_DOUBT_CODES]


def definition(definitions: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in definitions if item["metadata"]["name"] == name)


def test_executing_a_workflow_with_a_transaction_returns_its_state_and_phases():
    answer = run_move(move(), errors=REFUSED)
    assert answer["status"] == "failed"
    step = {"effect": "none", "attempts": 1}
    assert answer["transactions"] == [
        {
            "instanceKey": "move",
            "nodeId": "move",
            "state": "compensated",
            "cause": {"code": "REFUSED", "instanceKey": "submit", "effect": "none"},
            "participants": [
                {"nodeId": "hold", "instanceKey": "hold", "mode": "reserve", "status": "cancelled", **step},
                {"nodeId": "add", "instanceKey": "add", "mode": "compensate", "status": "undone", **step},
                # A definite failure applied nothing: it failed, and no undo runs for it.
                {
                    "nodeId": "submit",
                    "instanceKey": "submit",
                    "mode": "irreversible",
                    "status": "failed",
                    "effect": "none",
                },
            ],
        }
    ]
    submitted = next(entry["index"] for entry in answer["trace"] if entry["nodeId"] == "submit")
    assert answer["phases"] == [
        {
            "afterIndex": submitted,
            "nodeId": "add",
            "instanceKey": "add#undo",
            "phase": "undo",
            "status": "completed",
            "input": {"reference": "M-1", "addOperation": f"{SESSION_RUN_ID}:add"},
            "output": {},
        },
        {
            "afterIndex": submitted,
            "nodeId": "hold",
            "instanceKey": "hold#cancel",
            "phase": "cancel",
            "status": "completed",
            "input": {"reference": "M-1", "holdOperation": f"{SESSION_RUN_ID}:hold"},
            "output": {},
        },
    ]


def test_the_trace_is_unchanged_by_phases():
    answer = run_move(move(), errors=REFUSED)
    assert keys(answer) == ["add#undo", "hold#cancel"]
    assert [entry["nodeId"] for entry in answer["trace"]] == ["move", "hold", "add", "submit"]
    assert not [entry for entry in answer["trace"] if "#" in entry["instanceKey"]]


def test_a_transaction_in_doubt_is_reported_as_failed_with_its_incident():
    answer = run_move(move(), errors=STUCK)
    assert answer["status"] == "failed"
    [record] = answer["transactions"]
    assert (record["state"], record["incidentKey"]) == ("in_doubt", "add#undo:10")
    assert incidents(answer) == [("WV-TXN-UNDO-STUCK", IN_DOUBT)]


def test_a_stuck_undo_stops_the_undo_of_earlier_steps():
    answer = run_move(move(), errors=STUCK)
    # The undos run one at a time, last step first: hold keeps its reservation while add's undo is stuck.
    assert [(phase["instanceKey"], phase["status"]) for phase in answer["phases"]] == [("add#undo", "failed")]
    assert participants(answer) == {"hold": "done", "add": "undo_stuck", "submit": "failed"}
    [left] = answer["transactions"]
    assert [item.get("attempts") for item in left["participants"]] == [None, 10, None]


def test_a_completed_transaction_lists_its_confirms():
    answer = run_move(move())
    assert answer["status"] == "completed"
    assert [record["state"] for record in answer["transactions"]] == ["completed"]
    assert participants(answer) == {"hold": "confirmed", "add": "done", "submit": "done"}
    [confirm] = answer["phases"]
    assert (confirm["instanceKey"], confirm["phase"], confirm["status"]) == ("hold#confirm", "confirm", "completed")
    assert confirm["input"] == {"holdId": "h-1"}
    assert answer["trace"][confirm["afterIndex"]]["nodeId"] == "submit"


def test_an_unknown_outcome_of_the_step_that_cant_be_undone_is_in_doubt():
    answer = run_move(move(), errors={"node:submit": {"code": "UNAVAILABLE", "outcome": "unknown"}})
    assert answer["status"] == "failed"
    [record] = answer["transactions"]
    assert (record["state"], record["incidentKey"]) == ("in_doubt", "submit:1")
    assert participants(answer) == {"hold": "done", "add": "done", "submit": "unknown"}
    assert incidents(answer) == [("WV-TXN-PIVOT-UNKNOWN", IN_DOUBT)]
    assert "phases" not in answer


def test_each_item_of_a_loop_has_its_own_transaction_and_its_phases_in_run_order():
    loop = {**each([transaction([hold(), add(), submit()], onError="continue")], concurrency=1), "maxItems": 5}
    answer = run_move(
        source(loop),
        input={"moveId": "M-1", "quantity": 2, "items": [1, 2, 3]},
        errors={"node:submit[1]": {"code": "REFUSED", "effect": "none"}},
        mocks={"node:submit": {"orderId": "o-1"}},
    )
    assert answer["status"] == "completed"
    assert [(record["instanceKey"], record["state"]) for record in answer["transactions"]] == [
        ("move[0]", "completed"),
        ("move[1]", "compensated"),
        ("move[2]", "completed"),
    ]
    assert keys(answer) == ["hold[0]#confirm", "add[1]#undo", "hold[1]#cancel", "hold[2]#confirm"]
    after = [answer["trace"][phase["afterIndex"]]["instanceKey"] for phase in answer["phases"]]
    assert after == ["submit[0]", "submit[1]", "submit[1]", "submit[2]"]


def test_a_confirm_that_needs_test_data_is_waiting():
    definitions = strict(fixture(), "source-system.confirm-hold", ["confirmedAt"])["definitions"]
    answer = run_move(move(), definitions=definitions)
    assert (answer["status"], answer["blocked"]["instanceKey"]) == ("blocked", "hold#confirm")
    assert [(phase["instanceKey"], phase["status"]) for phase in answer["phases"]] == [("hold#confirm", "waiting")]
    assert answer["phases"][0]["input"] == {"holdId": "h-1"}
    assert [record["state"] for record in answer["transactions"]] == ["committing"]


def undo_from_output() -> tuple[str, list[dict[str, Any]]]:
    """A move whose add is undone with what add returned, and the fixture definitions that allow it."""
    definitions = fixture()["definitions"]
    output = definition(definitions, "target-system.add-quantity")["spec"]["outputSchema"]
    output["properties"]["undo"] = {"type": "object"}
    step = add()
    step["compensate"]["with"] = {"ref": "/steps/add/output/undo"}
    return move(hold(), step, submit()), definitions


def test_an_undo_whose_input_cant_be_built_failed_before_it_was_sent():
    workflow, definitions = undo_from_output()
    answer = run_move(workflow, definitions=definitions, errors=REFUSED)
    [phase] = answer["phases"]
    assert (phase["instanceKey"], phase["status"]) == ("add#undo", "failed")
    assert "input" not in phase and "output" not in phase
    assert participants(answer)["add"] == "undo_stuck"


def test_a_phase_input_with_a_secret_marked_value_is_refused_by_pointer():
    workflow, definitions = undo_from_output()
    remove = definition(definitions, "target-system.remove-quantity")["spec"]["inputSchema"]
    remove["properties"]["token"] = {"type": "string", "x-secret": True}
    undo = {"reference": "M-1", "addOperation": "add-1", "token": "s3cr3t"}
    with pytest.raises(SimulationRefused) as refused:
        run_move(workflow, definitions=definitions, errors=REFUSED, mocks={"node:add": {"id": "a-1", "undo": undo}})
    assert (refused.value.status, refused.value.code) == (422, "WV-SCHEMA-SECRET_VALUE")
    assert refused.value.paths == ["/phases/add#undo/input"]
    assert "s3cr3t" not in repr(refused.value.__dict__)


def nested(depth: int) -> Any:
    value: Any = "s3cr3t"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("direction", ["input", "output"])
def test_the_host_checks_what_a_phase_received_and_returned_by_its_own_operation(direction):
    definitions = fixture()["definitions"]
    definition(definitions, "target-system.remove-quantity")["spec"][direction + "Schema"]["properties"]["token"] = {
        "type": "string",
        "x-secret": True,
    }
    document = json.loads(move())
    _, simulator, _ = simulator_for(
        move_request(move(), definitions=definitions), None, document, whole=document, sliced=False
    )
    assert simulator is not None
    undone = {"instanceKey": "add#undo", direction: {"token": "s3cr3t"}}
    held = {"instanceKey": "hold#cancel", direction: {"token": "s3cr3t"}}
    # The release of hold marks nothing secret, so only the undo of add is refused, by its own fields.
    assert simulation.secret_phases(simulator.data.artifact, [held, undone]) == [f"/phases/add#undo/{direction}"]
    # A value too deep to classify is never passed on either.
    deep = {"instanceKey": "hold#cancel", direction: {"reference": nested(500)}}
    assert simulation.secret_phases(simulator.data.artifact, [deep]) == [f"/phases/hold#cancel/{direction}"]


def test_the_host_refuses_every_value_its_own_check_of_the_phases_finds(monkeypatch):
    # The runtime refuses such a value before the host sees it, so the check's finding is given here.
    checked: list[str] = []

    def found(_artifact: Any, phases: Any) -> list[str]:
        checked.extend(phase["instanceKey"] for phase in phases)
        return ["/phases/add#undo/output"]

    monkeypatch.setattr(simulation, "secret_phases", found)
    with pytest.raises(SimulationRefused) as refused:
        run_move(move(), errors=REFUSED)
    assert (refused.value.code, refused.value.paths) == ("WV-SCHEMA-SECRET_VALUE", ["/phases/add#undo/output"])
    assert checked == ["add#undo", "hold#cancel"]


def test_the_answer_carries_no_business_key():
    answer = run_move(move(), errors=STUCK, input={"moveId": "customer-7421", "quantity": 2})
    assert [record["state"] for record in answer["transactions"]] == ["in_doubt"]
    assert "customer-7421" not in json.dumps(answer["transactions"])
    assert str(SESSION_RUN_ID) not in json.dumps(answer["transactions"])
    [record] = answer["transactions"]
    assert set(record) == {"instanceKey", "nodeId", "state", "cause", "incidentKey", "participants"}


def test_a_cause_with_a_code_of_the_authors_own_spelling_is_left_out():
    # A fail step's code is any name; the answer only carries codes spelled as the platform's and actions' are.
    answer = run_move(move(hold(), add(), {**STOP, "code": "not-accepted"}))
    [record] = answer["transactions"]
    assert (answer["status"], record["state"]) == ("failed", "compensated")
    assert "cause" not in record
    kept = run_move(move(hold(), add(), STOP))["transactions"][0]
    assert kept["cause"]["code"] == "STOPPED"


def three_items(monkeypatch, limit: int) -> dict[str, Any]:
    """The answer about a loop of three transactions with two undos each, when an answer holds ``limit`` entries."""
    monkeypatch.setattr(local_contracts, "MAX_TRANSACTION_ENTRIES", limit)
    loop = {**each([transaction([hold(), add(), submit()], onError="continue")]), "maxItems": 5}
    return run_move(source(loop), input={"moveId": "M-1", "quantity": 2, "items": [1, 2, 3]}, errors=REFUSED)


def test_more_than_a_thousand_phases_answers_limit(monkeypatch):
    # Six undos: one more than the answer may hold stops it, and all of them fit a limit of six. The limit is 1,000
    # (the contract's own test); a run of 501 transactions takes most of a minute.
    answer = three_items(monkeypatch, 5)
    assert answer["status"] == "limit"
    assert (answer["trace"], "phases" in answer, "transactions" in answer) == ([], False, False)
    assert len(three_items(monkeypatch, 6)["phases"]) == 6


def test_more_than_a_thousand_transactions_answers_limit(monkeypatch):
    loop = {**each([transaction([{"id": "note", "kind": "transform", "value": {"literal": 1}}])]), "maxItems": 5}
    items = {"input": {"moveId": "M-1", "quantity": 2, "items": [1, 2, 3]}}
    monkeypatch.setattr(local_contracts, "MAX_TRANSACTION_ENTRIES", 2)
    assert run_move(source(loop), **items)["status"] == "limit"
    monkeypatch.setattr(local_contracts, "MAX_TRANSACTION_ENTRIES", 3)
    assert len(run_move(source(loop), **items)["transactions"]) == 3


def undone_with(kibibytes: int) -> dict[str, Any]:
    """The answer about three undos that each return this much."""
    definitions = fixture()["definitions"]
    definition(definitions, "target-system.remove-quantity")["spec"]["outputSchema"] = {"type": "object"}
    steps = [add(), add("again"), add("more")]
    returned = {"note": "x" * (kibibytes * 1024)}
    return run_move(
        move(*steps, submit()),
        definitions=definitions,
        errors=REFUSED,
        mocks={
            "node:again": {"id": "a-2"},
            "node:more": {"id": "a-3"},
            **{f"node:{step['id']}#undo": returned for step in steps},
        },
    )


def test_undo_steps_with_large_values_answer_limit():
    answer = undone_with(400)
    assert answer["status"] == "limit"
    assert (answer["trace"], "phases" in answer, "transactions" in answer) == ([], False, False)
    kept = undone_with(300)
    assert [len(phase["output"]["note"]) for phase in kept["phases"]] == [300 * 1024] * 3


def test_a_called_undo_in_the_request_is_one_phase_and_no_frame():
    reversed_ = {"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}
    answer = answered(undo_by_call_request(reversed_, output={"ref": "/steps/done/output"}))
    assert [(phase["instanceKey"], phase["status"], phase["output"]) for phase in answer["phases"]] == [
        ("add#undo", "completed", {"undone": True}),
        ("hold#cancel", "completed", {}),
    ]
    assert participants(answer) == {"hold": "cancelled", "add": "undone", "submit": "failed"}
    # The undo's own steps are its business: neither the trace nor the frames name them.
    assert [entry["nodeId"] for entry in answer["trace"]] == ["move", "hold", "add", "submit"]
    assert "frames" not in answer


def test_a_called_undo_that_fails_leaves_the_transaction_in_doubt():
    answer = answered(undo_by_call_request(STOP))
    assert [(phase["instanceKey"], phase["status"]) for phase in answer["phases"]] == [("add#undo", "failed")]
    assert incidents(answer) == [("WV-TXN-UNDO-STUCK", IN_DOUBT)]
    assert answer["status"] == "failed"


def test_a_called_undo_that_needs_test_data_blocks_at_its_undo():
    answer = answered(undo_by_call_request(LOOKUP))
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "add",
        "instanceKey": "add#undo",
        "message": "The undo of add needs test data: step lookup of reverse-target-addition has none.",
    }
    assert [(phase["instanceKey"], phase["status"]) for phase in answer["phases"]] == [("add#undo", "waiting")]


def inside_a_call(*steps: dict[str, Any], definitions: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """The answer about a workflow that calls one with a transaction of ``steps``; the request brings it along."""
    called = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "inside", "version": "1.0.0"},
        "spec": {**json.loads(move(*steps))["spec"], "callable": {}, "timeoutSeconds": 300},
    }
    call = {
        "id": "call",
        "kind": "callWorkflow",
        "uses": "inside@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
    }
    mocks = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}}
    return run_move(
        source(call),
        definitions=[*definitions, called],
        workflows={"inside@1.0.0": {"mocks": mocks}, **fields.pop("workflows", {})},
        **fields,
    )


def test_a_confirm_in_a_called_workflow_that_needs_test_data_names_both():
    definitions = strict(fixture(), "source-system.confirm-hold", ["confirmedAt"])["definitions"]
    answer = inside_a_call(hold(), add(), definitions=definitions)
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "hold",
        "instanceKey": "hold#confirm",
        "message": "The confirm of hold in inside needs test data.",
        "frame": "call",
    }
    # A called workflow's own transactions stay its business: the answer lists the simulated workflow's.
    assert not {"transactions", "phases"} & set(answer)


def test_a_called_undo_in_a_called_workflow_blocks_at_its_undo_in_that_workflow():
    undo, _ = callee(UNDO, [LOOKUP])
    others = [item for item in fixture()["definitions"] if item["metadata"]["name"] != UNDO]
    answer = inside_a_call(
        hold(),
        reverse_by_call(),
        STOP,
        definitions=[*others, undo],
        workflows={f"{UNDO}@1.0.0": {"mocks": {}}},
    )
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "add",
        "instanceKey": "add#undo",
        "message": "The undo of add needs test data: step lookup of reverse-target-addition has none.",
        "frame": "call",
    }
    assert [frame["key"] for frame in answer["frames"]] == ["call"]


def test_phases_keep_their_place_after_a_called_workflow_ran():
    lookup, _ = callee("look-up", [LOOKUP], input_schema={"type": "object"})
    call = {
        "id": "call",
        "kind": "callWorkflow",
        "uses": "look-up@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
    }
    answer = run_move(
        source(call, transaction([hold(), add(), submit()])),
        definitions=[*fixture()["definitions"], lookup],
        workflows={"look-up@1.0.0": {"mocks": {"node:lookup": {"count": 1}}}},
        errors=REFUSED,
    )
    # The called workflow's own events are not the simulated workflow's: the undos still come after submit.
    assert [entry["nodeId"] for entry in answer["trace"]] == ["call", "lookup", "move", "hold", "add", "submit"]
    assert [(phase["instanceKey"], phase["afterIndex"]) for phase in answer["phases"]] == [
        ("add#undo", 5),
        ("hold#cancel", 5),
    ]


def test_only_a_workflow_with_transactions_answers_with_them():
    note = {"id": "note", "kind": "transform", "value": {"literal": 1}}
    grouped = run_move(move(note))
    assert [(record["state"], record["participants"]) for record in grouped["transactions"]] == [("completed", [])]
    # Nothing to confirm, release or undo, so no phases either.
    assert "phases" not in grouped
    assert not {"transactions", "phases"} & set(run_move(source(note)))


def test_the_host_route_answers_a_called_undo_in_the_request(tmp_path):
    reversed_ = {"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}
    request = undo_by_call_request(reversed_, output={"ref": "/steps/done/output"})
    (tmp_path / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    origin, code = "http://127.0.0.1:8766", "terminal-only-pairing-code"
    studio = make_studio_app(StudioOptions(origin=origin, assets=tmp_path, pairing_code=code))
    with TestClient(studio, base_url=origin) as browser:
        session = browser.post("/studio/session", json={"code": code}, headers={"Origin": origin}).json()
        answer = browser.post(
            "/studio/local/debug/execute",
            content=request.model_dump_json(exclude_unset=True),
            headers={"Origin": origin, "X-Weave-CSRF": session["csrfToken"]},
        )
        assert answer.status_code == 200, answer.text
        assert [record["state"] for record in answer.json()["transactions"]] == ["compensated"]
        assert [phase["instanceKey"] for phase in answer.json()["phases"]] == ["add#undo", "hold#cancel"]
