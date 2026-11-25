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

from simulation_support import STOP, callee, each, fixture, parallel, strict  # noqa: E402
from transaction_support import add, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.contracts.transactions import IN_DOUBT_CODES  # noqa: E402
from firefly_weave.operations.debug.transactions import SESSION_RUN_ID  # noqa: E402
from firefly_weave.studio import debug_sessions, local_contracts, simulation  # noqa: E402
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


def test_a_cause_leaves_out_a_code_of_the_authors_own_spelling_and_keeps_its_step_and_effect():
    # A fail step's code is any name; the answer only carries codes spelled as the platform's and actions' are.
    answer = run_move(move(hold(), add(), {**STOP, "code": "not-accepted"}))
    [record] = answer["transactions"]
    assert (answer["status"], record["state"]) == ("failed", "compensated")
    assert record["cause"] == {"instanceKey": "stop", "effect": "none"}
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


def inside_a_call(
    *steps: dict[str, Any], definitions: list[dict[str, Any]], timeout: int | None = 300, **fields: Any
) -> dict[str, Any]:
    """The answer about a workflow that calls one with a transaction of ``steps``; the request brings it along."""
    called = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "inside", "version": "1.0.0"},
        "spec": {**json.loads(move(*steps))["spec"], "callable": {}},
    }
    if timeout is not None:
        called["spec"]["timeoutSeconds"] = timeout
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


def over_the_route(tmp_path: Path, body: str, path: str = "execute") -> Any:
    """The host's HTTP answer to a request, from a paired browser."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    origin, code = "http://127.0.0.1:8766", "terminal-only-pairing-code"
    studio = make_studio_app(StudioOptions(origin=origin, assets=tmp_path, pairing_code=code))
    with TestClient(studio, base_url=origin, raise_server_exceptions=False) as browser:
        session = browser.post("/studio/session", json={"code": code}, headers={"Origin": origin}).json()
        return browser.post(
            f"/studio/local/debug/{path}",
            content=body,
            headers={"Origin": origin, "X-Weave-CSRF": session["csrfToken"]},
        )


def test_the_host_route_answers_a_called_undo_in_the_request(tmp_path):
    reversed_ = {"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}
    request = undo_by_call_request(reversed_, output={"ref": "/steps/done/output"})
    answer = over_the_route(tmp_path, request.model_dump_json(exclude_unset=True))
    assert answer.status_code == 200, answer.text
    assert [record["state"] for record in answer.json()["transactions"]] == ["compensated"]
    assert [phase["instanceKey"] for phase in answer.json()["phases"]] == ["add#undo", "hold#cancel"]


def test_the_records_the_host_keeps_of_transactions_hold_no_values():
    # An undo that reads what add returned: the runtime's records hold that output, and the business key.
    workflow, definitions = undo_from_output()
    loop = {**each([json.loads(workflow)["spec"]["steps"][0]], concurrency=1), "maxItems": 5}
    body = move_request(
        source(loop),
        definitions=definitions,
        input={"moveId": "customer-7421", "quantity": 2, "items": [1]},
        mocks={"node:add": {"id": "returned-9", "undo": {}}, "node:submit": {"orderId": "o-1"}},
    )
    document = json.loads(body.source)
    _, simulator, _ = simulator_for(body, None, document, whole=document, sliced=False)
    assert simulator is not None
    simulator.set_breakpoints({"submit[0]"})
    simulator.continue_until_breakpoint()
    [open_one] = simulator.data.cursor.result.state.transactions.values()
    assert "returned-9" in open_one.model_dump_json() and open_one.business_key == "customer-7421"
    while_open = simulator.transactions_of("")
    simulator.continue_until_breakpoint()
    # The loop released its record; the host kept the last one.
    assert simulator.data.state.transactions == {}
    for records in (while_open, simulator.transactions_of("")):
        assert [item.status for item in records["move[0]"].participants][:2] in (
            ["done", "done"],
            ["confirmed", "done"],
        )
        kept = records["move[0]"].model_dump_json()
        assert "returned-9" not in kept and "customer-7421" not in kept


# --- the clock: waits of called workflows, and what a transaction needs to end ----------------------------------

PERSON = {
    "id": "ask",
    "kind": "humanTask",
    "assignment": "reviewers",
    "title": {"literal": "Approve"},
    "context": {"literal": {}},
    "formSchema": {"type": "object"},
    "decisions": ["approve", "reject"],
}
DONE = {"id": "done", "kind": "transform", "value": {"literal": {}}}


def wait(seconds: int, identifier: str = "pause") -> dict[str, Any]:
    return {"id": identifier, "kind": "wait", "durationSeconds": seconds}


def called_step(**settings: Any) -> dict[str, Any]:
    """A step that calls the workflow ``inside``, undone as add is."""
    values = {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}
    call = {"id": "call", "kind": "callWorkflow", "uses": "inside@1.0.0", "with": {"object": values}}
    return {**call, "compensate": add()["compensate"], **settings}


def run_calling(workflow: str, *steps: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """The answer about ``workflow`` when the request brings ``inside``, a called workflow of ``steps``."""
    inside, _ = callee("inside", list(steps))
    return run_move(
        workflow,
        definitions=[*fixture()["definitions"], inside],
        workflows={"inside@1.0.0": {"mocks": {}}},
        **fields,
    )


def entries(answer: dict[str, Any]) -> list[tuple[str | None, str, str]]:
    return [(entry.get("frame"), entry["instanceKey"] or entry["nodeId"], entry["status"]) for entry in answer["trace"]]


@pytest.mark.parametrize("before", [hold, add], ids=["a reservation", "a step with an undo"])
def test_a_called_step_that_waits_ends_before_its_transaction_does(before):
    answer = run_calling(move(before(), called_step(), submit()), wait(100), DONE)
    assert answer["status"] == "completed"
    [record] = answer["transactions"]
    assert record["state"] == "completed"
    assert participants(answer)["call"] == "done"
    assert [entry["status"] for entry in answer["trace"]] == ["completed"] * 6


@pytest.mark.parametrize("before", [[hold()], []], ids=["after a reservation", "alone"])
def test_a_called_undo_that_waits_is_undone_at_its_end(before):
    body = undo_by_call_request(wait(100), DONE)
    document = json.loads(body.source)
    document["spec"]["steps"][0]["steps"][0:1] = before
    mocks = {key: value for key, value in body.mocks.items() if before or key != "node:hold"}
    answer = answered(body.model_copy(update={"source": json.dumps(document), "mocks": mocks}))
    assert [item["code"] for item in answer["diagnostics"]] == ["WV-TXN-ROLLED-BACK"]
    assert [record["state"] for record in answer["transactions"]] == ["compensated"]
    assert participants(answer)["add"] == "undone"
    assert (answer["phases"][0]["instanceKey"], answer["phases"][0]["status"]) == ("add#undo", "completed")


@pytest.mark.parametrize("after", [600, 300], ids=["another moment", "the same moment"])
def test_the_clock_passes_a_wait_that_an_undo_already_stopped(after):
    # The pause is stopped with its transaction; its deadline is no run's to take, so it never holds the clock.
    branches = parallel(x=[add()], y=[wait(300)])
    workflow = source(transaction([hold(), branches], onError="continue"), wait(after, "long"), wait(10, "more"), DONE)
    answer = run_move(workflow, errors={"node:add": {"code": "UNAVAILABLE"}})
    assert answer["status"] == "completed"
    assert entries(answer)[-3:] == [
        (None, "long", "completed"),
        (None, "more", "completed"),
        (None, "done", "completed"),
    ]


def test_a_transaction_that_didnt_settle_never_answers_completed(monkeypatch):
    # The simulation gives up after so many moves of the clock.
    monkeypatch.setattr(simulation, "ROUNDS", 1)
    answer = run_move(move(hold(), wait(60), submit()))
    assert [record["state"] for record in answer["transactions"]] == ["open"]
    assert answer["status"] == "failed"
    assert [(item["code"], item["message"]) for item in answer["diagnostics"]] == [
        ("WV-STUDIO-SIMULATION", "Studio couldn't simulate transaction move, so the simulated run stopped there.")
    ]


def test_a_transaction_in_a_called_workflow_that_didnt_settle_never_answers_completed():
    # Nobody can answer a person's task inside a called workflow, and nothing bounds this one.
    answer = inside_a_call(
        add(),
        PERSON,
        definitions=fixture()["definitions"],
        timeout=None,
        workflows={"inside@1.0.0": {"mocks": {"node:add": {"id": "a-1"}}}},
    )
    assert answer["status"] == "failed"
    assert [(item["code"], item["message"]) for item in answer["diagnostics"]] == [
        (
            "WV-STUDIO-SIMULATION",
            "Studio couldn't simulate transaction move of inside, so the simulated run stopped there.",
        )
    ]


# --- which step failed ------------------------------------------------------------------------------------------

THREE = {"moveId": "M-1", "quantity": 2, "items": [1, 2, 3]}
EVERY_ITEM = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}
SECOND_REFUSED = {"node:submit[1]": {"code": "REFUSED", "effect": "none"}}
NOTE = {"id": "note", "kind": "transform", "value": {"literal": 1}}


def looped(*steps: dict[str, Any], concurrency: int = 3, **settings: Any) -> str:
    """A loop over the items with one transaction for each: hold, add and submit, or the steps given."""
    body = transaction(list(steps) or [hold(), add(), submit()], **settings)
    return source({**each([body], concurrency=concurrency), "maxItems": 5})


def failures(answer: dict[str, Any], frame: str | None = None) -> dict[str, str | None]:
    """The entries of a run that failed, each with its error's code."""
    return {
        entry["instanceKey"] or entry["nodeId"]: (entry.get("error") or {}).get("code")
        for entry in answer["trace"]
        if entry["status"] == "failed" and entry.get("frame") == frame
    }


ERROR_PATH = {"onError": "errorOutput", "errorPath": {"steps": [NOTE], "output": {"literal": None}}}


@pytest.mark.parametrize("setting", [{"onError": "continue"}, ERROR_PATH], ids=["continue", "errorOutput"])
def test_the_step_that_failed_in_a_transaction_whose_failure_is_handled_failed_too(setting):
    answer = run_move(move(**setting), errors=REFUSED)
    assert answer["status"] == "completed"
    assert failures(answer) == {"move": "WV-TXN-ROLLED-BACK", "submit": "REFUSED"}
    [submitted] = [entry for entry in answer["trace"] if entry["nodeId"] == "submit"]
    assert submitted["error"] == {
        "code": "REFUSED",
        "message": "The step failed with code REFUSED.",
        "outcome": "failed",
        "attempts": 1,
        "instanceKey": "submit",
    }
    assert "handled" not in submitted


def test_a_step_that_fails_after_a_handled_transaction_is_the_one_that_stopped_the_run():
    later = {"id": "later", "kind": "action", "uses": "target-system.add-quantity@1.0.0", "with": add()["with"]}
    workflow = source(transaction([hold(), add(), submit()], onError="continue"), later)
    answer = run_move(workflow, errors={**REFUSED, "node:later": {"code": "VALIDATION"}})
    assert answer["status"] == "failed"
    assert failures(answer) == {"move": "WV-TXN-ROLLED-BACK", "submit": "REFUSED", "later": "VALIDATION"}


def test_a_step_a_handled_transaction_stopped_never_stopped_the_run():
    # The workflow's output can't be built, after a transaction that left a wait behind when it was undone.
    branches = parallel(x=[add()], y=[wait(300)])
    document = json.loads(source(transaction([hold(), branches], onError="continue")))
    document["spec"]["output"] = {"ref": "/input/items/5"}
    answer = run_move(json.dumps(document), errors={"node:add": {"code": "UNAVAILABLE"}})
    assert answer["status"] == "failed"
    assert [entry["nodeId"] for entry in answer["trace"]][-1] == "pause"
    assert failures(answer) == {"move": "WV-TXN-ROLLED-BACK", "add": "UNAVAILABLE"}


def test_a_transform_that_fails_in_a_transaction_carries_its_error():
    missing = {**NOTE, "value": {"ref": "/input/items/5"}}
    answer = run_move(move(hold(), add(), missing))
    assert failures(answer) == {"note": "WV-EXPR-MISSING"}
    assert (answer["trace"][-1]["error"]["outcome"], answer["trace"][-1]["error"]["attempts"]) == ("not_started", 1)
    handled = run_move(move(hold(), add(), NOTE, onError="continue"), errors={"node:note": {"code": "BAD"}})
    assert failures(handled) == {"move": "WV-TXN-ROLLED-BACK", "note": "BAD"}


def test_a_fail_step_in_a_transaction_failed_with_its_code():
    assert failures(run_move(move(hold(), STOP))) == {"stop": "STOPPED"}
    # A code of the author's own spelling is left out of the answer, here as in the transaction's cause.
    assert failures(run_move(move(hold(), {**STOP, "code": "not-accepted"}))) == {"stop": None}


@pytest.mark.parametrize("setting", [{}, {"onError": "continue"}], ids=["stops the run", "continues"])
def test_the_item_whose_transaction_failed_failed_although_its_siblings_settled_first(setting):
    answer = run_move(looped(**setting), input=THREE, mocks=EVERY_ITEM, errors=SECOND_REFUSED)
    handled = {"move[1]": "WV-TXN-ROLLED-BACK"} if setting else {}
    assert failures(answer) == {**handled, "submit[1]": "REFUSED"}


def test_a_step_that_failed_in_a_parallel_step_failed_and_the_one_it_stopped_did_not():
    branches = parallel(x=[add()], y=[wait(300)])
    answer = run_move(move(hold(), branches), errors={"node:add": {"code": "UNAVAILABLE"}})
    assert answer["status"] == "failed"
    assert failures(answer) == {"add": "UNAVAILABLE"}
    assert [entry["nodeId"] for entry in answer["trace"]][-1] == "pause"


def test_every_step_of_a_parallel_step_that_failed_failed():
    branches = parallel(x=[add()], y=[add("other")])
    errors = {"node:add": {"code": "UNAVAILABLE"}, "node:other": {"code": "REJECTED"}}
    answer = run_move(move(hold(), branches), errors=errors)
    # The transaction names the first only; the second reported its failure while it was being undone.
    assert answer["transactions"][0]["cause"]["code"] == "UNAVAILABLE"
    assert failures(answer) == {"add": "UNAVAILABLE", "other": "REJECTED"}


def test_a_transaction_undone_because_the_run_ends_didnt_fail():
    # The second item's failure ends the run while the other two still wait, so they are undone too.
    answer = run_move(
        looped(hold(), add(), wait(60)),
        input=THREE,
        mocks={key: value for key, value in EVERY_ITEM.items() if key != "node:submit"},
        errors={"node:add[1]": {"code": "UNAVAILABLE"}},
    )
    assert [record["state"] for record in answer["transactions"]] == ["compensated"] * 3
    assert failures(answer) == {"add[1]": "UNAVAILABLE"}


def test_a_step_of_a_called_workflow_never_takes_the_failure_of_a_step_with_its_id():
    branches = parallel(x=[add()], y=[called_step()])
    answer = run_calling(move(hold(), branches), wait(60, "add"), errors={"node:add": {"code": "UNAVAILABLE"}})
    # The called run was stopped where it waited, when its transaction was undone.
    assert failures(answer, "call") == {"add": None}
    assert failures(answer)["add"] == "UNAVAILABLE"


def test_a_failed_step_of_a_called_workflow_never_takes_the_error_simulated_for_a_step_with_its_id():
    missing = {**NOTE, "value": {"ref": "/input/items/5"}}
    called = {**json.loads(move(add(), missing)), "metadata": {"name": "inside", "version": "1.0.0"}}
    called["spec"] |= {"callable": {}, "timeoutSeconds": 300}
    call = {key: value for key, value in called_step().items() if key != "compensate"}
    answer = run_move(
        source(transaction([hold(), NOTE], onError="continue"), {**call, "with": {"ref": "/input"}}),
        definitions=[*fixture()["definitions"], called],
        workflows={"inside@1.0.0": {"mocks": {"node:add": {"id": "a-1"}}}},
        errors={"node:note": {"code": "BAD"}},
    )
    assert failures(answer)["note"] == "BAD"
    assert failures(answer, "call") == {"note": "WV-EXPR-MISSING"}


def test_a_step_stopped_in_a_called_workflow_never_takes_the_failure_reported_by_a_step_with_its_id():
    missing = {**NOTE, "value": {"ref": "/input/items/5"}}
    body = move(hold(), parallel(x=[wait(60, "add")], y=[missing]))
    called = {**json.loads(body), "metadata": {"name": "inside", "version": "1.0.0"}}
    called["spec"] |= {"callable": {}, "timeoutSeconds": 300}
    call = {key: value for key, value in called_step().items() if key != "compensate"}
    answer = run_move(
        source(transaction([hold(), add()], onError="continue"), {**call, "with": {"ref": "/input"}}),
        definitions=[*fixture()["definitions"], called],
        workflows={"inside@1.0.0": {"mocks": {"node:hold": {"holdId": "h-1"}}}},
        errors={"node:add": {"code": "UNAVAILABLE"}},
    )
    assert failures(answer)["add"] == "UNAVAILABLE"
    assert failures(answer, "call") == {"note": "WV-EXPR-MISSING"}


def test_an_expired_reservation_is_its_transactions_failure():
    answer = run_move(move(hold(holdSeconds=60), add(), wait(3000)))
    assert failures(answer) == {"move": "WV-TXN-HOLD-EXPIRED"}
    [expired] = [entry for entry in answer["trace"] if entry["nodeId"] == "move"]
    assert expired["error"]["message"] == "Reservations were about to expire before the transaction could finish."


def test_a_step_that_wasnt_started_because_the_reservations_wouldnt_outlast_it_failed():
    answer = run_move(move(hold(holdSeconds=120), wait(60), submit()))
    assert failures(answer) == {"submit": "WV-TXN-HOLD-INSUFFICIENT"}
    assert (answer["trace"][-1]["error"]["outcome"], answer["trace"][-1]["error"]["attempts"]) == ("not_started", 0)


def test_a_called_step_whose_run_failed_carries_its_error():
    answer = run_calling(move(hold(), add(), called_step()), STOP)
    assert failures(answer) == {"call": "STOPPED"}
    [called] = [entry for entry in answer["trace"] if entry["nodeId"] == "call"]
    assert (called["error"]["outcome"], called["error"]["attempts"]) == ("failed", 1)


def test_a_transaction_in_doubt_at_a_confirm_blames_no_step():
    # The first item's reservation can't be confirmed while the steps of the other two are still running.
    stuck = {"node:hold[0]#confirm": {"code": "DOWN", "attempts": 10}}
    workflow = looped(hold(), submit(), wait(60))
    mocks = {key: value for key, value in EVERY_ITEM.items() if key != "node:add"}
    answer = run_move(workflow, input=THREE, mocks=mocks, errors=stuck)
    assert (answer["status"], incidents(answer)[0][0]) == ("failed", "WV-TXN-CONFIRM-STUCK")
    assert failures(answer) == {}


def test_a_step_that_changes_nothing_and_fails_after_the_point_of_no_return_failed():
    branches = parallel(x=[{**NOTE, "value": {"ref": "/input/items/5"}}], y=[wait(300)])
    answer = run_move(move(hold(), submit(), branches))
    assert incidents(answer)[0][0] == "WV-TXN-AFTER-PIVOT-STUCK"
    assert failures(answer) == {"note": "WV-EXPR-MISSING"}


def test_a_step_that_failed_in_a_transaction_of_a_called_workflow_failed_there():
    branches = parallel(x=[STOP], y=[wait(300)])
    answer = inside_a_call(hold(), add(), branches, definitions=fixture()["definitions"])
    assert answer["status"] == "failed"
    assert failures(answer, "call") == {"stop": "STOPPED"}


def test_a_step_that_failed_in_a_transaction_a_called_workflow_handles_failed_there():
    # The loop releases the record of each of its transactions as it ends, so the failure is read from the last one.
    body = transaction([hold(), add(), {**NOTE, "value": {"ref": "/input/items/5"}}], onError="continue")
    inside = json.loads(source(each([body], concurrency=1)))
    called = {**inside, "metadata": {"name": "inside", "version": "1.0.0"}}
    called["spec"] |= {"callable": {}, "timeoutSeconds": 300}
    call = {**called_step(), "with": {"ref": "/input"}}
    del call["compensate"]
    answer = run_move(
        source(call),
        definitions=[*fixture()["definitions"], called],
        workflows={"inside@1.0.0": {"mocks": {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}}}},
        input={**THREE, "items": [1]},
    )
    assert answer["status"] == "completed", answer
    assert failures(answer, "call") == {"move[0]": "WV-TXN-ROLLED-BACK", "note[0]": "WV-EXPR-MISSING"}


def beside(step: dict[str, Any]) -> str:
    """A workflow that runs a step beside a transaction that waits with its reservation made."""
    return source({**parallel(x=[step], y=[transaction([hold(), wait(60, "held"), submit()])]), "id": "outer"})


LATER = {"id": "later", "kind": "action", "uses": "target-system.add-quantity@1.0.0", "with": add()["with"]}
PAID = {"id": "paid", "kind": "signal", "name": "order-paid", "timeoutSeconds": 30, "payloadSchema": {}}
FAILED_LATER = {"errors": {"node:later": {"code": "VALIDATION"}}}


@pytest.mark.parametrize(
    "failing, fields, expected",
    [
        (LATER, FAILED_LATER, {"later": "VALIDATION"}),
        (PAID, {"scripts": {"signals": {"paid": {"outcome": "timeout"}}}}, {"paid": None}),
    ],
    ids=["an action", "a signal that never arrives"],
)
def test_a_step_of_an_open_transaction_is_never_blamed_for_a_failure_outside_of_it(failing, fields, expected):
    answer = run_move(beside(failing), **fields)
    assert answer["status"] == "failed", answer
    assert answer["trace"][-1]["nodeId"] in {"hold", "held"}
    assert failures(answer) == expected


def test_a_step_of_an_open_transaction_is_never_blamed_for_a_called_run_that_failed():
    call = {key: value for key, value in called_step().items() if key != "compensate"}
    answer = run_calling(beside(call), wait(10), STOP)
    assert answer["status"] == "failed", answer
    assert [entry["nodeId"] for entry in answer["trace"] if "frame" not in entry][-1] == "held"
    assert failures(answer) == {"call": None}
    assert failures(answer, "call") == {"stop": None}


def test_the_step_whose_incident_holds_the_run_is_blamed_before_one_that_failed_after_it():
    call = {key: value for key, value in called_step().items() if key != "compensate"}
    branches = parallel(x=[LATER], y=[call], z=[transaction([hold(), submit()])])
    answer = run_calling(source({**branches, "id": "outer"}), STOP, **FAILED_LATER)
    assert answer["status"] == "failed", answer
    assert failures(answer) == {"later": "VALIDATION"}


MISSING = {"id": "missing", "kind": "transform", "value": {"ref": "/input/items/5"}}


@pytest.mark.parametrize(
    "workflow, errors, expected, after",
    [
        (source(transaction([hold(), submit()]), {**MISSING, "onError": "continue"}), {}, ["hold#confirm"], "submit"),
        (
            source(transaction([hold(), add(), submit()], onError="continue"), {**MISSING, "onError": "continue"}),
            REFUSED,
            ["add#undo", "hold#cancel"],
            "submit",
        ),
        # The step is reached, and waits, before the reservation is confirmed.
        (move(hold(), submit(), MISSING), {}, ["hold#confirm"], "missing"),
    ],
    ids=["after a completed transaction", "after an undone transaction", "after the point of no return"],
)
def test_a_phase_is_listed_once_when_the_step_its_result_leads_to_fails(workflow, errors, expected, after):
    # The simulator reduces the event that carries the phase's result again, with that step failing.
    answer = run_move(workflow, errors=errors)
    assert failures(answer)["missing"] == "WV-EXPR-MISSING"
    assert keys(answer) == expected
    # In the place of its result, not of the step that failed because of it.
    index = next(entry["index"] for entry in answer["trace"] if entry["nodeId"] == after)
    assert [phase["afterIndex"] for phase in answer["phases"]] == [index] * len(expected)


# --- Execute step and Before a step, inside a transaction -------------------------------------------------------


def to(step: str, *, mode: str = "step", **instance: str) -> dict[str, Any]:
    return {"mode": mode, "stepId": step, **instance}


def states(answer: dict[str, Any]) -> dict[str, tuple[str, dict[str, str]]]:
    """Each transaction's state and its steps' statuses, by instance key."""
    return {
        record["instanceKey"]: (record["state"], {p["instanceKey"]: p["status"] for p in record["participants"]})
        for record in answer["transactions"]
    }


@pytest.mark.parametrize("step", ["hold", "add", "submit"])
def test_executing_a_step_of_a_transaction_runs_up_to_it_and_leaves_the_transaction_open(step):
    # No confirm is sent, so the confirm that needs test data never asks for any, the point of no return included.
    answer = run_move(move(), definitions=STRICT_CONFIRM, target=to(step))
    assert answer["status"] == "completed"
    steps = ["hold", "add", "submit"][: ["hold", "add", "submit"].index(step) + 1]
    assert states(answer) == {"move": ("open", dict.fromkeys(steps, "done"))}
    assert "phases" not in answer
    assert entries(answer) == [(None, "move", "running"), *((None, name, "completed") for name in steps)]
    assert "output" not in answer["trace"][0]
    assert not [item for item in answer["transactions"][0]["participants"] if item.get("attempts")]


@pytest.mark.parametrize("concurrency", [1, 3], ids=["one at a time", "at once"])
@pytest.mark.parametrize("step", ["add", "submit"])
def test_each_item_of_a_loop_stops_at_the_target(step, concurrency):
    answer = run_move(
        looped(concurrency=concurrency),
        definitions=STRICT_CONFIRM,
        input=THREE,
        mocks=EVERY_ITEM,
        target=to(step),
    )
    assert answer["status"] == "completed"
    steps = ["hold", "add", "submit"][: 2 if step == "add" else 3]
    assert states(answer) == {
        f"move[{item}]": ("open", {f"{name}[{item}]": "done" for name in steps}) for item in range(3)
    }
    assert "phases" not in answer
    assert {key: status for _, key, status in entries(answer) if key.startswith(("move", step))} == {
        **{f"move[{item}]": "running" for item in range(3)},
        **{f"{step}[{item}]": "completed" for item in range(3)},
    }


def test_only_a_failing_target_plays_the_undo():
    assert states(run_move(move(), target=to("submit")))["move"][0] == "open"
    answer = run_move(move(), target=to("submit"), errors=REFUSED)
    assert answer["status"] == "failed"
    assert states(answer) == {"move": ("compensated", {"hold": "cancelled", "add": "undone", "submit": "failed"})}
    assert keys(answer) == ["add#undo", "hold#cancel"]
    assert failures(answer) == {"submit": "REFUSED"}
    earlier = run_move(move(), target=to("submit"), errors={"node:add": {"code": "UNAVAILABLE"}})
    assert (states(earlier)["move"][0], keys(earlier)) == ("compensated", ["add#undo", "hold#cancel"])
    unknown = run_move(move(), target=to("submit"), errors={"node:submit": {"code": "DOWN", "outcome": "unknown"}})
    assert (unknown["status"], states(unknown)["move"][0]) == ("failed", "in_doubt")


def test_a_failing_target_called_by_a_transaction_plays_the_whole_undo():
    call = {**called_step(irreversible=True), "onFailure": "stop"}
    del call["compensate"]
    answer = run_calling(move(hold(), add(), call), STOP, target=to("call"))
    assert states(answer) == {"move": ("compensated", {"hold": "cancelled", "add": "undone", "call": "failed"})}
    succeeded = run_calling(move(hold(), add(), call), DONE, target=to("call"))
    assert states(succeeded) == {"move": ("open", {"hold": "done", "add": "done", "call": "done"})}


def test_an_item_whose_target_fails_is_undone_while_the_others_stop_at_it():
    answer = run_move(
        looped(concurrency=1, onError="continue"),
        definitions=STRICT_CONFIRM,
        input=THREE,
        mocks=EVERY_ITEM,
        errors=SECOND_REFUSED,
        target=to("submit"),
    )
    assert answer["status"] == "completed"
    assert {key: state for key, (state, _) in states(answer).items()} == {
        "move[0]": "open",
        "move[1]": "compensated",
        "move[2]": "open",
    }
    assert keys(answer) == ["add[1]#undo", "hold[1]#cancel"]


def test_the_items_that_reach_the_target_stop_at_it_while_another_is_in_doubt():
    answer = run_move(
        looped(),
        definitions=STRICT_CONFIRM,
        input=THREE,
        mocks=EVERY_ITEM,
        errors={"node:submit[0]": {"code": "DOWN", "outcome": "unknown"}},
        target=to("submit"),
    )
    assert answer["status"] == "failed"
    assert {key: state for key, (state, _) in states(answer).items()} == {
        "move[0]": "in_doubt",
        "move[1]": "open",
        "move[2]": "open",
    }
    assert states(answer)["move[1]"][1] == {"hold[1]": "done", "add[1]": "done", "submit[1]": "done"}
    assert (keys(answer), failures(answer)) == ([], {"submit[0]": "DOWN"})


AFTER = {"id": "after", "kind": "transform", "value": {"literal": 1}}


def test_a_target_outside_of_a_transactions_body_settles_it():
    whole = ("completed", {"hold": "confirmed", "add": "done", "submit": "done"})
    for target, expected in (
        (to("submit"), ("open", {"hold": "done", "add": "done", "submit": "done"})),
        (to("move"), whole),
        (to("after"), whole),
        (to("after", mode="before"), whole),
    ):
        answer = run_move(source(transaction([hold(), add(), submit()]), AFTER), target=target)
        assert (answer["status"], states(answer)) == ("completed", {"move": expected})
        assert keys(answer) == (["hold#confirm"] if expected is whole else [])


def test_a_target_in_a_transactions_error_path_settles_it():
    assert states(run_move(move(**ERROR_PATH), target=to("add")))["move"][0] == "open"
    answer = run_move(move(**ERROR_PATH), errors=REFUSED, target=to("note"))
    assert states(answer)["move"][0] == "compensated"
    assert (keys(answer), entries(answer)[-1]) == (["add#undo", "hold#cancel"], (None, "note", "completed"))
    # One that succeeds is settled too, and the step isn't reached.
    reached = run_move(move(**ERROR_PATH), target=to("note"))
    assert (reached["blocked"]["reason"], states(reached)["move"][0]) == ("path", "completed")


def test_a_transaction_before_the_targets_settles():
    first = transaction([hold("held"), add("added")], identifier="first")
    answer = run_move(
        source(first, transaction([hold(), add(), submit()])),
        mocks={"node:held": {"holdId": "h-0"}, "node:added": {"id": "a-0"}},
        target=to("add"),
    )
    assert {key: state for key, (state, _) in states(answer).items()} == {"first": "completed", "move": "open"}
    assert keys(answer) == ["held#confirm"]


def test_before_a_step_of_one_item_stops_the_items_before_it_at_the_step():
    answer = run_move(
        looped(concurrency=1),
        definitions=STRICT_CONFIRM,
        input=THREE,
        mocks=EVERY_ITEM,
        target=to("submit", mode="before", instanceKey="submit[1]"),
    )
    assert answer["status"] == "completed"
    assert states(answer) == {
        "move[0]": ("open", {"hold[0]": "done", "add[0]": "done", "submit[0]": "done"}),
        "move[1]": ("open", {"hold[1]": "done", "add[1]": "done"}),
    }
    assert "phases" not in answer


def test_a_target_after_the_point_of_no_return_runs_the_confirms_that_come_before_it():
    nt = {
        "id": "tell",
        "kind": "action",
        "uses": "notify.send-message@1.0.0",
        "with": {"object": {"text": {"ref": "/input/moveId"}}},
        "irreversible": True,
    }
    workflow, mocks = move(hold(), submit(), nt), {"node:tell": {}}
    answer = run_move(workflow, mocks=mocks, target=to("tell"))
    assert answer["status"] == "completed"
    assert states(answer) == {"move": ("committing", {"hold": "confirmed", "submit": "done", "tell": "done"})}
    assert (keys(answer), entries(answer)[0]) == (["hold#confirm"], (None, "move", "running"))
    asks = run_move(workflow, mocks=mocks, definitions=STRICT_CONFIRM, target=to("tell"))
    assert (asks["status"], asks["blocked"]["instanceKey"]) == ("blocked", "hold#confirm")


def test_a_target_in_a_loop_inside_a_transaction_runs_every_item():
    inner = {**each([add()], concurrency=2), "maxItems": 5}
    answer = run_move(
        move(hold(), inner, submit()),
        definitions=STRICT_CONFIRM,
        input=THREE,
        mocks={"node:add": {"id": "a-1"}},
        target=to("add"),
    )
    assert answer["status"] == "completed"
    assert states(answer) == {"move": ("open", {"hold": "done", **{f"add[{item}]": "done" for item in range(3)}})}
    assert "phases" not in answer


def test_a_target_on_a_path_not_taken_leaves_the_transaction_open():
    choice = {
        "id": "choice",
        "kind": "switch",
        "cases": [{"when": {"literal": False}, "steps": [add()], "output": {"literal": None}}],
        "default": {"steps": [], "output": {"literal": None}},
    }
    answer = run_move(move(hold(), choice, submit()), definitions=STRICT_CONFIRM, target=to("add"))
    assert (answer["status"], answer["blocked"]["reason"]) == ("blocked", "path")
    assert states(answer) == {"move": ("open", {"hold": "done"})}
    assert "phases" not in answer


def test_an_item_that_stopped_at_the_target_never_expires_while_the_next_ones_wait():
    # Each item waits ten minutes before the target; a reservation lasts fifteen.
    answer = run_move(
        looped(hold(), wait(600), add(), concurrency=1),
        input=THREE,
        mocks={key: value for key, value in EVERY_ITEM.items() if key != "node:submit"},
        target=to("add"),
    )
    assert {state for state, _ in states(answer).values()} == {"open"}
    assert len(states(answer)) == 3


def test_a_confirm_that_couldnt_be_sent_never_puts_a_transaction_in_doubt_at_the_target():
    # The confirm reads what add may leave out; the transaction's own end would find that out, not the target.
    definitions = fixture()["definitions"]
    held = definition(definitions, "source-system.hold-quantity")["spec"]["outputSchema"]
    held["properties"]["ticket"] = {"type": "string"}
    step = hold()
    step["confirm"]["with"] = {"object": {"holdId": {"ref": "/steps/hold/output/ticket"}}}
    workflow = move(step, add(), submit())
    assert incidents(run_move(workflow, definitions=definitions))[0][0] == "WV-TXN-CONFIRM-STUCK"
    for name in ("add", "submit"):
        answer = run_move(workflow, definitions=definitions, target=to(name))
        assert (answer["status"], states(answer)["move"][0]) == ("completed", "open")


# --- codes, causes and stale keys -------------------------------------------------------------------------------


def coded(length: int) -> str:
    return "A" * length


@pytest.mark.parametrize("length", [100, 101, 128])
def test_every_code_a_simulation_accepts_is_answered_as_a_cause(length):
    refused = {"node:submit": {"code": coded(length), "effect": "none"}}
    answer = run_move(move(), errors=refused)
    assert answer["transactions"][0]["cause"]["code"] == coded(length)
    stopped = run_move(move(hold(), add(), {**STOP, "code": coded(length)}))
    assert stopped["transactions"][0]["cause"]["code"] == coded(length)


def test_a_code_too_long_for_the_answer_is_left_out_of_a_cause_alone():
    answer = run_move(move(hold(), add(), {**STOP, "code": coded(129)}))
    assert (answer["status"], answer["transactions"][0]["state"]) == ("failed", "compensated")
    assert answer["transactions"][0]["cause"] == {"instanceKey": "stop", "effect": "none"}
    assert failures(answer) == {"stop": None}


def test_a_session_shows_a_transaction_undone_for_any_code_a_simulation_accepts(tmp_path):
    # The debug view itself: a fail step's code has no limit, and a scripted failure's is 128.
    body = move_request(move(hold(), add(), {**STOP, "code": coded(200)}))
    document = json.loads(body.source)
    _, simulator, _ = simulator_for(body, None, document, whole=document, sliced=False)
    assert simulator is not None
    [record] = simulator.continue_until_breakpoint().transactions
    assert (record.state, record.cause.code) == ("compensated", coded(200))


@pytest.mark.parametrize("path", ["execute", "sessions"])
def test_an_answer_the_host_cant_build_says_so_without_any_value_of_the_request(tmp_path, monkeypatch, path):
    def refused(*_: Any, **__: Any) -> None:
        ScriptedFailure.model_validate({"code": "s3cr3t in lower case"})

    monkeypatch.setattr(simulation, "execute", refused)
    monkeypatch.setattr(debug_sessions, "simulator_for", refused)
    body = move_request(move()).model_dump(mode="json", by_alias=True, exclude_unset=True)
    if path == "sessions":
        del body["target"]
    answer = over_the_route(tmp_path, json.dumps(body), path)
    assert answer.status_code == 422
    assert answer.json() == {
        "status": 422,
        "code": "WV-STUDIO-SIMULATION",
        "message": "Studio couldn't answer about this simulated run",
    }


def test_a_failure_code_over_the_route_is_answered_as_a_cause(tmp_path):
    body = move_request(move(), errors={"node:submit": {"code": coded(101), "effect": "none"}})
    answer = over_the_route(tmp_path, body.model_dump_json(by_alias=True, exclude_unset=True))
    assert answer.status_code == 200
    assert answer.json()["transactions"][0]["cause"]["code"] == coded(101)


# --- stubs of confirms, releases and undos ----------------------------------------------------------------------


def without(*names: str) -> list[dict[str, Any]]:
    return [item for item in fixture()["definitions"] if item["metadata"]["name"] not in names]


@pytest.mark.parametrize(
    "names",
    [["target-system.remove-quantity"], ["source-system.confirm-hold", "source-system.release-hold"], None],
    ids=["an undo", "a confirm and a release", "every action"],
)
def test_confirms_releases_and_undos_nobody_has_run_on_stubs(names):
    definitions = without(*names) if names is not None else []
    workflow = move()
    undone = run_move(workflow, definitions=definitions, stubUnresolved=True, errors=REFUSED)
    assert undone["compile"]["ok"], undone["compile"]["diagnostics"]
    assert states(undone)["move"][0] == "compensated"
    assert keys(undone) == ["add#undo", "hold#cancel"]
    confirmed = run_move(workflow, definitions=definitions, stubUnresolved=True)
    assert (confirmed["status"], keys(confirmed)) == ("completed", ["hold#confirm"])
    expected = [f"{name}@1.0.0" for name in names] if names is not None else None
    assert expected is None or undone["compile"]["stubs"] == sorted(expected)


def test_a_called_undo_nobody_has_runs_on_a_stub():
    workflow = move(hold(), reverse_by_call(), submit())
    answer = run_move(workflow, definitions=without(UNDO), stubUnresolved=True, errors=REFUSED)
    assert answer["compile"]["ok"], answer["compile"]["diagnostics"]
    assert answer["compile"]["stubs"] == [f"{UNDO}@1.0.0"]
    assert (states(answer)["move"][0], keys(answer)) == ("compensated", ["add#undo", "hold#cancel"])


# --- a called undo that can't run, and misfits --------------------------------------------------------------------


def broken_undo(**workflow: Any) -> ExecuteRequest:
    """A move undone by a called workflow that the request brings and that doesn't compile."""
    body = undo_by_call_request(DONE).model_dump(mode="json", by_alias=True, exclude_unset=True)
    for item in body["definitions"]:
        if item["metadata"]["name"] == UNDO:
            item["spec"]["steps"] = [{**LOOKUP, "uses": "nobody.has@1.0.0"}]
    body["workflows"] = {f"{UNDO}@1.0.0": {"mocks": {}, **workflow}}
    return ExecuteRequest.model_validate_json(json.dumps(body))


@pytest.mark.parametrize(
    "workflow", [{}, {"source": "{", "format": "json"}], ids=["doesn't compile", "doesn't read as a workflow"]
)
def test_a_called_undo_in_the_request_that_doesnt_compile_never_counts_as_done(workflow):
    answer = answered(broken_undo(**workflow))
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "add",
        "instanceKey": "add#undo",
        "message": "The undo of add can't run, because reverse-target-addition doesn't compile.",
    }
    assert [(phase["instanceKey"], phase["status"]) for phase in answer["phases"]] == [("add#undo", "waiting")]
    assert states(answer)["move"] == ("compensating", {"hold": "done", "add": "undoing", "submit": "failed"})
    # A pin stands for it.
    body = broken_undo(**workflow)
    pinned = answered(body.model_copy(update={"mocks": {**body.mocks, "node:add#undo": {}}}))
    assert (keys(pinned), states(pinned)["move"][0]) == (["add#undo", "hold#cancel"], "compensated")


def test_a_called_undo_of_a_called_workflow_that_doesnt_compile_never_counts_as_done():
    undo, _ = callee(UNDO, [DONE])
    undo["spec"]["steps"] = [{**LOOKUP, "uses": "nobody.has@1.0.0"}]
    answer = inside_a_call(
        hold(),
        reverse_by_call(),
        STOP,
        definitions=[*without(UNDO), undo],
        workflows={f"{UNDO}@1.0.0": {"mocks": {}}},
    )
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "add",
        "instanceKey": "add#undo",
        "message": "The undo of add in inside can't run, because reverse-target-addition doesn't compile.",
        "frame": "call",
    }


def test_a_misfitting_pin_of_a_step_is_never_said_to_be_its_confirms():
    # hold takes its action's mock, as its own pin doesn't fit; nothing is pinned at its confirm.
    mocks = {"node:hold": {"holdId": 7}, "action:source-system.hold-quantity@1.0.0": {"holdId": "h-1"}}
    answer = run_move(move(), definitions=STRICT_CONFIRM, mocks=mocks)
    assert answer["blocked"]["instanceKey"] == "hold#confirm"
    assert answer["blocked"]["message"] == "The confirm of hold needs test data."


@pytest.mark.parametrize(
    "fields, path",
    [
        ({"mocks": {"node:add#undo": {}}}, "/mocks/node:add#undo"),
        ({"errors": {"node:add#undo": {"code": "DOWN"}}}, "/errors/node:add#undo"),
        ({"mocks": {"node:add#confirm": {}, "node:hold#confirm": {}}}, "/mocks/node:add#confirm"),
    ],
    ids=["a pin of an undo", "a failure of an undo", "a pin of a confirm"],
)
def test_test_data_of_a_phase_a_step_no_longer_has_is_refused_by_its_key(fields, path):
    step = {**add(), "irreversible": True}
    del step["compensate"]
    with pytest.raises(SimulationRefused) as refused:
        run_move(move(hold(), step), **fields)
    assert (refused.value.status, refused.value.code) == (422, "WV-DEBUG-MOCK-KEY")
    assert refused.value.paths == [path]
    named = "undo" if path.endswith("undo") else "confirm"
    assert refused.value.message == f"This workflow has no {named} of add; remove its test data"


@pytest.mark.parametrize(
    "workflow, key, named",
    [
        (looped(), "add[9]#undo", "add[9]"),
        (looped(), "add[9]#confirm", "add"),
        (move(), "add[0]#undo", "add[0]"),
    ],
    ids=["an item past the limit", "a phase the step doesn't have", "an item of a step outside of loops"],
)
def test_test_data_of_an_item_that_cant_be_names_the_item_when_its_step_has_the_phase(workflow, key, named):
    phase = key.partition("#")[2]
    with pytest.raises(SimulationRefused) as refused:
        run_move(workflow, input=THREE, mocks={f"node:{key}": {}})
    assert refused.value.paths == [f"/mocks/node:{key}"]
    assert refused.value.message == f"This workflow has no {phase} of {named}; remove its test data"


def test_test_data_of_a_phase_a_called_workflow_no_longer_has_is_refused_by_its_key():
    mocks = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:add#confirm": {}}
    with pytest.raises(SimulationRefused) as refused:
        inside_a_call(hold(), add(), definitions=fixture()["definitions"], workflows={"inside@1.0.0": {"mocks": mocks}})
    assert refused.value.paths == ["/workflows/inside@1.0.0/mocks/node:add#confirm"]
    assert refused.value.message == "inside@1.0.0 has no confirm of add; remove its test data"


def test_a_phase_the_host_cant_find_is_refused_by_pointer_never_passed_on():
    document = json.loads(move())
    _, simulator, _ = simulator_for(move_request(move()), None, document, whole=document, sliced=False)
    assert simulator is not None
    unknown = {"instanceKey": "nowhere#undo", "input": {"reference": "M-1"}, "output": {}}
    stale = {"instanceKey": "add#confirm", "output": {}}
    known = {"instanceKey": "add#undo", "input": {"reference": "M-1", "addOperation": "a-1"}}
    assert simulation.secret_phases(simulator.data.artifact, [unknown, known, stale]) == [
        "/phases/nowhere#undo/input",
        "/phases/nowhere#undo/output",
        "/phases/add#confirm/output",
    ]
    # Nothing it received or returned: nothing to pass on.
    assert simulation.secret_phases(simulator.data.artifact, [{"instanceKey": "nowhere#undo"}]) == []
