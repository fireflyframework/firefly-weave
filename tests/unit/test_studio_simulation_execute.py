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
