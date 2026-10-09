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

"""Enable AI, run the guide's AI task on a real local model, and check each refusal against real services.

The tests run in file order and share one platform: setup first, then the typed call, then
the refusals; the last one stops and restarts the installation's own Ollama container.
"""

import hashlib

import pytest

from firefly_weave.sdk import platform_ai_setup as setup
from firefly_weave.sdk.errors import WeaveError

pytestmark = pytest.mark.live_ai

MISSING = "weave-missing:1b"
PROVIDER = ("--provider", "openai-chat")


def test_enable_sets_up_ai_and_a_repeated_enable_changes_nothing(live):
    arguments = ("ai", "enable", "--ollama", "container", "--model", live.model, "--yes")
    first = live.weave(*arguments)
    assert (first["ok"], first["stage"], first["mode"], first["weave_ai"]) == (
        True,
        "ready",
        "container",
        "not_available",
    )
    assert live.model in first["models"]
    assert (first["test"]["ok"], first["test"]["model"]) == (True, live.model)

    again = live.weave(*arguments)
    assert again["changed"] == []
    assert again["test"]["ok"] is True

    status = live.weave("ai", "status")
    assert status["enabled"] is True
    assert status["worker"]["presence"] == "recent"
    assert status["gateway"]["state"] == status["ollama"]["state"] == "running"
    assert live.model in {item["name"] for item in status["models"]["served"]}


async def test_the_guide_summarize_workflow_runs_on_the_local_model(live):
    outcome = await live.run(live.model)
    assert outcome["status"] == "succeeded", outcome
    assert isinstance(outcome["output"], str) and outcome["output"].strip()
    assert outcome["usage"].get("requests", 0) >= 1
    assert outcome["replay"] == "consistent"


async def test_a_model_the_policy_no_longer_approves_fails_with_llm_policy(live):
    removed = live.weave("ai", "models", "remove", *PROVIDER, "--model", live.model)
    try:
        assert live.model not in removed["approved"]
        outcome = await live.run(live.model)
        assert outcome["code"] == "LLM_POLICY", outcome
    finally:
        restored = live.weave("ai", "models", "approve", *PROVIDER, "--served")
    assert restored["approval"] == "served"


async def test_an_approved_model_that_ollama_does_not_serve_fails_with_model_not_found(live):
    live.weave("ai", "models", "approve", *PROVIDER, "--model", MISSING)
    try:
        async with live.client() as (client, _):
            test = await setup.test_connection(client, live.receipt()["connection_revision_id"], MISSING)
        assert (test["ok"], test["code"]) == (False, "LLM_MODEL_NOT_FOUND")
        outcome = await live.run(MISSING)
        assert outcome["code"] == "LLM_MODEL_NOT_FOUND", outcome
    finally:
        live.weave("ai", "models", "approve", *PROVIDER, "--served")


@pytest.mark.parametrize(
    "endpoint",
    ["http://ollama-elsewhere.test:11434/v1", "http://203.0.113.7:11434/v1"],
    ids=["origin-without-entry", "public-address"],
)
async def test_plain_http_model_endpoints_without_a_model_entry_are_refused(live, endpoint):
    receipt = live.receipt()
    request = setup.connection_request(receipt["connector_version_id"], endpoint, endpoint.removesuffix("/v1"))
    request = request.model_copy(update={"name": "refused-" + hashlib.sha256(endpoint.encode()).hexdigest()[:8]})
    async with live.client() as (client, _):
        with pytest.raises(WeaveError) as refused:
            await client.invoke("connections.create", body=request)
    assert (refused.value.status, refused.value.code) == (422, "WV-CONNECTION")
    assert "/config/endpoint" in refused.value.problem.model_dump_json()


async def test_a_stopped_ollama_fails_with_llm_unreachable_and_runs_again_after_a_restart(live):
    containers = live.ollama_containers()
    assert containers, "The installation's own Ollama container is missing; these journeys use --ollama container."
    live.docker("stop", *containers)
    try:
        outcome = await live.run(live.model)
        assert outcome["code"] == "LLM_UNREACHABLE", outcome
    finally:
        live.docker("start", *containers)
        live.wait_healthy(containers)
    again = await live.run(live.model)
    assert again["status"] == "succeeded", again
