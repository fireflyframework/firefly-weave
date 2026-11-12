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

"""Debug sessions make steps fail on purpose, so error paths, loops and waits are tried without a platform."""

import json
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from firefly_weave.cli.debug import simulate
from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.step_errors import STEP_ERROR_MESSAGES
from firefly_weave.operations.debug.models import DebugError, ScriptedError
from firefly_weave.operations.debug.simulator import Simulator

NOW = datetime(2026, 10, 26, tzinfo=UTC)
STATUS = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "required": ["status"],
    "additionalProperties": False,
}
# name: side effect
ACTIONS = {"look": "idempotent", "submit": "non_idempotent"}
OK = {"status": "ok"}


def catalog() -> CatalogSnapshot:
    contracts, definitions = [], []
    for name, effect in ACTIONS.items():
        contract = {"inputSchema": {}, "outputSchema": STATUS, "sideEffect": effect, "timeoutSeconds": 30}
        contracts.append({"taskType": name, "taskVersion": "1.0.0", **contract})
        definitions.append(
            load_definition(
                {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": name, "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                        **contract,
                    },
                }
            )
        )
    return CatalogSnapshot.from_definitions(definitions, tasks=contracts)


def compiled(*steps, output=None) -> CompiledArtifact:
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "scripted-failures", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": output or {"literal": None}},
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog())
    assert result.ok, result.to_bytes()
    return result.artifact


def action(identifier, uses="look", value=None, **settings) -> dict:
    return {"id": identifier, "kind": "action", "uses": f"{uses}@1.0.0", "with": value or {"literal": {}}, **settings}


def recover(steps=(), output=None) -> dict:
    return {"steps": list(steps), "output": output if output is not None else {"literal": "recovered"}}


def session(artifact, *, mocks=None, errors=None, value=None) -> Simulator:
    scripted = {key: ScriptedError.model_validate(error) for key, error in (errors or {}).items()}
    return Simulator(artifact, mocks=mocks or {}, errors=scripted, input=value, now=NOW)


def test_a_scripted_failure_on_a_step_that_continues_lets_the_session_go_on():
    artifact = compiled(action("lookup", onError="continue"), action("notify-team"))
    view = session(
        artifact, mocks={"node:notify-team": OK}, errors={"node:lookup": {"code": "RATE_LIMITED"}}
    ).continue_until_breakpoint()
    assert view.status == "succeeded"
    assert view.variables["steps"]["lookup"] == {
        "output": None,
        "error": {
            "code": "RATE_LIMITED",
            "message": STEP_ERROR_MESSAGES["RATE_LIMITED"],
            "outcome": "failed",
            "attempts": 1,
            "instanceKey": "lookup",
        },
    }
    failed = next(event for event in view.events if event.type == "step_failed")
    assert (failed.data["node_id"], failed.data["code"], failed.data["attempts"]) == ("lookup", "RATE_LIMITED", 1)


def test_a_scripted_failure_on_one_iteration_fails_only_that_iteration():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 10,
        "body": {"steps": [action("send", onError="continue")], "output": {"ref": "/steps/send/output"}},
    }
    artifact = compiled(each, output={"ref": "/steps/each/output"})
    view = session(
        artifact,
        mocks={"node:send": OK},
        errors={"node:send[2]": {"code": "RATE_LIMITED"}},
        value=[1, 2, 3, 4, 5],
    ).continue_until_breakpoint()
    assert view.status == "succeeded"
    assert view.variables["output"] == [OK, OK, None, OK, OK]


def test_a_mock_and_an_error_under_the_same_key_conflict():
    with pytest.raises(DebugError) as raised:
        session(
            compiled(action("lookup", onError="continue")),
            mocks={"node:lookup": OK},
            errors={"node:lookup": {"code": "TIMEOUT"}},
        )
    assert raised.value.code == "WV-DEBUG-MOCK-CONFLICT"


def test_a_scripted_failure_names_a_step_that_can_fail_this_way():
    artifact = compiled({"id": "shape", "kind": "transform", "value": {"literal": 1}})
    with pytest.raises(DebugError) as raised:
        session(artifact, errors={"node:shape": {"code": "TIMEOUT"}})
    assert raised.value.code == "WV-DEBUG-MOCK-KEY"


def test_precedence_is_instance_key_then_step_then_action():
    artifact = compiled(action("lookup", onError="continue"))
    failed = session(
        artifact, mocks={"action:look@1.0.0": OK}, errors={"node:lookup": {"code": "TIMEOUT"}}
    ).continue_until_breakpoint()
    assert failed.variables["steps"]["lookup"]["error"]["code"] == "TIMEOUT"
    mocked = session(
        artifact, mocks={"node:lookup": OK}, errors={"action:look@1.0.0": {"code": "TIMEOUT"}}
    ).continue_until_breakpoint()
    assert mocked.variables["steps"]["lookup"] == {"output": OK}


def test_a_scripted_failure_on_a_step_that_stops_shows_the_incident():
    safe = session(compiled(action("lookup")), errors={"node:lookup": {"code": "TIMEOUT"}}).continue_until_breakpoint()
    assert (safe.status, safe.variables["incident"]) == ("suspended", "WV-TASK-RETRIES-EXHAUSTED")
    unsafe = session(
        compiled(action("submit", uses="submit")), errors={"node:submit": {"code": "TIMEOUT"}}
    ).continue_until_breakpoint()
    assert (unsafe.status, unsafe.variables["incident"]) == ("suspended", "WV-TASK-AMBIGUOUS")


def test_an_unknown_outcome_on_a_step_that_changes_data_shows_the_barrier():
    artifact = compiled(action("submit", uses="submit", onError="errorOutput", errorPath=recover()))
    view = session(
        artifact, errors={"node:submit": {"code": "TIMEOUT", "outcome": "unknown"}}
    ).continue_until_breakpoint()
    assert (view.status, view.variables["incident"]) == ("suspended", "WV-TASK-AMBIGUOUS")
    assert not any(event.type == "step_failed" for event in view.events)


def test_a_scripted_failure_on_a_pure_step_fails_it_without_evaluating_it():
    shape = {
        "id": "shape",
        "kind": "transform",
        "value": {"ref": "/input/missing"},
        "onError": "errorOutput",
        "errorPath": recover(),
    }
    view = session(
        compiled(shape, output={"ref": "/steps/shape/output"}), errors={"node:shape": {"code": "TIMEOUT"}}, value={}
    ).continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", "recovered")
    assert view.variables["steps"]["shape"]["error"]["code"] == "TIMEOUT"


def test_advance_time_past_a_handled_wait_runs_its_error_path():
    reply = {
        "id": "reply",
        "kind": "signal",
        "name": "reply",
        "timeoutSeconds": 60,
        "payloadSchema": {},
        "onError": "errorOutput",
        "errorPath": recover(output={"literal": "no reply"}),
    }
    simulator = session(compiled(reply, output={"ref": "/steps/reply/output"}))
    assert simulator.continue_until_breakpoint().status == "waiting"
    simulator.advance_time(61)
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", "no reply")
    failed = next(event for event in view.events if event.type == "step_failed")
    assert failed.data["code"] == "WV-SIGNAL-TIMEOUT" and "deadline" in failed.data


def test_breakpoints_stop_inside_error_paths():
    note = {"id": "note", "kind": "transform", "value": {"literal": "x"}}
    artifact = compiled(action("lookup", onError="errorOutput", errorPath=recover([note])))
    simulator = session(artifact, errors={"node:lookup": {"code": "TIMEOUT"}})
    simulator.set_breakpoints({"note"})
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.selected_node) == ("paused", "note")
    assert simulator.continue_until_breakpoint().status == "succeeded"


def test_a_session_with_scripted_failures_restores_and_one_without_keeps_its_bytes():
    artifact = compiled(action("lookup", onError="continue"))
    scripted = session(artifact, errors={"node:lookup": {"code": "TIMEOUT"}})
    assert '"errors"' in scripted.serialize()
    assert Simulator.restore(scripted.serialize()).continue_until_breakpoint().status == "succeeded"
    assert '"errors"' not in session(artifact, mocks={"node:lookup": OK}).serialize()


def test_the_local_simulation_accepts_error_options(tmp_path):
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 10,
        "body": {"steps": [action("send", onError="continue")], "output": {"ref": "/steps/send/output"}},
    }
    request = {
        "artifact": json.loads(compiled(each, output={"ref": "/steps/each/output"}).to_bytes()),
        "mocks": {"node:send": OK},
        "input": [1, 2, 3],
        "now": NOW.isoformat(),
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    result = CliRunner().invoke(simulate, [str(path), "--error", "node:send[2]=RATE_LIMITED", "--output", "json"])
    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert view["variables"]["output"] == [OK, OK, None]
    refused = CliRunner().invoke(simulate, [str(path), "--error", "node:send[2]"])
    assert refused.exit_code != 0 and "KEY=CODE" in refused.output
