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

"""Node-specific model output classification applies before storing or exporting data."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_llm_workflows import compile_flow, documents

from firefly_weave.operations.debug.models import DebugError
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.operations.exports import safe_event
from firefly_weave.runtime.models import RuntimeEvent


def secret_artifact():
    source, _, _ = documents()
    source["spec"]["llmProfiles"]["assistant"]["outputSchema"] = {"type": "string", "x-secret": True}
    source["spec"]["output"] = {"literal": "done"}
    compiled = compile_flow(source)
    assert compiled.ok, compiled.diagnostics
    return compiled.artifact


def secret_output():
    return {
        "result": "private-model-output-canary",
        "provider": "openai-responses",
        "model": "fixture-model",
        "usage": {"requests": 1, "inputTokens": 10, "outputTokens": 5},
    }


@pytest.mark.parametrize("key", ["node:summarize", "action:generate@1.0.0"])
def test_simulator_rejects_profile_classified_mocks_before_serialization(key):
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(secret_artifact(), input={}, mocks={key: secret_output()}, now=datetime.now(UTC))


@pytest.mark.parametrize("event_type", ["task_completed", "incident_resolved"])
def test_export_defensively_redacts_profile_classified_output(event_type):
    event = RuntimeEvent(
        id=uuid4(),
        type=event_type,
        sequence=2,
        timestamp=datetime.now(UTC),
        data={"node_id": "summarize", "generation": 1, "output": secret_output()},
    )
    assert "private-model-output-canary" not in safe_event(event, secret_artifact()).model_dump_json()


def test_action_mock_must_satisfy_every_node_profile_not_only_the_last():
    source, _, _ = documents()
    source["spec"]["llmProfiles"]["public"] = deepcopy(source["spec"]["llmProfiles"]["assistant"])
    source["spec"]["llmProfiles"]["assistant"]["outputSchema"] = {"type": "string", "x-secret": True}
    source["spec"]["steps"].append({**deepcopy(source["spec"]["steps"][0]), "id": "public", "profile": "public"})
    source["spec"]["output"] = {"literal": "done"}
    compiled = compile_flow(source)
    assert compiled.ok, compiled.diagnostics
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(compiled.artifact, input={}, mocks={"action:generate@1.0.0": secret_output()}, now=datetime.now(UTC))
