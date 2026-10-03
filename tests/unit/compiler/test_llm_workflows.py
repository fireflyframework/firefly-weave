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

"""AI steps compile to bounded durable tasks with pinned workflow profiles."""

import pytest
from test_analysis import connector, workflow
from test_llm_contracts import profile

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition


def documents():
    connection = connector()
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "generate", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "weave-agentic.generate", "taskVersion": "1.0.0"},
            "inputSchema": {},
            "outputSchema": {},
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 120,
            "connection": {"connector": "service@1.0.0", "required": True},
        },
    }
    source = workflow(
        [
            {
                "id": "summarize",
                "kind": "llm",
                "uses": "generate@1.0.0",
                "profile": "assistant",
                "prompt": {"literal": "Summarize"},
                "context": {"literal": {}},
                "connection": "provider",
            }
        ],
        output={"ref": "/steps/summarize/output/result"},
        output_schema={"type": "string"},
    )
    source["spec"].update(
        llmProfiles={"assistant": profile()}, connections={"provider": {"connector": "service@1.0.0"}}
    )
    return source, action, connection


def compile_flow(source=None, action_change=None):
    original, action, connection = documents()
    if action_change:
        action["spec"].update(action_change)
    task = {
        "taskType": "weave-agentic.generate",
        "taskVersion": "1.0.0",
        "inputSchema": {},
        "outputSchema": {},
        "sideEffect": "non_idempotent",
        "timeoutSeconds": 120,
    }
    return compile_source(
        source or original,
        format="object",
        catalog=CatalogSnapshot.from_definitions(
            [load_definition(action), load_definition(connection)], tasks=[task], adapters=["http"]
        ),
    )


def test_llm_profile_is_pinned_in_a_durable_action_and_requires_feature_ir():
    result = compile_flow()
    assert result.ok, result.diagnostics
    executable = result.artifact.executable
    assert executable["irVersion"] == "weave/ir-v1alpha3"
    node = next(node for node in executable["graph"]["nodes"] if node["id"] == "summarize")
    assert node["kind"] == "action"
    assert node["llmProfile"]["model"] == "fixture-model"
    assert node["with"]["object"]["profile"]["literal"] == node["llmProfile"]
    assert import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "missing"},
        {"prompt": {"literal": 42}},
        {"connection": "missing"},
        {"context": {"ref": "/steps/future/output"}},
    ],
)
def test_invalid_llm_configuration_has_a_source_diagnostic(change):
    source, _, _ = documents()
    source["spec"]["steps"][0].update(change)
    result = compile_flow(source)
    assert not result.ok
    assert any(d.path.startswith("/spec/steps/0") for d in result.diagnostics)


@pytest.mark.parametrize(
    "change",
    [
        {"retry": {"maxAttempts": 2}},
        {"timeoutSeconds": 60},
        {"sideEffect": "read_only"},
        {"implementation": {"kind": "worker", "taskType": "other", "taskVersion": "1.0.0"}},
    ],
)
def test_llm_cannot_use_an_unsafe_action_policy(change):
    assert not compile_flow(action_change=change).ok


def test_actual_agentic_catalog_compiles_without_provider_runtime():
    from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability

    source, _, _ = documents()
    source["spec"]["connections"]["provider"]["connector"] = "weave-agentic-provider@1.0.0"
    source["spec"]["steps"][0]["uses"] = "weave-agentic-generate@1.0.0"
    catalog = CatalogSnapshot.from_definitions(
        [load_definition(action_definition()), load_definition(AGENTIC_DESCRIPTOR.manifest.value)],
        tasks=[task_capability()],
        adapters=["weave-agentic-provider"],
    )
    result = compile_source(source, format="object", catalog=catalog)
    assert result.ok, result.diagnostics


@pytest.mark.parametrize("mutation", ["input", "guard", "policy", "old-ir"])
def test_import_rechecks_llm_profile_policy_and_result_validation(mutation):
    import json

    from firefly_weave.compiler.canonical import canonical_digest

    result = compile_flow()
    assert result.ok, result.diagnostics
    envelope = json.loads(result.artifact.to_bytes())
    executable = envelope["executable"]
    node = next(n for n in executable["graph"]["nodes"] if n["id"] == "summarize")
    if mutation == "input":
        node["with"]["object"]["profile"]["literal"]["model"] = "different"
    elif mutation == "guard":
        executable["guards"] = [g for g in executable["guards"] if g["purpose"] != "action_output"]
    elif mutation == "old-ir":
        executable["irVersion"] = "weave/ir-v1alpha1"
    else:
        dependency = next(d for d in executable["dependencies"] if d["kind"] == "Action")
        dependency["document"]["spec"]["retry"]["maxAttempts"] = 2
        dependency["digest"] = canonical_digest(dependency["document"])
        node["dependency"] = dependency["digest"]
    envelope["digest"] = canonical_digest(executable)
    with pytest.raises(ValueError):
        import_artifact(envelope)


@pytest.mark.parametrize("bad", ["result", "provider", "model", "usage"])
def test_kernel_rejects_wrong_result_or_profile_metadata_without_advancing(bad):
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    artifact = compile_flow().artifact

    def event(kind, seq, data):
        return RuntimeEvent(id=uuid4(), type=kind, sequence=seq, timestamp=datetime(2026, 10, 3, tzinfo=UTC), data=data)

    waiting = transition(RunState(input={}), event("started", 1, {}), artifact)
    assert waiting.state.status == "waiting" and len(waiting.commands) == 1
    output = {
        "result": "summary",
        "provider": "openai-responses",
        "model": "fixture-model",
        "usage": {"requests": 1, "inputTokens": 10, "outputTokens": 5},
    }
    valid = transition(waiting.state, event("task_completed", 2, {"node_id": "summarize", "output": output}), artifact)
    assert valid.state.status == "succeeded" and valid.state.output == "summary"
    if bad == "result":
        output["result"] = 42
    elif bad == "usage":
        output["usage"]["requests"] = 9
    else:
        output[bad] = "anthropic" if bad == "provider" else "unexpected-model"
    failed = transition(waiting.state, event("task_completed", 2, {"node_id": "summarize", "output": output}), artifact)
    assert failed.state.status == "suspended"
    assert "summarize" not in failed.state.steps
    assert not failed.commands
