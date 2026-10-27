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

"""A step that handles its failure turns it into data: the run continues, or runs the step's error path first."""

from step_error_support import (
    CHILD,
    Run,
    action,
    changes,
    compiled,
    controls,
    loop,
    path,
    tasks,
    transform,
)

MISSING = {"ref": "/input/missing"}


def test_a_pure_step_that_continues_records_its_error_and_the_run_goes_on():
    flow = compiled(
        transform("pick", MISSING, onError="continue"),
        transform("code", {"ref": "/steps/pick/error/code"}),
        output={"object": {"pick": {"ref": "/steps/pick/output"}, "code": {"ref": "/steps/code/output"}}},
    )
    run = Run(flow, {})
    result = run.start()
    assert run.state.status == "succeeded"
    assert run.state.output == {"pick": None, "code": "WV-EXPR-MISSING"}
    assert run.state.steps["pick"] == {
        "output": None,
        "error": {
            "code": "WV-EXPR-MISSING",
            "message": "A value the step reads is missing.",
            "outcome": "not_started",
            "attempts": 1,
            "instanceKey": "pick",
        },
    }
    assert changes(result) == [("pick", "failed"), ("code", "completed")]
    assert result.steps[0].model_dump(mode="json")["error"]["code"] == "WV-EXPR-MISSING"
    assert run.state.incidents == {} and run.state.incident is None


def test_an_error_path_runs_after_a_failure_and_its_output_becomes_the_steps():
    flow = compiled(
        transform(
            "pick",
            MISSING,
            onError="errorOutput",
            errorPath=path(transform("why", {"ref": "/steps/pick/error/code"}), output={"ref": "/steps/why/output"}),
        ),
        output={"ref": "/steps/pick/output"},
    )
    run = Run(flow, {})
    result = run.start()
    assert (run.state.status, run.state.output) == ("succeeded", "WV-EXPR-MISSING")
    assert changes(result) == [
        ("pick", "failed"),
        ("why", "completed"),
        ("@branch:pick:0", "completed"),
        ("pick", "failed"),
        ("@join:pick", "completed"),
    ]
    assert result.steps[3].output == "WV-EXPR-MISSING" and result.steps[3].error["instanceKey"] == "pick"
    assert controls(result) == [
        ("spawn_branch", "pick", "errorPath"),
        ("accept_output", "@branch:pick:0", "errorPath"),
        ("join", "@join:pick", None),
    ]
    # The path's frame and join exist only while it runs.
    assert run.state.branches == {} and run.state.joins == {}
    assert run.state.steps["pick"]["output"] == "WV-EXPR-MISSING"


def test_a_step_that_succeeds_never_runs_its_error_path_and_its_join_changes_nothing():
    flow = compiled(
        transform("pick", {"literal": 1}, onError="errorOutput", errorPath=path(output={"literal": 0})),
        output={"ref": "/steps/pick/output"},
    )
    run = Run(flow, {})
    result = run.start()
    assert (run.state.status, run.state.output) == ("succeeded", 1)
    assert changes(result) == [("pick", "completed")]
    assert controls(result) == [] and run.state.steps == {"pick": {"output": 1}}


def test_an_action_whose_input_is_invalid_fails_before_any_task_with_no_attempt():
    flow = compiled(action("lookup", value={"object": {"id": {"ref": "/input/id"}}}, onError="continue"))
    run = Run(flow, {"id": 7})
    result = run.start()
    assert tasks(result) == [] and run.state.active == []
    assert run.state.steps["lookup"]["error"]["code"] == "WV-SCHEMA-INVALID_INSTANCE"
    assert (run.state.steps["lookup"]["error"]["attempts"], run.state.status) == (0, "succeeded")


def test_a_step_that_stops_the_run_keeps_todays_incident():
    run = Run(compiled(transform("pick", MISSING)), {})
    result = run.start()
    assert (run.state.status, run.state.incident) == ("suspended", "WV-EXPR-MISSING")
    assert result.steps == []


def test_a_final_failure_reported_after_retries_closes_the_interim_incident_and_continues():
    flow = compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/error"})
    run = Run(flow, {})
    run.start()
    run.send("task_failed", node_id="lookup", generation=2, output={"code": "RATE_LIMITED", "outcome": "failed"})
    assert (run.state.status, run.state.incident) == ("suspended", "WV-TASK-FAILED")
    result = run.fail("lookup", "RATE_LIMITED", attempts=3, generation=2)
    assert run.state.status == "succeeded" and run.state.incidents == {}
    assert run.state.output == {
        "code": "RATE_LIMITED",
        "message": "The endpoint refused the request because of its rate limit.",
        "outcome": "failed",
        "attempts": 3,
        "instanceKey": "lookup",
    }
    assert changes(result) == [("lookup", "failed")]


def test_an_unknown_outcome_of_a_step_that_changes_data_is_never_handled():
    flow = compiled(action("place", uses="submit", value={"object": {}}, onError="continue"))
    run = Run(flow, {})
    run.start()
    run.fail("place", "TIMEOUT", outcome="unknown")
    assert (run.state.status, run.state.incident) == ("suspended", "WV-RUNTIME-EVENT")
    assert "place" in run.state.active


def test_a_step_without_the_setting_refuses_a_handled_failure():
    run = Run(compiled(action("lookup")), {})
    run.start()
    run.fail("lookup", "RATE_LIMITED")
    assert (run.state.status, run.state.incident) == ("suspended", "WV-RUNTIME-EVENT")


def test_a_handled_failure_behind_a_pause_waits_for_the_resume():
    run = Run(compiled(action("lookup", onError="continue"), output={"ref": "/steps/lookup/output"}), {})
    run.start()
    run.send("paused")
    run.fail("lookup", "WV-TASK-DEADLINE", outcome="not_started", attempts=0)
    assert [event.type for event in run.state.deferred_results] == ["step_failed"]
    assert run.state.status == "waiting" and "lookup" in run.state.active
    run.send("resumed")
    assert (run.state.status, run.state.output) == ("succeeded", None)
    assert run.state.steps["lookup"]["error"]["code"] == "WV-TASK-DEADLINE"


def test_confirm_it_had_no_effect_runs_the_error_path_of_a_step_that_changes_data():
    flow = compiled(
        action(
            "place",
            uses="submit",
            value={"object": {}},
            onError="errorOutput",
            errorPath=path(transform("why", {"ref": "/steps/place/error/code"}), output={"literal": {"status": "no"}}),
        ),
        output={"object": {"place": {"ref": "/steps/place/output"}, "why": {"ref": "/steps/place/error/code"}}},
    )
    run = Run(flow, {})
    run.start()
    run.send("incident_opened", node_id="place", generation=0, code="WV-TASK-AMBIGUOUS")
    assert run.state.incident == "WV-TASK-AMBIGUOUS"
    run.send("incident_resolved", node_id="place", incident_key="place:0", kind="accept_reconciled_failure", attempts=1)
    assert run.state.status == "succeeded"
    assert run.state.output == {"place": {"status": "no"}, "why": "WV-TASK-RECONCILED-FAILURE"}
    assert run.state.steps["place"]["error"]["outcome"] == "failed"


def test_confirm_it_had_no_effect_is_refused_for_a_step_that_stops_the_run():
    run = Run(compiled(action("place", uses="submit", value={"object": {}})), {})
    run.start()
    run.send("incident_opened", node_id="place", generation=0, code="WV-TASK-AMBIGUOUS")
    # The service refuses it first (409); the kernel never lets the step go on either.
    run.send("incident_resolved", node_id="place", incident_key="place:0", kind="accept_reconciled_failure")
    assert (run.state.status, run.state.incident, run.state.active) == ("suspended", "WV-RUNTIME-EVENT", ["place"])


def test_confirm_it_had_no_effect_is_refused_for_an_interim_failure_that_recovery_still_decides():
    run = Run(compiled(action("lookup", onError="continue")), {})
    run.start()
    run.send("task_failed", node_id="lookup", generation=1, output={"code": "TIMEOUT", "outcome": "failed"})
    assert run.state.incidents["lookup:1"].code == "WV-TASK-FAILED"
    run.send("incident_resolved", node_id="lookup", incident_key="lookup:1", kind="accept_reconciled_failure")
    assert (run.state.status, run.state.active) == ("suspended", ["lookup"])
    assert "lookup" not in run.state.steps


def test_each_loop_iteration_handles_its_own_failure_and_the_results_keep_one_entry_per_element():
    flow = compiled(
        loop(
            "each",
            {"ref": "/input"},
            transform("pick", {"ref": "/item/name"}, onError="continue"),
            output={
                "op": {
                    "name": "coalesce",
                    "args": [{"ref": "/steps/pick/output"}, {"ref": "/steps/pick/error/instanceKey"}],
                }
            },
            concurrency=2,
        ),
        output={"ref": "/steps/each/output"},
    )
    items = [{"name": "a"}, {"name": "b"}, {}, {"name": "d"}, {}]
    run = Run(flow, items)
    result = run.start()
    assert run.state.output == ["a", "b", "pick[2]", "d", "pick[4]"]
    assert [key for key, status in changes(result) if status == "failed"] == ["pick[2]", "pick[4]"]
    assert run.state.branches == {} and run.state.joins == {}


def test_error_paths_inside_loop_iterations_are_released_with_their_iteration():
    flow = compiled(
        loop(
            "each",
            {"ref": "/input"},
            transform("pick", {"ref": "/item/name"}, onError="errorOutput", errorPath=path(output={"literal": "-"})),
            output={"ref": "/steps/pick/output"},
        ),
        output={"ref": "/steps/each/output"},
    )
    run = Run(flow, [{"name": "a"}, {}, {"name": "c"}])
    run.start()
    assert run.state.output == ["a", "-", "c"]
    assert run.state.branches == {} and run.state.joins == {}


def test_an_iteration_waiting_on_its_error_path_keeps_its_frame_until_the_path_finishes():
    flow = compiled(
        loop(
            "each",
            {"ref": "/input"},
            transform(
                "pick",
                {"ref": "/item/name"},
                onError="errorOutput",
                errorPath=path(action("tell", uses="alert", value={"literal": None}), output={"literal": "-"}),
            ),
            output={"ref": "/steps/pick/output"},
        ),
        output={"ref": "/steps/each/output"},
    )
    run = Run(flow, [{"name": "a"}, {}])
    result = run.start()
    assert tasks(result) == ["tell[1]"]
    assert (
        set(run.state.branches) == {"each/body[1]", "pick/errorPath[1]"} and run.state.joins["pick[1]"].mode == "error"
    )
    run.send("task_completed", node_id="tell[1]", output=None)
    assert (run.state.status, run.state.output) == ("succeeded", ["a", "-"])
    assert run.state.branches == {} and run.state.joins == {}


def test_a_fail_step_on_an_error_path_fails_the_run_with_its_code():
    flow = compiled(
        transform(
            "pick",
            MISSING,
            onError="errorOutput",
            errorPath=path({"id": "stop", "kind": "fail", "code": "NO_NAME", "message": "No name."}),
        )
    )
    run = Run(flow, {})
    run.start()
    assert (run.state.status, run.state.incident, run.state.output) == (
        "failed",
        "NO_NAME",
        {"code": "NO_NAME", "message": "No name."},
    )


def test_a_handled_failure_stays_inside_its_parallel_branch():
    flow = compiled(
        {
            "id": "both",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "left": {
                    "steps": [transform("bad", MISSING, onError="continue")],
                    "output": {"ref": "/steps/bad/error/code"},
                },
                "right": {"steps": [action("lookup")], "output": {"ref": "/steps/lookup/output"}},
            },
        },
        output={"ref": "/steps/both/output"},
    )
    run = Run(flow, {})
    run.start()
    run.send("task_completed", node_id="lookup", output={"status": "ok"})
    assert run.state.output == {"left": "WV-EXPR-MISSING", "right": {"status": "ok"}}


def test_a_called_child_that_failed_is_handled_with_its_own_code_and_message():
    call = {
        "id": "enrich",
        "kind": "callWorkflow",
        "uses": "enrich@1.0.0",
        "with": {"object": {}},
        "onError": "continue",
    }
    flow = compiled(call, output={"ref": "/steps/enrich/error"})
    run = Run(flow, {})
    run.start()
    failure = {"code": "NO_RECORD", "message": "No such record."}
    run.send("call_completed", node_id="enrich", generation=1, child_run_id=CHILD, status="failed", output=failure)
    assert run.state.output == {
        "code": "NO_RECORD",
        "message": "No such record.",
        "outcome": "failed",
        "attempts": 2,
        "instanceKey": "enrich",
        "childRunId": CHILD,
    }
    run = Run(flow, {})
    run.start()
    cancelled = {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."}
    run.send("call_completed", node_id="enrich", generation=0, child_run_id=CHILD, status="cancelled", output=cancelled)
    assert (run.state.output["outcome"], run.state.output["message"]) == (
        "unknown",
        "The called workflow was cancelled.",
    )


def test_a_signal_timeout_on_a_step_that_handles_it_runs_its_error_path():
    signal = {
        "id": "confirmed",
        "kind": "signal",
        "name": "confirmed",
        "timeoutSeconds": 60,
        "payloadSchema": {},
        "onError": "errorOutput",
        "errorPath": path(output={"literal": "late"}),
    }
    run = Run(compiled(signal, output={"ref": "/steps/confirmed/output"}), {})
    run.start()
    due = run.state.waits["confirmed"].isoformat()
    early = run.fail("confirmed", "WV-SIGNAL-TIMEOUT", outcome="not_started", deadline=due)
    assert early.state.incident == "WV-RUNTIME-EARLY-WAKEUP"
    run = Run(compiled(signal, output={"ref": "/steps/confirmed/output"}), {})
    run.start()
    run.later(60)
    run.fail("confirmed", "WV-SIGNAL-TIMEOUT", outcome="not_started", deadline=due)
    assert (run.state.status, run.state.output, run.state.waits) == ("succeeded", "late", {})


def test_a_result_that_breaks_the_actions_contract_is_never_handled():
    # The action ran, so its effect happened: a result it should not have returned stops the run as before.
    run = Run(compiled(action("lookup", onError="continue")), {})
    run.start()
    run.send("task_completed", node_id="lookup", generation=1, output={"unexpected": True})
    assert run.state.status == "suspended"
    assert run.state.incident is not None and run.state.incident.startswith("WV-SCHEMA-")
    assert "lookup" not in run.state.steps
