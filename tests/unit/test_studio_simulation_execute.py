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

"""Execute step, Execute previous steps and Execute workflow, simulated on the Studio host with no platform."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from firefly_weave.contracts.step_errors import step_error_message
from firefly_weave.studio.local_contracts import ExecuteRequest
from firefly_weave.studio.simulation import CatalogConflict, SimulationRefused, execute

ACTION = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))
CHECK = {
    "id": "check",
    "kind": "action",
    "uses": "onboarding.check-customer@1.0.0",
    "with": {"object": {"customerId": {"ref": "/input/id"}}},
}
SHAPE = {"id": "shape", "kind": "transform", "value": {"object": {"ok": {"ref": "/steps/check/output/eligible"}}}}
BROKEN = {"id": "broken", "kind": "transform", "value": {"ref": "/steps/nowhere/output"}}
PAID = {
    "id": "paid",
    "kind": "signal",
    "name": "order-paid",
    "timeoutSeconds": 600,
    "payloadSchema": {"type": "object"},
}
APPROVAL = {
    "id": "approval",
    "kind": "humanTask",
    "assignment": "reviewers",
    "title": {"literal": "Approve"},
    "context": {"literal": {}},
    "formSchema": {"type": "object"},
    "decisions": ["approve", "reject"],
}
PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 300}


def workflow(*steps, input_schema=None) -> str:
    return json.dumps(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "orders", "version": "1.0.0"},
            "spec": {
                "inputSchema": input_schema or {"type": "object", "properties": {"id": {"type": "string"}}},
                "outputSchema": {},
                "steps": list(steps),
                "output": {"literal": None},
            },
        }
    )


def request(source: str, **fields: Any) -> ExecuteRequest:
    body = {
        "source": source,
        "format": "json",
        "definitions": [ACTION],
        "catalog": "none",
        "input": {"id": "c-1"},
        "now": "2026-10-26T10:00:00Z",
        "target": {"mode": "workflow"},
        **fields,
    }
    return ExecuteRequest.model_validate_json(json.dumps(body))


def run(source: str, **fields: Any) -> dict[str, Any]:
    return execute(request(source, **fields), None).model_dump(mode="json", by_alias=True, exclude_unset=True)


def test_execute_workflow_runs_every_step_with_the_pins_as_results():
    answer = run(workflow(CHECK, SHAPE), mocks={"node:check": {"eligible": True}})
    assert answer["status"] == "completed"
    assert answer["compile"]["ok"] and not answer["compile"]["sliced"]
    assert [(e["nodeId"], e["status"], e.get("input"), e.get("output")) for e in answer["trace"]] == [
        ("check", "completed", {"customerId": "c-1"}, {"eligible": True}),
        ("shape", "completed", None, {"ok": True}),
    ]


def test_execute_step_runs_up_to_the_step_and_ignores_a_broken_later_step():
    answer = run(
        workflow(CHECK, SHAPE, BROKEN),
        mocks={"node:check": {"eligible": False}},
        target={"mode": "step", "stepId": "shape"},
    )
    assert answer["status"] == "completed" and answer["compile"]["sliced"]
    assert [e["nodeId"] for e in answer["trace"]] == ["check", "shape"]
    assert answer["trace"][-1]["output"] == {"ok": False}
    whole = run(workflow(CHECK, SHAPE, BROKEN), mocks={"node:check": {"eligible": False}})
    assert whole["status"] == "failed" and not whole["compile"]["ok"]


def test_execute_previous_steps_stops_before_the_step():
    answer = run(
        workflow(CHECK, SHAPE), mocks={"node:check": {"eligible": True}}, target={"mode": "before", "stepId": "shape"}
    )
    assert answer["status"] == "completed"
    assert [(e["nodeId"], e["status"]) for e in answer["trace"]] == [("check", "completed")]


def test_a_step_without_test_data_blocks_and_names_itself():
    answer = run(workflow(CHECK, SHAPE))
    assert answer["status"] == "blocked"
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "check",
        "instanceKey": "",
        "message": "Step check needs test data.",
    }


def test_a_pin_that_no_longer_fits_asks_to_be_edited_instead_of_refusing_the_run():
    answer = run(workflow(CHECK, SHAPE), mocks={"node:check": {"eligible": "yes"}})
    assert answer["status"] == "blocked"
    assert (
        answer["blocked"]["message"]
        == "The pinned output of check doesn't fit its output fields. Edit it, or unpin it."
    )


def test_signals_and_people_ask_until_scripted_and_waits_advance_by_themselves():
    source = workflow(PAUSE, PAID, APPROVAL)
    first = run(source)
    assert first["status"] == "blocked"
    assert first["blocked"]["reason"] == "signal" and first["blocked"]["message"] == "Send signal order-paid"
    assert [(e["nodeId"], e["status"]) for e in first["trace"]] == [("pause", "completed"), ("paid", "waiting")]
    second = run(source, scripts={"signals": {"paid": {"payload": {"paid": True}, "source": "manual"}}})
    assert second["blocked"]["reason"] == "human" and second["blocked"]["message"] == "Decide for approval"
    third = run(
        source,
        scripts={
            "signals": {"paid": {"payload": {"paid": True}, "source": "manual"}},
            "humans": {"approval": {"decision": "approve", "data": {}, "source": "manual"}},
        },
    )
    assert third["status"] == "completed"
    assert [(e["nodeId"], e["status"]) for e in third["trace"]] == [
        ("pause", "completed"),
        ("paid", "completed"),
        ("approval", "completed"),
    ]
    assert third["trace"][1]["output"] == {"paid": True}


def test_a_step_on_a_path_the_test_data_does_not_take_says_which_path_was_taken():
    route = {
        "id": "route",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/input/id"}, {"literal": "vip"}]}},
                "steps": [{"id": "big", "kind": "transform", "value": {"literal": 1}}],
                "output": {"literal": None},
            }
        ],
        "default": {"steps": [], "output": {"literal": None}},
    }
    answer = run(workflow(route), target={"mode": "step", "stepId": "big"})
    assert answer["status"] == "blocked"
    assert answer["blocked"]["reason"] == "path"
    assert answer["blocked"]["message"] == "With this test data, route takes Otherwise, so big isn't reached."


def test_secret_values_are_refused_with_pointers_only():
    schema = {"type": "object", "properties": {"id": {"type": "string"}, "pin": {"type": "string", "x-secret": True}}}
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(SHAPE.copy() | {"value": {"literal": 1}}, input_schema=schema), input={"id": "c", "pin": "1234"})
    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/input"]
    assert "1234" not in repr(refused.value.__dict__)


def test_unknown_actions_run_on_stubs_only_when_asked():
    lookup = CHECK | {"id": "lookup", "uses": "crm.lookup@1.0.0"}
    stubbed = run(workflow(lookup), stubUnresolved=True, mocks={"node:lookup": {"any": "thing"}})
    assert stubbed["status"] == "completed" and stubbed["compile"]["stubs"] == ["crm.lookup@1.0.0"]
    plain = run(workflow(lookup), mocks={"node:lookup": {"any": "thing"}})
    assert plain["status"] == "failed" and plain["compile"]["stubs"] == []


def test_a_local_definition_that_conflicts_with_one_shipped_with_weave_is_refused():
    from firefly_weave.contracts import agentic

    clash = agentic.action_definition()
    clash = clash | {"spec": clash["spec"] | {"timeoutSeconds": 7}}
    with pytest.raises(CatalogConflict):
        run(workflow(SHAPE.copy() | {"value": {"literal": 1}}), definitions=[clash])


def test_waits_that_are_not_advanced_are_for_sessions():
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(PAUSE), scripts={"autoAdvanceWaits": False})
    assert refused.value.code == "WV-STUDIO-REQUEST"


def test_a_test_input_that_does_not_fit_the_input_fields_is_refused_with_its_pointer():
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(SHAPE | {"value": {"literal": 1}}), input={"id": 5})
    assert refused.value.code == "WV-SCHEMA-INVALID_INSTANCE"
    assert refused.value.paths == ["/input"]


def test_a_step_the_runtime_refuses_fails_the_run_instead_of_the_host(monkeypatch):
    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.runtime.kernel import KernelError

    def refuse(self):
        raise KernelError("WV-RUNTIME-PREDICATE")

    monkeypatch.setattr(Simulator, "continue_until_breakpoint", refuse)
    answer = run(workflow(SHAPE | {"value": {"literal": 1}}))
    assert answer["status"] == "failed"
    assert [d["code"] for d in answer["diagnostics"]] == ["WV-RUNTIME-PREDICATE"]


SUMMARIZE = {
    "id": "summarize",
    "kind": "llm",
    "uses": "weave-agentic-generate@1.0.0",
    "profile": "summarize",
    "prompt": {"literal": "Summarize the ticket."},
    "context": {"literal": {}},
    "connection": "ai",
}


def ai_workflow() -> str:
    document = json.loads(workflow(SUMMARIZE))
    document["spec"]["connections"] = {"ai": {"connector": "weave-agentic-provider@1.0.0"}}
    document["spec"]["llmProfiles"] = {
        "summarize": {
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "options": {"max_tokens": 1024},
            "outputSchema": {"type": "string", "maxLength": 20000},
        }
    }
    return json.dumps(document)


def test_an_ai_task_without_saved_turns_asks_for_test_data_and_calls_no_model():
    answer = run(ai_workflow(), definitions=[])
    assert answer["status"] == "blocked"
    assert answer["blocked"]["message"] == "Step summarize needs test data."


def test_an_ai_task_replays_its_saved_turn_with_no_usage():
    turns = {"summarize": {"turns": [{"final": "Two sentences."}], "source": "manual"}}
    answer = run(ai_workflow(), definitions=[], scripts={"aiTurns": turns})
    assert answer["status"] == "completed"
    assert answer["trace"][0]["output"] == {
        "result": "Two sentences.",
        "usage": {"requests": 0, "inputTokens": 0, "outputTokens": 0},
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }


def test_a_secret_in_a_saved_ai_turn_is_refused_with_its_pointer():
    document = json.loads(ai_workflow())
    document["spec"]["llmProfiles"]["summarize"]["outputSchema"] = {"type": "string", "x-secret": True}
    turns = {"summarize": {"turns": [{"final": "s3cr3t"}], "source": "manual"}}
    with pytest.raises(SimulationRefused) as refused:
        run(json.dumps(document), definitions=[], scripts={"aiTurns": turns})
    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/scripts/aiTurns/summarize/turns/0/final"]
    assert "s3cr3t" not in repr(refused.value.__dict__)


def test_a_secret_in_a_step_mock_is_found_by_that_step_s_output_fields_with_its_pointer():
    secret = json.loads(json.dumps(ACTION))
    secret["metadata"]["name"] = "vault.read"
    secret["spec"]["implementation"]["taskType"] = "vault.read"
    secret["spec"]["outputSchema"] = {
        "type": "object",
        "properties": {"value": {"type": "string", "x-secret": True}},
    }
    read = CHECK | {"id": "read", "uses": "vault.read@1.0.0"}
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(read), definitions=[ACTION, secret], mocks={"node:read": {"value": "s3cr3t"}})
    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/mocks/node:read"]
    assert "s3cr3t" not in repr(refused.value.__dict__)
    # The same value in another step's mock, whose fields mark nothing secret, is never refused for it.
    plain = run(workflow(CHECK), mocks={"node:check": {"eligible": True, "value": "s3cr3t"}})
    assert plain["status"] == "blocked" and plain["blocked"]["reason"] == "missing_mock"


LOOP_APPROVAL = {
    "id": "each",
    "kind": "forEach",
    "items": {"literal": [1, 2]},
    "body": {"steps": [APPROVAL], "output": {"literal": None}},
}


def test_a_script_for_one_iteration_wins_over_the_step_s():
    answer = run(
        workflow(LOOP_APPROVAL),
        scripts={
            "humans": {
                "approval": {"decision": "approve", "data": {}, "source": "manual"},
                "approval[1]": {"decision": "reject", "data": {}, "source": "manual"},
            }
        },
    )
    assert answer["status"] == "completed"
    outputs = {e["instanceKey"]: e["output"]["decision"] for e in answer["trace"] if e["nodeId"] == "approval"}
    assert outputs == {"approval[0]": "approve", "approval[1]": "reject"}


def test_a_signal_script_that_arrives_later_advances_time_first():
    on_time = run(workflow(PAID), scripts={"signals": {"paid": {"payload": {"a": 1}, "source": "manual"}}})
    assert on_time["trace"] == [
        {"index": 0, "nodeId": "paid", "instanceKey": "", "kind": "signal", "status": "completed", "output": {"a": 1}}
    ]
    # Ten minutes is the signal's deadline: arriving after it, the payload never lands.
    late = run(
        workflow(PAID), scripts={"signals": {"paid": {"payload": {"a": 1}, "afterSeconds": 700, "source": "manual"}}}
    )
    assert "output" not in late["trace"][0]


# --- fixes from review ------------------------------------------------------------------------------------------

LATER = CHECK | {"id": "later"}


def test_a_later_step_s_mock_never_blocks_executing_an_earlier_one():
    answer = run(
        workflow(CHECK, SHAPE, LATER),
        mocks={"node:check": {"eligible": True}, "node:later": {"eligible": False}},
        target={"mode": "step", "stepId": "shape"},
    )
    assert answer["status"] == "completed"
    assert answer["trace"][-1]["output"] == {"ok": True}


def test_a_mock_for_no_step_at_all_is_still_refused():
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(CHECK, SHAPE), mocks={"node:check": {"eligible": True}, "node:nowhere": {}})
    assert refused.value.code == "WV-DEBUG-MOCK-KEY"


AFTER = {"id": "after", "kind": "transform", "value": {"literal": 1}}


@pytest.mark.parametrize(
    "script",
    [
        {"payload": {}, "afterSeconds": 900, "source": "manual"},
        {"outcome": "timeout"},
    ],
)
def test_a_signal_whose_deadline_passes_fails_the_step_and_the_run(script):
    answer = run(workflow(PAID, AFTER), scripts={"signals": {"paid": script}})
    assert answer["status"] == "failed"
    assert [(e["nodeId"], e["status"]) for e in answer["trace"]] == [("paid", "failed")]
    assert [d["message"] for d in answer["diagnostics"]] == [
        "The deadline of order-paid passed before the signal arrived."
    ]


def test_a_person_s_task_that_expires_fails_the_step_and_the_run():
    expiring = APPROVAL | {"expirySeconds": 3600}
    answer = run(workflow(expiring, AFTER), scripts={"humans": {"approval": {"outcome": "expire"}}})
    assert answer["status"] == "failed"
    assert [(e["nodeId"], e["status"]) for e in answer["trace"]] == [("approval", "failed")]
    assert [d["message"] for d in answer["diagnostics"]] == ["The task approval expired before anyone decided."]


CALL = {"id": "enrich", "kind": "callWorkflow", "uses": "enrich@1.0.0", "mode": "wait", "with": {"literal": {}}}


def test_an_unpinned_call_that_waits_needs_test_data():
    answer = run(workflow(CALL), stubUnresolved=True)
    assert answer["status"] == "blocked"
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "enrich",
        "instanceKey": "",
        "message": "Step enrich needs test data.",
    }
    pinned = run(workflow(CALL), stubUnresolved=True, mocks={"node:enrich": {"ok": True}})
    assert pinned["status"] == "completed"


def callee_definition() -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "enrich", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "callable": {},
            "steps": [CHECK | {"with": {"object": {"customerId": {"literal": "c-9"}}}}],
            "output": {"ref": "/steps/check/output"},
        },
    }


def test_mocks_for_steps_inside_a_called_workflow_are_used():
    answer = run(
        workflow(CALL),
        definitions=[ACTION, callee_definition()],
        workflows={"enrich@1.0.0": {"mocks": {"node:check": {"eligible": True}}}},
    )
    assert answer["status"] == "completed", answer
    assert answer["trace"][0]["output"] == {"eligible": True}


def test_a_called_workflow_that_returns_null_gives_null_to_the_call():
    callee = callee_definition()
    callee["spec"]["output"] = {"literal": None}
    answer = run(
        workflow(CALL),
        definitions=[ACTION, callee],
        workflows={"enrich@1.0.0": {"mocks": {"node:check": {"eligible": True}}}},
    )
    assert answer["status"] == "completed", answer
    # The call gets null; the called workflow's own step shows in its frame.
    assert [(e["nodeId"], e["status"], e.get("output"), e.get("frame")) for e in answer["trace"]] == [
        ("enrich", "completed", None, None),
        ("check", "completed", {"eligible": True}, "enrich"),
    ]


def test_after_a_signal_a_step_the_simulator_cant_carry_on_with_is_named_and_what_ran_is_kept(caplog, monkeypatch):
    from firefly_weave.runtime import kernel

    def defect(*_: object) -> None:
        raise TypeError("a defect in the runtime")

    # No valid workflow trips the simulator on purpose: a defect stands in where the called run's result arrives.
    monkeypatch.setattr(kernel, "call_result", defect)
    with caplog.at_level("DEBUG", logger="firefly_weave.studio.simulation"):
        answer = run(
            workflow(PAID, CALL),
            definitions=[ACTION, callee_definition()],
            workflows={"enrich@1.0.0": {"mocks": {"node:check": {"eligible": True}}}},
            scripts={"signals": {"paid": {"payload": {"secret-ish": "v-123"}, "source": "manual"}}},
        )
    assert answer["status"] == "failed", answer
    assert [(d["code"], d["message"]) for d in answer["diagnostics"]] == [
        ("WV-STUDIO-SIMULATION", "Studio couldn't simulate step enrich, so the simulated run stopped there.")
    ]
    assert [(e["nodeId"], e["status"]) for e in answer["trace"]][0] == ("paid", "completed")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "TypeError" in logged and "enrich" in logged
    assert "v-123" not in logged and "a defect in the runtime" not in logged


def loop_over(items: list[int]) -> dict[str, Any]:
    """A loop of up to two items over ``items``."""
    return {
        "id": "orders",
        "kind": "forEach",
        "items": {"literal": items},
        "maxItems": 2,
        "body": {
            "steps": [{"id": "each", "kind": "transform", "value": {"ref": "/item"}}],
            "output": {"literal": None},
        },
    }


# A run timeout gives the simulation a deadline to advance to; the run still stops at the loop.
@pytest.mark.parametrize("timeout", [None, 3600], ids=["no run timeout", "a run timeout"])
def test_a_loop_with_more_items_than_its_limit_fails_the_run_at_the_loop(timeout):
    document = json.loads(workflow(loop_over([1, 2, 3]), AFTER))
    if timeout is not None:
        document["spec"]["timeoutSeconds"] = timeout
    answer = run(json.dumps(document))
    assert answer["status"] == "failed"
    assert [(e["nodeId"], e["status"]) for e in answer["trace"]] == [("orders", "failed")]
    assert [(d["code"], d["message"]) for d in answer["diagnostics"]] == [
        ("WV-RUNTIME-LOOP-LIMIT", "A loop's list was longer than its item limit.")
    ]


def test_a_loop_over_its_limit_in_a_called_workflow_fails_the_call_and_the_run():
    callee = callee_definition()
    callee["spec"]["steps"] = [loop_over([1, 2, 3])]
    callee["spec"]["output"] = {"literal": None}
    answer = run(workflow(CALL, AFTER), definitions=[ACTION, callee], workflows={"enrich@1.0.0": {"mocks": {}}})
    assert answer["status"] == "failed"
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]] == [
        ("enrich", "failed", None),
        ("orders", "failed", "enrich"),
    ]
    assert [(f["key"], f["status"]) for f in answer["frames"]] == [("enrich", "failed")]
    assert [d["code"] for d in answer["diagnostics"]] == ["WV-RUNTIME-LOOP-LIMIT"]


def test_a_secret_in_a_called_workflow_s_mock_is_refused_with_its_pointer():
    secret = json.loads(json.dumps(ACTION))
    secret["spec"]["outputSchema"]["properties"]["eligible"]["x-secret"] = True
    with pytest.raises(SimulationRefused) as refused:
        run(
            workflow(CALL),
            definitions=[secret, callee_definition()],
            workflows={"enrich@1.0.0": {"mocks": {"node:check": {"eligible": True}}}},
        )
    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/workflows/enrich@1.0.0/mocks/node:check"]


def test_a_secret_in_an_action_mock_is_refused_with_its_pointer():
    secret = json.loads(json.dumps(ACTION))
    secret["spec"]["outputSchema"]["properties"]["eligible"]["x-secret"] = True
    with pytest.raises(SimulationRefused) as refused:
        run(
            workflow(CHECK),
            definitions=[secret],
            mocks={"action:onboarding.check-customer@1.0.0": {"eligible": True}},
        )
    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/mocks/action:onboarding.check-customer@1.0.0"]


def test_test_data_too_large_to_check_is_refused_as_too_large():
    with pytest.raises(SimulationRefused) as refused:
        run(workflow(SHAPE | {"value": {"literal": 1}}), input={"id": "x" * (1536 * 1024)})
    assert refused.value.code != "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/input"]
    assert "secret" not in refused.value.message


def test_the_answer_keeps_only_the_paths_decisions_took():
    answer = run(workflow(CHECK, SHAPE), mocks={"node:check": {"eligible": True}})
    assert set(answer.get("variables", {})) <= {"branches"}


# --- simulated step failures ------------------------------------------------------------------------------------

REFUSED = {"code": "REQUEST_REFUSED"}
NOTE = {"id": "note", "kind": "transform", "value": {"literal": "noted"}}
NOTIFY_TEAM = {"id": "notify-team", "kind": "transform", "value": {"ref": "/steps/check/error/code"}}
SUBMIT_ACTION = {
    **ACTION,
    "metadata": {"name": "orders.submit", "version": "1.0.0"},
    "spec": {
        **{name: value for name, value in ACTION["spec"].items() if name != "retry"},
        "implementation": {"kind": "worker", "taskType": "orders.submit", "taskVersion": "1.0.0"},
        "sideEffect": "non_idempotent",
    },
}
SUBMIT = CHECK | {"id": "submit", "uses": "orders.submit@1.0.0"}


def refused(key: str, **fields: Any) -> dict[str, Any]:
    """The error object later steps read when the step instance ``key`` fails with REQUEST_REFUSED."""
    return {
        "code": "REQUEST_REFUSED",
        "message": step_error_message("REQUEST_REFUSED"),
        "outcome": "failed",
        "attempts": 1,
        "instanceKey": key,
        **fields,
    }


def entries(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["instanceKey"] or entry["nodeId"]: entry for entry in answer["trace"] if "frame" not in entry}


def test_a_failure_a_step_continues_after_is_reported_as_handled_and_the_run_goes_on():
    answer = run(workflow(CHECK | {"onError": "continue"}, NOTE), errors={"node:check": REFUSED})
    assert answer["status"] == "completed"
    check = entries(answer)["check"]
    assert (check["status"], check["handled"], check["error"]) == ("failed", "continue", refused("check"))
    assert entries(answer)["note"]["status"] == "completed"


def test_a_failure_an_error_path_handles_runs_the_path_and_gives_the_step_the_path_output():
    path = {"steps": [NOTIFY_TEAM], "output": {"ref": "/steps/notify-team/output"}}
    answer = run(workflow(CHECK | {"onError": "errorOutput", "errorPath": path}), errors={"node:check": REFUSED})
    assert answer["status"] == "completed"
    check, notified = entries(answer)["check"], entries(answer)["notify-team"]
    assert (check["status"], check["handled"], check["output"]) == ("failed", "errorOutput", "REQUEST_REFUSED")
    assert check["error"] == refused("check")
    assert (notified["status"], notified["output"]) == ("completed", "REQUEST_REFUSED")


def test_a_failure_that_stops_the_run_carries_its_error_and_is_not_handled():
    answer = run(workflow(CHECK, NOTE), errors={"node:check": REFUSED})
    assert answer["status"] == "failed"
    check = entries(answer)["check"]
    assert (check["status"], check["error"]) == ("failed", refused("check"))
    assert "handled" not in check and "note" not in entries(answer)


def test_an_unknown_outcome_on_a_step_that_changes_data_still_stops_the_run_whatever_its_setting():
    answer = run(
        workflow(SUBMIT | {"onError": "continue"}, NOTE),
        definitions=[ACTION, SUBMIT_ACTION],
        errors={"node:submit": REFUSED | {"outcome": "unknown"}},
    )
    assert answer["status"] == "failed"
    submit = entries(answer)["submit"]
    assert submit["status"] == "failed" and "handled" not in submit
    assert "WV-TASK-AMBIGUOUS" in [d["code"] for d in answer["diagnostics"]]
    assert "note" not in entries(answer)


def test_one_iteration_fails_and_is_handled_while_the_others_go_on():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/ids"},
        "maxItems": 10,
        "body": {
            "steps": [CHECK | {"with": {"object": {"customerId": {"ref": "/item"}}}, "onError": "continue"}],
            "output": {"ref": "/steps/check/output"},
        },
    }
    ids = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}}
    answer = run(
        workflow(each, input_schema=ids),
        input={"ids": ["c-1", "c-2", "c-3"]},
        mocks={"node:check": {"eligible": True}},
        errors={"node:check[1]": REFUSED},
    )
    assert answer["status"] == "completed"
    found = entries(answer)
    statuses = [found[key]["status"] for key in ("check[0]", "check[1]", "check[2]")]
    assert statuses == ["completed", "failed", "completed"]
    assert (found["check[1]"]["handled"], found["check[1]"]["error"]) == ("continue", refused("check[1]"))
    assert "handled" not in found["check[0]"] and "error" not in found["check[2]"]
    assert [loop["status"] for loop in answer["loops"]] == ["completed"]


def test_a_call_that_waits_handles_its_called_run_failing_with_its_error_path():
    failing = callee_definition()
    failing["spec"]["steps"] = [{"id": "stop", "kind": "fail", "code": "REQUEST_REFUSED", "message": "No."}]
    failing["spec"]["output"] = {"literal": None}
    path = {"steps": [], "output": {"literal": "recovered"}}
    answer = run(
        workflow(CALL | {"onError": "errorOutput", "errorPath": path}),
        definitions=[ACTION, failing],
        workflows={"enrich@1.0.0": {"mocks": {}}},
    )
    assert answer["status"] == "completed", answer
    enrich = entries(answer)["enrich"]
    assert (enrich["status"], enrich["handled"], enrich["output"]) == ("failed", "errorOutput", "recovered")
    assert (enrich["error"]["code"], enrich["error"]["message"]) == ("REQUEST_REFUSED", "No.")
    assert "childRunId" in enrich["error"]


def test_a_failure_scripted_on_a_call_that_waits_is_handled_by_its_setting():
    answer = run(
        workflow(CALL | {"onError": "continue"}, NOTE),
        definitions=[ACTION, callee_definition()],
        workflows={"enrich@1.0.0": {"mocks": {"node:check": {"eligible": True}}}},
        errors={"node:enrich": REFUSED},
    )
    assert answer["status"] == "completed"
    enrich = entries(answer)["enrich"]
    assert (enrich["status"], enrich["handled"], enrich["error"]) == ("failed", "continue", refused("enrich"))


def test_an_ai_task_takes_a_scripted_failure():
    document = json.loads(ai_workflow())
    document["spec"]["steps"][0]["onError"] = "continue"
    answer = run(json.dumps(document), definitions=[], errors={"node:summarize": REFUSED})
    assert answer["status"] == "completed"
    summarize = entries(answer)["summarize"]
    assert (summarize["handled"], summarize["error"]) == ("continue", refused("summarize"))


@pytest.mark.parametrize(
    ("steps", "fields", "code"),
    [
        ([CHECK | {"onError": "continue"}], {"mocks": {"node:check": {"eligible": True}}}, "WV-DEBUG-MOCK-CONFLICT"),
        ([SHAPE | {"value": {"literal": 1}}], {}, "WV-DEBUG-MOCK-KEY"),
    ],
    ids=["a pin and a failure on the same key", "a step that can't fail this way"],
)
def test_a_failure_the_simulation_cannot_apply_is_refused_with_its_code(steps, fields, code):
    key = "node:" + steps[0]["id"]
    with pytest.raises(SimulationRefused) as refusal:
        run(workflow(*steps), errors={key: REFUSED}, **fields)
    assert (refusal.value.status, refusal.value.code) == (422, code)


@pytest.mark.parametrize(
    "failure",
    [{"code": "request-refused"}, {"code": "E" * 129}, REFUSED | {"attempts": 1001}, REFUSED | {"outcome": "maybe"}],
    ids=["a lowercase code", "a code longer than 128 characters", "more than 1000 attempts", "an unknown outcome"],
)
def test_a_failure_outside_the_limits_is_refused_before_the_simulation(failure):
    at_the_limit = request(workflow(NOTE), errors={"node:note": REFUSED | {"attempts": 1000}})
    assert at_the_limit.errors["node:note"].attempts == 1000
    with pytest.raises(ValidationError):
        request(workflow(NOTE), errors={"node:note": failure})


def test_simulated_failures_never_reach_a_real_run():
    from firefly_weave.contracts.step_tests import DraftTestActivationRequest, DraftTestRunRequest, StepTestRequest

    errors = {"node:check": REFUSED}
    assert request(workflow(CHECK), errors=errors).errors["node:check"].code == "REQUEST_REFUSED"
    draft = {"draft_id": "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b", "draft_revision": 1}
    real_runs = [
        (StepTestRequest, {**draft, "kind": "action", "step_id": "check", "input": {}, "timeout_seconds": 30}),
        (DraftTestActivationRequest, draft),
        (DraftTestRunRequest, {"input": {}}),
    ]
    for model, body in real_runs:
        assert model.model_validate_json(json.dumps(body))
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps({**body, "errors": errors}))


def test_executing_a_step_in_an_error_path_its_step_did_not_take_says_why():
    path = {"steps": [NOTIFY_TEAM], "output": {"literal": None}}
    answer = run(
        workflow(CHECK | {"onError": "errorOutput", "errorPath": path}),
        mocks={"node:check": {"eligible": True}},
        target={"mode": "step", "stepId": "notify-team"},
    )
    assert answer["status"] == "blocked"
    assert answer["blocked"] == {
        "reason": "path",
        "nodeId": "notify-team",
        "instanceKey": "",
        "message": "With this test data, check succeeds, so notify-team isn't reached.",
    }
