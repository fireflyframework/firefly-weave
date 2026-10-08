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

"""Step test and draft test activation bodies match the documented snake_case examples."""

import json
import re

import pytest
from pydantic import ValidationError

from firefly_weave.contracts import step_tests as st

REQUEST = {
    "kind": "action",
    "step_id": "check-customer",
    "uses": "onboarding.check-customer@1.0.0",
    "input": {"id": "c-1"},
    "connections": {"crm": "7a8c1f20-5b3d-4e6f-9a81-0c2d3e4f5a6b"},
    "profile": None,
    "acknowledged_side_effect": "non_idempotent",
    "releases": {"weave-http@2.0.0": None},
    "tool_mocks": {},
    "real_tools": [],
    "draft_id": "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
    "draft_revision": 12,
    "timeout_seconds": 120,
}
VIEW = {
    "id": "5c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
    "run_id": "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11",
    "status": "waiting",
    "side_effect": "non_idempotent",
    "pending": [
        {
            "node_id": "support",
            "instance_key": "support#2.1",
            "kind": "confirm",
            "tool": "refund_order",
            "side_effect": "non_idempotent",
            "arguments": {"orderId": "A-17"},
        },
        {
            "node_id": "support",
            "instance_key": "support#3.1.review",
            "kind": "review",
            "tool": "send_reply",
            "task_id": "6d7e8f90-1a2b-4c3d-9e8f-7a6b5c4d3e2f",
        },
        {
            "node_id": "support",
            "instance_key": "support#4.1",
            "kind": "person",
            "tool": "ask_manager",
            "task_id": "7e8f9a0b-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
        },
    ],
    "output": None,
    "error": None,
    "attempts": 1,
    "started_at": "2026-10-07T10:00:00Z",
    "ended_at": None,
    "duration_ms": None,
}


def test_the_step_test_examples_round_trip():
    assert st.StepTestRequest.model_validate_json(json.dumps(REQUEST)).model_dump(mode="json") == REQUEST
    assert st.StepTestView.model_validate_json(json.dumps(VIEW)).model_dump(mode="json") == VIEW


@pytest.mark.parametrize("missing", ["draft_id", "draft_revision"])
def test_a_step_test_names_the_saved_draft_it_runs(missing):
    # A step kind's body leaves both out; Studio adds them when it sends the request.
    body = {name: value for name, value in REQUEST.items() if name != missing}
    with pytest.raises(ValidationError):
        st.StepTestRequest.model_validate_json(json.dumps(body))


@pytest.mark.parametrize(
    ("kind", "seconds", "valid"),
    [("action", 300, True), ("action", 301, False), ("agent", 900, True), ("agent", 901, False)],
)
def test_step_tests_are_capped_at_five_minutes_and_agents_at_fifteen(kind, seconds, valid):
    body = json.dumps({**REQUEST, "kind": kind, "timeout_seconds": seconds})
    if valid:
        st.StepTestRequest.model_validate_json(body)
    else:
        with pytest.raises(ValidationError):
            st.StepTestRequest.model_validate_json(body)


def test_real_tools_list_each_tool_once_and_use_tool_names():
    for tools in (["refund_order", "refund_order"], ["1refund"]):
        with pytest.raises(ValidationError):
            st.StepTestRequest.model_validate_json(json.dumps({**REQUEST, "real_tools": tools}))


@pytest.mark.parametrize(
    "item",
    [
        {"node_id": "support", "instance_key": "support#2.1", "kind": "confirm", "tool": "refund_order"},
        {"node_id": "support", "instance_key": "support#3.1.review", "kind": "review", "tool": "send_reply"},
    ],
)
def test_pending_items_carry_what_their_answer_needs(item):
    with pytest.raises(ValidationError):
        st.StepTestPending.model_validate_json(json.dumps(item))


@pytest.mark.parametrize("change", [{"pending": []}, {"status": "succeeded"}])
def test_pending_items_exist_exactly_while_waiting(change):
    with pytest.raises(ValidationError):
        st.StepTestView.model_validate_json(json.dumps({**VIEW, **change}))


def test_answers_confirm_one_instance():
    answer = st.StepTestAnswer.model_validate_json(json.dumps({"instance_key": "support#2.1", "confirm": True}))
    assert answer.confirm is True
    with pytest.raises(ValidationError):
        st.StepTestAnswer.model_validate_json(json.dumps({"instance_key": "support#2.1", "confirm": "yes"}))


def test_draft_test_activations_default_to_fifteen_minutes_and_stop_at_one_hour():
    body = {
        "draft_id": "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
        "draft_revision": 12,
        "bindings": {"workflow_activation_ids": {"notify-customer@1.0.0": "8f9a0b1c-3d4e-4f50-9a1b-2c3d4e5f6a7b"}},
        "acknowledged_side_effect": "non_idempotent",
    }
    assert st.DraftTestActivationRequest.model_validate_json(json.dumps(body)).lifetime_seconds == 900
    with pytest.raises(ValidationError):
        st.DraftTestActivationRequest.model_validate_json(json.dumps({**body, "lifetime_seconds": 3601}))
    unversioned = {"workflow_activation_ids": {"notify-customer": "8f9a0b1c-3d4e-4f50-9a1b-2c3d4e5f6a7b"}}
    with pytest.raises(ValidationError):
        st.DraftTestActivationRequest.model_validate_json(json.dumps({**body, "bindings": unversioned}))


def test_listening_needs_a_secret_handle_and_returns_a_url_without_a_query():
    listen = st.DraftTestListenRequest.model_validate_json(json.dumps({"secret_ref": "orders-webhook"}))
    assert (listen.max_body_bytes, listen.tolerance_seconds) == (1_048_576, 300)
    with pytest.raises(ValidationError):
        st.DraftTestListenRequest.model_validate_json(json.dumps({}))
    with pytest.raises(ValidationError):
        st.DraftTestListen.model_validate_json(
            json.dumps({"test_url": "https://weave.local/hooks/t?token=x", "expires_at": "2026-10-07T10:15:00Z"})
        )


def test_the_test_policy_is_enabled_or_disabled():
    assert st.EnvironmentTestPolicy.model_validate_json('{"test_calls": "disabled", "revision": 0}').revision == 0
    with pytest.raises(ValidationError):
        st.EnvironmentTestPolicyUpdate.model_validate_json('{"test_calls": "on"}')


def test_a_synthesized_test_is_one_workflow_with_its_strongest_side_effect():
    synthesized = st.SynthesizedTest.model_validate_json(
        json.dumps(
            {
                "workflow": {"apiVersion": "weave/v1alpha1", "kind": "Workflow"},
                "input": {"id": "c-1"},
                "side_effect": "read_only",
                "slots": {"crm": "7a8c1f20-5b3d-4e6f-9a81-0c2d3e4f5a6b"},
                "timeout_seconds": 120,
            }
        )
    )
    assert synthesized.side_effect == "read_only"


PLATFORM_BODIES = [
    st.StepTestRequest,
    st.StepTestAccepted,
    st.StepTestPending,
    st.StepTestError,
    st.StepTestView,
    st.StepTestAnswer,
    st.DraftTestBindings,
    st.DraftTestActivationRequest,
    st.DraftTestActivation,
    st.DraftTestRunRequest,
    st.DraftTestListenRequest,
    st.DraftTestListen,
    st.EnvironmentTestPolicy,
    st.EnvironmentTestPolicyUpdate,
    st.SynthesizedTest,
]


def test_platform_bodies_are_snake_case():
    for model in PLATFORM_BODIES:
        for name, field in model.model_fields.items():
            assert re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)*", field.alias or name), (model.__name__, name)
