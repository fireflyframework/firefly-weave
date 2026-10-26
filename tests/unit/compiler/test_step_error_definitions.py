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

"""Eligible steps accept onError and, with errorOutput only, an error path; nothing else changes bytes."""

from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from firefly_weave.compiler.calls import called_references, workflow_steps
from firefly_weave.contracts import definitions
from firefly_weave.contracts.language import language_manifest
from firefly_weave.contracts.language_features import ERROR_KINDS, LANGUAGE_FEATURES, STEP_FIELD_FEATURES
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.step_errors import STEP_ERROR_SCHEMA, StepError, step_error, strongest

STEPS = {
    "action": {"id": "s", "kind": "action", "uses": "send@1.0.0", "with": {"object": {}}},
    "llm": {
        "id": "s",
        "kind": "llm",
        "uses": "ask@1.0.0",
        "profile": "answer",
        "prompt": {"literal": "Hi"},
        "context": {"object": {}},
        "connection": "ai",
    },
    "transform": {"id": "s", "kind": "transform", "value": {"literal": 1}},
    "decisionTable": {"id": "s", "kind": "decisionTable", "uses": "rate@1.0.0", "with": {"object": {}}},
    "callWorkflow": {"id": "s", "kind": "callWorkflow", "uses": "child@1.0.0", "with": {"object": {}}},
    "humanTask": {
        "id": "s",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": {"literal": "Review"},
        "context": {"object": {}},
        "formSchema": {},
    },
    "signal": {"id": "s", "kind": "signal", "name": "confirmed", "timeoutSeconds": 60, "payloadSchema": {}},
    "transaction": {
        "id": "s",
        "kind": "transaction",
        "steps": [{"id": "t", "kind": "transform", "value": {"literal": 1}}],
        "output": {"literal": None},
    },
}
INELIGIBLE = {
    "switch": {
        "id": "s",
        "kind": "switch",
        "cases": [{"when": {"literal": True}, "steps": [], "output": {"literal": 1}}],
        "default": {"steps": [], "output": {"literal": None}},
    },
    "parallel": {
        "id": "s",
        "kind": "parallel",
        "branches": {"a": {"steps": [], "output": {"literal": 1}}},
        "concurrency": 1,
    },
    "forEach": {
        "id": "s",
        "kind": "forEach",
        "items": {"literal": []},
        "body": {"steps": [], "output": {"literal": 1}},
    },
    "wait": {"id": "s", "kind": "wait", "durationSeconds": 1},
    "fail": {"id": "s", "kind": "fail", "code": "NO", "message": "No."},
}
NULL = {"literal": None}
PATH = {"steps": [{"id": "alert", "kind": "transform", "value": {"ref": "/steps/s/error/code"}}], "output": NULL}
CHILD = "6f1c4a8e-2d0b-4c41-9a54-6e2f5b7d9c10"
STEP: TypeAdapter[definitions.Step] = TypeAdapter(definitions.Step)


@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_every_eligible_kind_accepts_each_setting_and_keeps_it_in_its_bytes(kind):
    for policy in ("stop", "continue"):
        step = STEP.validate_python({**STEPS[kind], "onError": policy})
        assert step.model_dump(by_alias=True)["onError"] == policy
    step = STEP.validate_python({**STEPS[kind], "onError": "errorOutput", "errorPath": PATH})
    dumped = step.model_dump(by_alias=True)
    assert (dumped["onError"], dumped["errorPath"]["steps"][0]["id"]) == ("errorOutput", "alert")


@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_a_step_without_the_setting_keeps_its_bytes(kind):
    dumped = STEP.validate_python(STEPS[kind]).model_dump(by_alias=True)
    assert "onError" not in dumped and "errorPath" not in dumped


@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_an_error_path_goes_with_error_output_and_nothing_else(kind):
    for value in ({"onError": "errorOutput"}, {"errorPath": PATH}, {"onError": "continue", "errorPath": PATH}):
        with pytest.raises(ValidationError, match="errorPath goes with onError: errorOutput"):
            STEP.validate_python({**STEPS[kind], **value})
    with pytest.raises(ValidationError):
        STEP.validate_python({**STEPS[kind], "onError": None})
    with pytest.raises(ValidationError):
        STEP.validate_python({**STEPS[kind], "onError": "retry"})


@pytest.mark.parametrize("kind", sorted(INELIGIBLE))
def test_containers_waits_and_fail_steps_take_no_setting(kind):
    STEP.validate_python(INELIGIBLE[kind])
    with pytest.raises(ValidationError):
        STEP.validate_python({**INELIGIBLE[kind], "onError": "continue"})


def test_a_call_handles_its_errors_only_when_it_waits():
    detached = {**STEPS["callWorkflow"], "mode": "detach"}
    with pytest.raises(ValidationError, match="onError applies only when the call waits for its result"):
        STEP.validate_python({**detached, "onError": "continue"})
    assert STEP.validate_python({**STEPS["callWorkflow"], "mode": "wait", "onError": "continue"}).on_error == "continue"


def test_error_paths_are_walked_like_every_other_block():
    document = {
        "kind": "Workflow",
        "spec": {
            "steps": [
                {
                    **STEPS["action"],
                    "onError": "errorOutput",
                    "errorPath": {
                        "steps": [{"id": "fallback", "kind": "callWorkflow", "uses": "backup@1.0.0", "with": NULL}],
                        "output": NULL,
                    },
                }
            ]
        },
    }
    assert [path for _, path in workflow_steps(document)] == ["/spec/steps/0", "/spec/steps/0/errorPath/steps/0"]
    assert called_references(document) == ["backup@1.0.0"]


def test_the_feature_and_its_fields_are_named_once():
    assert "flow.onError" in LANGUAGE_FEATURES
    assert {name: STEP_FIELD_FEATURES[name] for name in ("onError", "errorPath")} == {
        "onError": "flow.onError",
        "errorPath": "flow.onError",
    }
    entry = next(field for field in language_manifest().step_fields if field.name == "onError")
    assert entry.model_dump(mode="json") == {
        "name": "onError",
        "feature": "flow.onError",
        "kinds": ["action", "llm", "transform", "decisionTable", "callWorkflow", "humanTask", "signal", "transaction"],
        "values": ["stop", "continue", "errorOutput"],
        "path": "/onError",
        "default": "stop",
        "block": {"name": "errorPath", "path": "/errorPath", "label": "On error"},
        "studio": "pending",
    }


def test_the_workflow_schema_names_the_policy_and_the_error_object_has_its_own_schema():
    schemas = export_schemas()
    assert get_args(definitions.StepErrorPolicy.__value__) == ("stop", "continue", "errorOutput")
    assert "StepErrorPolicy" in schemas["workflow"]["$defs"]
    assert schemas["step-error"]["required"] == ["code", "message", "outcome", "attempts", "instanceKey"]
    assert set(schemas["step-error"]["properties"]) == set(STEP_ERROR_SCHEMA["properties"])


def test_the_error_object_is_closed_camel_case_and_bounded():
    error = step_error("RATE_LIMITED", "failed", 3, "send[3]")
    assert error == {
        "code": "RATE_LIMITED",
        "message": "The endpoint refused the request because of its rate limit.",
        "outcome": "failed",
        "attempts": 3,
        "instanceKey": "send[3]",
    }
    child = step_error("REFUSED", "failed", 1, "place", message="Refused by the other system.", child_run_id=CHILD)
    assert (child["message"], child["childRunId"]) == ("Refused by the other system.", CHILD)
    assert step_error("CUSTOM_CODE", "unknown", 0, "x")["message"] == "The step failed with code CUSTOM_CODE."
    assert len(step_error("X", "failed", 1, "x", message="m" * 600)["message"]) == 512
    with pytest.raises(ValidationError):
        StepError.model_validate({**error, "extra": 1})
    with pytest.raises(ValidationError):
        StepError.model_validate({**error, "instanceKey": "send[03]"})


def test_the_strongest_outcome_wins_over_attempts():
    assert strongest([]) == "not_started"
    assert strongest(["not_started", "failed"]) == "failed"
    assert strongest(["failed", "unknown", "not_started"]) == "unknown"
