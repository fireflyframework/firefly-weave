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
from uuid import UUID, uuid4

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


MODEL_REFUSED = Problem(
    status=422, code="WV-ACTIVATION-AI-MODEL", message="The model is not approved for this AI connection"
)


class PlatformAPI:
    """The public API calls one run makes, with the platform's activation rules.

    A known activation key replays the activation it created. A new key must name the workflow's
    current activation revision, as the platform answers 412 otherwise, and is then refused while
    the policy does not approve the model. Runs fail with LLM_POLICY while it is not approved.
    """

    def __init__(self):
        self.release_id, self.revision_id = uuid4(), uuid4()
        self.scope = {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())}
        self.calls = []
        self.versions = {}
        self.activations = {}
        self.approved = True

    async def publish(self, collection, source, format, *, idempotency_key):
        self.calls.append(("publish", idempotency_key, None))
        name = json.loads(source)["metadata"]["name"]
        identifier = next((key for key, value in self.versions.items() if value == name), uuid4())
        self.versions[identifier] = name
        return SimpleNamespace(id=identifier, digest="a" * 64)

    async def activate(self, request, *, idempotency_key, revision=None):
        self.calls.append(("activate", idempotency_key, revision))
        assert request.connection_revision_ids == {"ai-provider": self.revision_id}
        if idempotency_key in self.activations:
            return self.activations[idempotency_key]
        name = self.versions[request.version_id]
        current = max((item.revision for item in self.activations.values() if item.name == name), default=None)
        if revision != current:
            raise WeaveError(Problem(status=412, code="WV-ETAG", message="Revision precondition failed"))
        if not self.approved:
            raise WeaveError(MODEL_REFUSED)
        activation = SimpleNamespace(id=uuid4(), name=name, revision=(current or 0) + 1)
        self.activations[idempotency_key] = activation
        return activation

    async def list_activations(self, *, limit):
        assert limit == 100
        return SimpleNamespace(items=list(self.activations.values()), next_cursor=None)

    async def start_run(self, request, *, idempotency_key):
        self.calls.append(("start_run", request.activation_id, None))
        return SimpleNamespace(id=uuid4())

    async def read_run(self, identifier):
        status = "succeeded" if self.approved else "failed"
        return SimpleNamespace(state=SimpleNamespace(status=status, output="A short summary."))

    async def history(self, identifier, *, limit):
        if self.approved:
            return SimpleNamespace(
                events=[SimpleNamespace(type="task_completed", data={"output": {"usage": {"requests": 1}}})]
            )
        return SimpleNamespace(events=[SimpleNamespace(type="task_failed", data={"output": {"code": "LLM_POLICY"}})])

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

    def weave(self, *args):
        """The approval commands the journeys run, applied to the platform's policy."""
        if args[:3] == ("ai", "models", "remove") and args[-1] == self.model:
            self.api.approved = False
            return {"approved": []}
        if args[:3] == ("ai", "models", "approve") and args[-1] == "--served":
            self.api.approved = True
            return {"approval": "served"}
        raise AssertionError(f"Unexpected platform command {args}")

    @asynccontextmanager
    async def client(self):
        yield self.api, self.api.scope


def sent(api):
    return [(key, revision) for call, key, revision in api.calls if call == "activate"]


def default_key(api):
    version = next(iter(api.versions))
    return HELPERS["_key"]("activation", f"{version}:{api.release_id}:{api.revision_id}")


async def test_default_runs_replay_one_activation_and_report_its_worker_refusal():
    api = PlatformAPI()
    live = OfflineLive(api)
    first = await live.run(live.model)
    api.approved = False
    again = await live.run(live.model)
    assert sent(api) == [(default_key(api), None)] * 2
    assert first["activation_id"] == again["activation_id"] == str(api.activations[default_key(api)].id)
    assert (first["status"], again["code"]) == ("succeeded", "LLM_POLICY")
    activation = api.activations[default_key(api)].id
    assert [call for call in api.calls if call[0] == "start_run"] == [("start_run", activation, None)] * 2


async def test_a_fresh_activation_key_and_revision_are_sent_unchanged_and_a_refusal_starts_no_run():
    api = PlatformAPI()
    live = OfflineLive(api)
    await live.run(live.model)
    before = dict(api.activations)
    api.approved = False
    with pytest.raises(WeaveError) as stale:
        await live.run(live.model, activation_idempotency_key="fresh-admission")
    assert (stale.value.status, stale.value.code) == (412, "WV-ETAG")
    with pytest.raises(WeaveError) as refused:
        await live.run(live.model, activation_idempotency_key="fresh-admission", activation_revision=1)
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-MODEL")
    assert sent(api)[-1] == ("fresh-admission", 1) and api.calls[-1][0] == "activate"
    assert api.activations == before
    assert len([call for call in api.calls if call[0] == "start_run"]) == 1


async def test_policy_refusal_journeys_replay_or_name_the_current_revision_and_create_nothing():
    api = PlatformAPI()
    live = OfflineLive(api)
    guide = await live.run(live.model)
    # A later revision of the same workflow and an unrelated workflow: only the former's revision applies.
    name = HELPERS["summarize_workflow"](live.model)["metadata"]["name"]
    api.activations["redeployed"] = SimpleNamespace(id=uuid4(), name=name, revision=2)
    api.activations["unrelated"] = SimpleNamespace(id=uuid4(), name="another-workflow", revision=7)
    before = dict(api.activations)
    await JOURNEYS["test_a_model_the_policy_no_longer_approves_fails_with_llm_policy"](live)
    assert sent(api)[-1] == (default_key(api), None)
    assert api.activations[default_key(api)].id == UUID(guide["activation_id"])
    await JOURNEYS["test_a_model_the_policy_no_longer_approves_fails_at_activation"](live)
    fresh_key, revision = sent(api)[-1]
    assert fresh_key not in before and revision == 2
    assert api.activations == before and api.approved


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
