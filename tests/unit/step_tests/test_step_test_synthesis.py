# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""A step test runs one step of a saved draft as a one-step workflow that reads the step's input."""

import json
import re
from uuid import UUID, uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.definitions import SEMVER_PATTERN, load_definition
from firefly_weave.contracts.step_tests import DraftStep, StepTestRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests import synthesis

ACTIVATION = UUID("0123456789ab4cde8f0123456789abcd")
CRM = UUID("6f1d2c3b-4a59-4687-9a0b-1c2d3e4f5a6b")
MODEL = UUID("7a2e3d4c-5b6a-4798-8b1c-2d3e4f5a6b7c")
CUSTOMER_INPUT = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}},
    "required": ["customerId"],
    "additionalProperties": False,
}
CUSTOMER_OUTPUT = {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False}


def connector():
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "crm-api", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 60},
            "actions": {
                "get-customer": {
                    "inputSchema": CUSTOMER_INPUT,
                    "outputSchema": CUSTOMER_OUTPUT,
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                }
            },
        },
    }


def action(effect="read_only"):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "fetch-customer", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "crm-api@1.0.0", "action": "get-customer"},
            "inputSchema": CUSTOMER_INPUT,
            "outputSchema": CUSTOMER_OUTPUT,
            "sideEffect": effect,
            "timeoutSeconds": 30,
            "connection": {"connector": "crm-api@1.0.0"},
        },
    }


def catalog(effect="read_only"):
    return CatalogSnapshot.from_definitions(
        [load_definition(connector()), load_definition(action(effect))], adapters=["http"]
    )


def ai_catalog():
    return CatalogSnapshot.from_definitions(
        [load_definition(action_definition()), load_definition(AGENTIC_DESCRIPTOR.manifest.value)],
        tasks=[task_capability()],
        adapters=["weave-agentic-provider"],
    )


FETCH = {
    "id": "fetch-customer",
    "kind": "action",
    "uses": "fetch-customer@1.0.0",
    "with": {"object": {"customerId": {"ref": "/input/customer"}}},
    "connection": "crm",
}
SUMMARIZE = {
    "id": "summarize",
    "kind": "llm",
    "uses": "weave-agentic-generate@1.0.0",
    "profile": "assistant",
    "prompt": {"literal": "Summarize the customer"},
    "context": {"ref": "/steps/fetch-customer/output"},
    "connection": "model",
}
PROFILE = {
    "provider": "openai-responses",
    "model": "fixture-model",
    "options": {"max_tokens": 500},
    "outputSchema": {"type": "string"},
}


def draft(version="1.3.0+build.7"):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "invoice-approval", "version": version},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {},
            "connections": {
                "crm": {"connector": "crm-api@1.0.0"},
                "mail": {"connector": "weave-email@1.0.0"},
                "model": {"connector": "weave-agentic-provider@1.0.0"},
            },
            "llmProfiles": {"assistant": PROFILE, "reviewer": {**PROFILE, "model": "other-model"}},
            "steps": [FETCH, SUMMARIZE],
            "output": {"ref": "/steps/summarize/output/result"},
        },
    }


def request(kind="action", step=FETCH, **changes):
    values = {
        "kind": kind,
        "step_id": step["id"],
        "uses": step["uses"],
        "input": {"customerId": "C-1"},
        "connections": {"crm": str(CRM)},
        "draft_id": str(uuid4()),
        "draft_revision": 4,
        "timeout_seconds": 120,
    }
    values.update(changes)
    # The platform reads requests from JSON, as the API does.
    return StepTestRequest.model_validate_json(json.dumps(values))


def test_an_action_step_becomes_a_one_step_workflow_that_reads_its_input():
    synthesized = synthesis.ActionSynthesizer().synthesize(request(), DraftStep(draft(), FETCH), catalog())
    assert synthesized.workflow == {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "invoice-approval", "version": "1.3.0+build.7"},
        "spec": {
            "inputSchema": CUSTOMER_INPUT,
            "outputSchema": CUSTOMER_OUTPUT,
            "connections": {"crm": {"connector": "crm-api@1.0.0"}},
            "timeoutSeconds": 120,
            "steps": [
                {
                    "id": "fetch-customer",
                    "kind": "action",
                    "uses": "fetch-customer@1.0.0",
                    "with": {"ref": "/input"},
                    "connection": "crm",
                }
            ],
            "output": {"ref": "/steps/fetch-customer/output"},
        },
    }
    assert synthesized.input == {"customerId": "C-1"}
    assert synthesized.slots == {"crm": CRM}
    assert synthesized.timeout_seconds == 120
    compiled = compile_source(synthesized.workflow, format="object", catalog=catalog())
    assert compiled.ok and compiled.diagnostics == (), compiled.diagnostics


@pytest.mark.parametrize(
    "draft_version, expected",
    [
        ("1.3.0+build.7", "1.3.0+test.0123456789ab"),
        ("1.3.0", "1.3.0+test.0123456789ab"),
        ("2.0.0-rc.1", "2.0.0-rc.1+test.0123456789ab"),
        ("2.0.0-rc.1+sha.5114f85", "2.0.0-rc.1+test.0123456789ab"),
    ],
)
def test_the_test_version_keeps_the_drafts_version_and_drops_build_metadata(draft_version, expected):
    version = synthesis.test_version(draft_version, ACTIVATION)
    assert version == expected
    assert re.fullmatch(SEMVER_PATTERN, version)


@pytest.mark.parametrize("effect", ["read_only", "idempotent", "idempotency_key", "non_idempotent"])
def test_the_side_effect_is_the_actions_never_the_requests(effect):
    acknowledged = "read_only" if effect != "read_only" else "non_idempotent"
    synthesized = synthesis.ActionSynthesizer().synthesize(
        request(acknowledged_side_effect=acknowledged), DraftStep(draft(), FETCH), catalog(effect)
    )
    assert synthesized.side_effect == effect


def test_only_the_steps_slot_is_declared():
    synthesized = synthesis.ActionSynthesizer().synthesize(
        request(connections={"crm": str(CRM), "mail": str(uuid4())}), DraftStep(draft(), FETCH), catalog()
    )
    assert list(synthesized.workflow["spec"]["connections"]) == ["crm"]
    assert synthesized.slots == {"crm": CRM}


def test_a_slot_without_a_chosen_connection_is_left_for_admission_to_name():
    synthesized = synthesis.ActionSynthesizer().synthesize(
        request(connections={}), DraftStep(draft(), FETCH), catalog()
    )
    assert synthesized.workflow["spec"]["connections"] == {"crm": {"connector": "crm-api@1.0.0"}}
    assert synthesized.slots == {}


def test_the_workflow_times_out_with_the_step_test():
    synthesized = synthesis.ActionSynthesizer().synthesize(
        request(timeout_seconds=17), DraftStep(draft(), FETCH), catalog()
    )
    assert synthesized.workflow["spec"]["timeoutSeconds"] == 17 and synthesized.timeout_seconds == 17


def test_an_ai_task_keeps_its_profile_and_reads_its_prompt_and_context_from_the_input():
    ai_request = request(
        "llm",
        SUMMARIZE,
        profile="assistant",
        input={"prompt": "Summarize the customer", "context": {"name": "Acme"}},
        connections={"model": str(MODEL)},
    )
    synthesized = synthesis.AiTaskSynthesizer().synthesize(ai_request, DraftStep(draft(), SUMMARIZE), ai_catalog())
    spec = synthesized.workflow["spec"]
    assert spec["llmProfiles"] == {"assistant": PROFILE}
    assert spec["connections"] == {"model": {"connector": "weave-agentic-provider@1.0.0"}}
    assert spec["inputSchema"] == {
        "type": "object",
        "required": ["prompt", "context"],
        "properties": {"prompt": {"type": "string", "minLength": 1}, "context": {}},
    }
    assert spec["steps"] == [
        {
            "id": "summarize",
            "kind": "llm",
            "uses": "weave-agentic-generate@1.0.0",
            "profile": "assistant",
            "prompt": {"ref": "/input/prompt"},
            "context": {"ref": "/input/context"},
            "connection": "model",
        }
    ]
    assert synthesized.slots == {"model": MODEL}
    assert synthesized.side_effect == "non_idempotent"
    compiled = compile_source(synthesized.workflow, format="object", catalog=ai_catalog())
    # Both references are proved present; only the generated action's input containment is left to runtime.
    assert compiled.ok, compiled.diagnostics
    assert [d.code for d in compiled.diagnostics] == ["WV-COMP-UNKNOWN_COMPATIBILITY"]


def test_a_step_whose_action_isnt_in_the_catalog_is_not_found():
    missing = {**FETCH, "uses": "fetch-customer@9.0.0"}
    with pytest.raises(CatalogError) as refused:
        synthesis.ActionSynthesizer().synthesize(
            request(uses="fetch-customer@9.0.0"), DraftStep(draft(), missing), catalog()
        )
    assert (refused.value.status, refused.value.code) == (404, "WV-NOT-FOUND")


@pytest.mark.parametrize(
    "step, change",
    [
        (FETCH, {"connection": "unknown"}),
        (SUMMARIZE, {"profile": "unknown"}),
    ],
)
def test_a_draft_that_doesnt_declare_the_steps_slot_or_profile_is_refused(step, change):
    broken = {**step, **change}
    kind = step["kind"]
    synthesizer = synthesis.ActionSynthesizer() if kind == "action" else synthesis.AiTaskSynthesizer()
    with pytest.raises(CatalogError) as refused:
        synthesizer.synthesize(
            request(kind, broken, profile=broken.get("profile")),
            DraftStep(draft(), broken),
            catalog() if kind == "action" else ai_catalog(),
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-STEPTEST-KIND")


def test_a_second_synthesizer_for_a_kind_is_refused():
    registry = synthesis.StepTestSynthesizers()
    with pytest.raises(ValueError):
        registry.register(synthesis.ActionSynthesizer())


def test_the_registry_knows_actions_and_ai_tasks_and_nothing_else():
    registry = synthesis.StepTestSynthesizers()
    assert isinstance(registry.for_kind("action"), synthesis.ActionSynthesizer)
    assert isinstance(registry.for_kind("llm"), synthesis.AiTaskSynthesizer)
    assert registry.for_kind("agent") is None and registry.for_kind("transform") is None
