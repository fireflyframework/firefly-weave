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

"""Keep the owned model service recoverable, and keep new activations apart from replayed ones."""

import json
import runpy
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.public import Problem
from firefly_weave.sdk.errors import WeaveError

LIVE = Path(__file__).resolve().parents[1] / "live_ai"
JOURNEYS = runpy.run_path(str(LIVE / "test_ollama_journeys.py"))
HELPERS = runpy.run_path(str(LIVE / "conftest.py"))


class InterruptedService:
    """Apply a mutation, then lose its answer exactly once; recovery commands still work."""

    model = "qwen2.5:1.5b"

    def __init__(self, error):
        self.error = error
        self.approved = {self.model}
        self.running = True
        self.recovered = False

    def weave(self, *args):
        if args[-1] == "--served":
            self.approved = {self.model}
            self.recovered = True
            return {"approval": "served"}
        if args[2] == "remove":
            self.approved.remove(self.model)
        else:
            self.approved.add(args[-1])
        raise self.error

    def ollama_containers(self):
        return ["owned-ollama"]

    def docker(self, command, *containers):
        assert containers == ("owned-ollama",)
        self.running = command == "start"
        if command == "stop":
            raise self.error

    def wait_healthy(self, containers):
        assert containers == ["owned-ollama"] and self.running
        self.recovered = True


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("test-command", 1), json.JSONDecodeError("lost output", "", 0)]
)
@pytest.mark.parametrize(
    "journey",
    [
        "test_a_model_the_policy_no_longer_approves_fails_with_llm_policy",
        "test_a_model_the_policy_no_longer_approves_fails_at_activation",
        "test_an_approved_model_that_ollama_does_not_serve_fails_with_model_not_found",
        "test_a_stopped_ollama_fails_with_llm_unreachable_and_runs_again_after_a_restart",
    ],
)
async def test_ambiguous_mutation_result_still_restores_the_owned_service(journey, error):
    service = InterruptedService(error)
    with pytest.raises(type(error)):
        await JOURNEYS[journey](service)
    assert service.approved == {service.model}
    assert service.running
    assert service.recovered


class PlatformAPI:
    """The public API calls one run makes; each activation key keeps the activation it first created."""

    def __init__(self):
        self.version_id, self.release_id, self.revision_id = uuid4(), uuid4(), uuid4()
        self.scope = {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())}
        self.calls = []
        self.activations = {}
        self.refused = {}
        self.events = [SimpleNamespace(type="task_completed", data={"output": {"usage": {"requests": 1}}})]
        self.status = "succeeded"

    async def publish(self, collection, source, format, *, idempotency_key):
        self.calls.append(("publish", idempotency_key))
        return SimpleNamespace(id=self.version_id, digest="a" * 64)

    async def activate(self, request, *, idempotency_key):
        self.calls.append(("activate", idempotency_key))
        assert request.connection_revision_ids == {"ai-provider": self.revision_id}
        if idempotency_key in self.refused:
            raise WeaveError(self.refused[idempotency_key])
        return SimpleNamespace(id=self.activations.setdefault(idempotency_key, uuid4()))

    async def start_run(self, request, *, idempotency_key):
        self.calls.append(("start_run", request.activation_id))
        return SimpleNamespace(id=uuid4())

    async def read_run(self, identifier):
        return SimpleNamespace(state=SimpleNamespace(status=self.status, output="A short summary."))

    async def history(self, identifier, *, limit):
        return SimpleNamespace(events=self.events)

    async def replay(self, identifier):
        return SimpleNamespace(status="consistent")


class OfflineLive(HELPERS["Live"]):
    def __init__(self, api):
        super().__init__(Path("unused-installation"), "qwen2.5:1.5b")
        self.api = api

    def receipt(self):
        return {
            "stage": "ready",
            "connection_revision_id": str(self.api.revision_id),
            "release_id": str(self.api.release_id),
        }

    @asynccontextmanager
    async def client(self):
        yield self.api, self.api.scope


def sent_keys(api):
    return [key for call, key in api.calls if call == "activate"]


async def test_default_runs_replay_one_activation_and_report_its_worker_refusal():
    api = PlatformAPI()
    live = OfflineLive(api)
    first = await live.run(live.model)
    api.status = "failed"
    api.events = [SimpleNamespace(type="task_failed", data={"output": {"code": "LLM_POLICY"}})]
    again = await live.run(live.model)
    pins = f"{api.version_id}:{api.release_id}:{api.revision_id}"
    assert sent_keys(api) == [HELPERS["_key"]("activation", pins)] * 2
    assert first["activation_id"] == again["activation_id"] == str(next(iter(api.activations.values())))
    assert (first["status"], again["code"]) == ("succeeded", "LLM_POLICY")
    assert [call for call in api.calls if call[0] == "start_run"] == [
        ("start_run", api.activations[sent_keys(api)[0]])
    ] * 2


async def test_a_fresh_activation_key_is_sent_unchanged_and_its_refusal_starts_no_run():
    api = PlatformAPI()
    live = OfflineLive(api)
    await live.run(live.model)
    before = dict(api.activations)
    api.refused["fresh-admission"] = Problem(
        status=422, code="WV-ACTIVATION-AI-MODEL", message="The model is not approved for this AI connection"
    )
    with pytest.raises(WeaveError) as refused:
        await live.run(live.model, activation_idempotency_key="fresh-admission")
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-MODEL")
    assert sent_keys(api)[-1] == "fresh-admission" and api.calls[-1] == ("activate", "fresh-admission")
    assert api.activations == before
    assert len([call for call in api.calls if call[0] == "start_run"]) == 1


class ActivationPages:
    def __init__(self, items, next_cursor):
        self.page = SimpleNamespace(items=items, next_cursor=next_cursor)
        self.limits = []

    async def list_activations(self, *, limit):
        self.limits.append(limit)
        return self.page

    @asynccontextmanager
    async def client(self):
        yield self, {}


async def test_activation_snapshot_is_exhaustive_or_refuses_to_compare():
    items = [SimpleNamespace(id=uuid4()) for _ in range(3)]
    complete = ActivationPages(items, None)
    assert await JOURNEYS["activation_ids"](complete) == frozenset(str(item.id) for item in items)
    assert complete.limits == [100]
    with pytest.raises(AssertionError, match="exceeds its activation snapshot bound"):
        await JOURNEYS["activation_ids"](ActivationPages(items, "more"))
