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
"""The checks an AI task's step test passes before anything is written, in their order."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.ai_policy import AIPolicy, PolicyEndpoint
from firefly_weave.compiler.api import CompiledArtifact, CompileResult, compile_source
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import agentic
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.step_tests import StepTestRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests import repository as repository_module
from firefly_weave.step_tests import service as service_module
from firefly_weave.step_tests.admission import ReleaseChoice
from firefly_weave.step_tests.synthesis import StepTestSynthesizers

ENDPOINT = "https://models.example/v1"
MODEL = uuid4()
PROFILE = {
    "provider": "openai-chat",
    "model": "chosen",
    "options": {"max_tokens": 500},
    "outputSchema": {"type": "string"},
}
ASK = {
    "id": "ask",
    "kind": "llm",
    "uses": "weave-agentic-generate@1.0.0",
    "profile": "assistant",
    "prompt": {"ref": "/input/question"},
    "context": {"literal": None},
    "connection": "model",
}


def workflow(*steps, mark=None):
    question = {"type": "string", "minLength": 1, **(mark or {})}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "answers", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "required": ["question"], "properties": {"question": question}},
            "outputSchema": {},
            "connections": {"model": {"connector": "weave-agentic-provider@1.0.0"}},
            "llmProfiles": {"assistant": PROFILE},
            "steps": list(steps) or [ASK],
            "output": {"literal": None},
        },
    }


def ai_catalog():
    return CatalogSnapshot.from_definitions(
        [
            load_definition(agentic.action_definition()),
            load_definition(turn_action_definition()),
            load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value),
        ],
        tasks=[agentic.task_capability(), turn_capability()],
        adapters=["weave-agentic-provider"],
    )


def policy(*models):
    entry = PolicyEndpoint(
        id="models",
        label="Models",
        url=ENDPOINT,
        providers=("openai-chat",),
        credential="required",
        models=models,
    )
    return AIPolicy(version=2, endpoints=(entry,))


class Platform:
    """A step test service over a saved draft; what needs the database answers from here."""

    def __init__(self, monkeypatch, draft, approved=("chosen",), provider="openai-chat"):
        self.read = []
        self.policy = policy(*approved) if approved is not None else None
        platform = self

        class Repository:
            def __init__(self, tx):
                pass

            async def draft(self, identifier, revision):
                return draft

            async def environment_name(self):
                return "local"

            async def release_choices(self, artifact):
                platform.read.append("releases")
                return {"weave-agentic.generate@1.0.0": (ReleaseChoice(uuid4(), "only"),)}

            async def connector_version(self, digest):
                return None

        class Definitions:
            def __init__(self, tx):
                pass

            async def snapshot(self, capabilities):
                return ai_catalog()

        class Connections:
            def __init__(self, tx):
                pass

            async def revision(self, identifier):
                config = {"provider": provider, "endpoint": ENDPOINT}
                return SimpleNamespace(connector="weave-agentic-provider@1.0.0", config=config)

        async def nothing(*args, **kwargs):
            return None

        async def enabled(tx):
            return True

        monkeypatch.setattr(repository_module, "StepTestRepository", Repository)
        monkeypatch.setattr(service_module, "DefinitionRepository", Definitions)
        monkeypatch.setattr(service_module, "ConnectionRepository", Connections)
        monkeypatch.setattr(service_module, "load_principal", nothing)
        registry = SimpleNamespace(require_operational=lambda: None, action_validators=dict)
        self.service = service_module.StepTestService(
            None,
            SimpleNamespace(require=lambda *args, **kwargs: None),
            SimpleNamespace(registry=registry, compiler_capabilities=nothing, ai_policy=self),
            SimpleNamespace(require_revision=lambda *args, **kwargs: None, _ready=nothing),
            None,
            SimpleNamespace(enabled=enabled),
            StepTestSynthesizers(),
        )

    def current(self):
        self.read.append("policy")
        return self.policy

    async def admit(self, step=ASK, **changes):
        body = {
            "kind": step["kind"],
            "step_id": step["id"],
            "uses": step["uses"],
            "profile": step.get("profile"),
            "input": {"prompt": "What is it?", "context": None},
            "connections": {"model": str(MODEL)},
            "acknowledged_side_effect": "non_idempotent",
            "draft_id": str(uuid4()),
            "draft_revision": 1,
            "timeout_seconds": 60,
            **changes,
        }
        request = StepTestRequest.model_validate_json(json.dumps(body))
        scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        return await self.service.admit(
            SimpleNamespace(id=uuid4()), scope, request, uuid4(), context=None, tx=SimpleNamespace(session=None)
        )

    async def refused(self, *args, **kwargs):
        with pytest.raises(CatalogError) as raised:
            await self.admit(*args, **kwargs)
        return raised.value.status, raised.value.code


@pytest.mark.parametrize("mark", [{"x-secret": True}, {"writeOnly": True}], ids=["x-secret", "writeOnly"])
async def test_a_text_that_reads_a_value_marked_secret_is_refused_after_the_cheaper_checks_and_before_the_release(
    monkeypatch, mark
):
    platform = Platform(monkeypatch, workflow(mark=mark))
    assert await platform.refused(acknowledged_side_effect=None) == (409, "WV-STEPTEST-CONFIRM")
    assert await platform.refused(connections={}) == (422, "WV-STEPTEST-CONNECTION")
    assert await platform.refused(input={"prompt": ""}) == (422, "WV-STEPTEST-INPUT")
    assert await platform.refused() == (422, "WV-STEPTEST-DRAFT")
    assert "releases" not in platform.read
    admitted = await Platform(monkeypatch, workflow()).admit()
    assert admitted.worker_release_ids.keys() == {"weave-agentic.generate"}


async def test_a_draft_unfinished_after_the_step_still_tests_it_and_one_unfinished_before_it_doesnt(monkeypatch):
    unfinished = {"id": "later", "kind": "action"}
    admitted = await Platform(monkeypatch, workflow(ASK, unfinished)).admit()
    assert admitted.workflow["spec"]["steps"][0]["id"] == "ask"
    assert await Platform(monkeypatch, workflow(unfinished, ASK)).refused() == (422, "WV-STEPTEST-DRAFT")


@pytest.mark.parametrize(
    "approved, provider, code",
    [
        (("other",), "openai-chat", "WV-ACTIVATION-AI-MODEL"),
        (("chosen",), "anthropic", "WV-ACTIVATION-AI-PROVIDER"),
    ],
    ids=["model", "provider"],
)
async def test_a_model_or_provider_the_ai_policy_doesnt_approve_is_refused_as_an_activation_is(
    monkeypatch, approved, provider, code
):
    platform = Platform(monkeypatch, workflow(), approved, provider)
    assert await platform.refused() == (422, code)
    assert "releases" not in platform.read


async def test_a_policy_that_cant_be_read_refuses_an_ai_task(monkeypatch):
    platform = Platform(monkeypatch, workflow())

    def unreadable():
        raise CatalogError(503, "WV-AI-POLICY", "The AI policy can't be read")

    platform.service.definitions.ai_policy = SimpleNamespace(current=unreadable)
    assert await platform.refused() == (503, "WV-AI-POLICY")


async def test_without_a_policy_the_worker_stays_the_only_judge_of_the_model(monkeypatch):
    platform = Platform(monkeypatch, workflow(), approved=None, provider="anthropic")
    assert (await platform.admit()).worker_release_ids
    assert platform.read[0] == "policy"


def stray_turn():
    """A compiled workflow whose action step runs the platform's model turn; the compiler never makes one."""
    agent = {
        "id": "ask",
        "kind": "agent",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": "assistant",
        "connection": "model",
        "instructions": {"literal": "Answer"},
        "prompt": {"literal": "What is it?"},
    }
    compiled = compile_source(workflow(agent), format="object", catalog=ai_catalog())
    value = json.loads(compiled.artifact.to_bytes())
    nodes = value["executable"]["graph"]["nodes"]
    node = next(node for node in nodes if node["kind"] == "agent")
    stray = {key: node[key] for key in ("id", "path", "scope", "connection", "dependency")}
    nodes[nodes.index(node)] = {**stray, "kind": "action", "with": {"object": {"prompt": node["prompt"]}}}
    del value["executable"]["features"]
    value["executable"]["irVersion"] = "weave/ir-v1alpha3"
    value["digest"] = canonical_digest(value["executable"])
    return CompileResult((), artifact=CompiledArtifact(canonical_bytes(value)))


async def test_a_step_that_would_run_the_platforms_model_turn_is_refused_as_an_activation_is(monkeypatch):
    platform = Platform(monkeypatch, workflow(), approved=None)
    monkeypatch.setattr(service_module, "compile_source", lambda *args, **kwargs: stray_turn())
    assert await platform.refused() == (422, "WV-ACTIVATION-AI-RESERVED")
    assert "releases" not in platform.read
