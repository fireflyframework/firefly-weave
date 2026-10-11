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

"""Keep the owned model service recoverable, new activations apart from replayed ones, and other platforms untouched."""

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
CONFLICT = Problem(status=409, code="WV-IDEMPOTENCY-CONFLICT", message="Idempotency key was used for another request")


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
        self.requested = {}
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
            # The expected revision is part of the request a key stands for.
            if self.requested[idempotency_key] != revision:
                raise WeaveError(CONFLICT)
            return self.activations[idempotency_key]
        name = self.versions[request.version_id]
        current = max((item.revision for item in self.activations.values() if item.name == name), default=None)
        if revision != current:
            raise WeaveError(Problem(status=412, code="WV-ETAG", message="Revision precondition failed"))
        if not self.approved:
            raise WeaveError(MODEL_REFUSED)
        activation = SimpleNamespace(id=uuid4(), name=name, revision=(current or 0) + 1)
        self.activations[idempotency_key] = activation
        self.requested[idempotency_key] = revision
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
    api.requested.update(redeployed=1, unrelated=6)
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


async def test_a_replayed_key_that_names_another_revision_is_a_conflict_not_a_replay():
    api = PlatformAPI()
    live = OfflineLive(api)
    first = await live.run(live.model)
    api.activations["redeployed"] = SimpleNamespace(id=uuid4(), name=live.workflow_name(live.model), revision=2)
    api.requested["redeployed"] = 1
    with pytest.raises(WeaveError) as conflict:
        await live.run(live.model, activation_revision=2)
    assert (conflict.value.status, conflict.value.code) == (409, "WV-IDEMPOTENCY-CONFLICT")
    assert (await live.run(live.model))["activation_id"] == first["activation_id"]
    assert len([call for call in api.calls if call[0] == "start_run"]) == 2


# Discovery reads the pulled model through the public API: one refresh, then the cached answer.


class DiscoveryAPI:
    def __init__(self, model, *, tools="yes", discovery="ok", cached=None, approval="served"):
        from firefly_weave.contracts.ai import AIModelsResult

        self.revision_id = uuid4()
        self.queries = []
        self.result = AIModelsResult.model_validate_json(
            json.dumps(
                {
                    "approval": approval,
                    "discovery": discovery,
                    "discovered_at": "2026-10-11T10:00:00Z",
                    "models": [
                        {"name": "weave-other:1b", "approved": True, "available": True, "tools": "no"},
                        {"name": model, "approved": True, "available": True, "tools": tools, "context_tokens": 8192},
                    ],
                }
            )
        )
        self.cached = cached

    async def invoke(self, operation, *, query):
        assert operation == "ai_models.list"
        self.queries.append(dict(query))
        if query.get("refresh") == "true":
            return self.result
        return self.cached if self.cached is not None else self.result


class DiscoveryLive(HELPERS["Live"]):
    def __init__(self, api, model="qwen3:4b"):
        super().__init__(Path("unused-installation"), model)
        self.api = api

    def receipt(self):
        return {"stage": "ready", "connection_revision_id": str(self.api.revision_id)}

    @asynccontextmanager
    async def client(self):
        yield self.api, {}


DISCOVERY = "test_discovery_lists_the_pulled_model_with_tool_support"


def test_discovery_runs_right_after_the_enable_journey():
    names = [name for name in JOURNEYS if name.startswith("test_")]
    assert names.index(DISCOVERY) == names.index("test_enable_sets_up_ai_and_a_repeated_enable_changes_nothing") + 1


async def test_discovery_refreshes_once_and_then_reads_the_same_cached_answer():
    api = DiscoveryAPI("qwen3:4b")
    await JOURNEYS[DISCOVERY](DiscoveryLive(api))
    revision = str(api.revision_id)
    assert api.queries == [{"connection": revision, "refresh": "true"}, {"connection": revision}]


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": "unknown"},
        {"tools": "no"},
        {"discovery": "LLM_UNREACHABLE"},
        {"approval": "listed"},
        {"model": "qwen2.5:1.5b"},
        {"cached": "stale"},
    ],
    ids=["tools-unknown", "no-tools", "unreachable", "listed", "model-missing", "cache-differs"],
)
async def test_discovery_requires_the_pulled_model_tool_support_and_a_stable_cache(changes):
    from firefly_weave.contracts.ai import AIModelsResult

    changes = dict(changes)
    model = changes.pop("model", "qwen3:4b")
    stale = changes.pop("cached", None)
    api = DiscoveryAPI(model, **changes)
    if stale:
        api.cached = AIModelsResult(approval="served", discovery="unknown")
    with pytest.raises(AssertionError):
        await JOURNEYS[DISCOVERY](DiscoveryLive(api, "qwen3:4b"))


# The journeys change only an installation this checkout owns, on the owned test Docker context.

OWNED = Path(__file__).resolve().parents[2]


def owned_state(directory, **changes):
    return {"context": "colima-weave-tests", "id": "a1b2c3d4e5f6", "directory": str(directory)} | changes


def labels_for(project, count=3):
    return lambda requested: [{"com.docker.compose.project": project} for _ in range(count)]


@pytest.mark.parametrize("root", ["build/acceptance/run/private/.local/platform", ".local/platform"])
def test_an_owned_installation_on_the_test_context_is_accepted(root):
    directory = OWNED / root
    requested = []

    def labels(project):
        requested.append(project)
        return [{"com.docker.compose.project": project, "com.docker.compose.service": "api"}]

    HELPERS["require_owned"](directory, owned_state(directory), labels)
    assert requested == ["weave-local-a1b2c3d4e5f6"]


@pytest.mark.parametrize("context", ["colima", "default", "desktop-linux", "colima-datacean", ""])
def test_another_docker_context_is_refused_before_docker_is_asked(context):
    directory = OWNED / "build/acceptance/run/private/.local/platform"

    def labels(project):
        raise AssertionError("Docker was asked about another context's containers")

    with pytest.raises(AssertionError, match="colima-weave-tests"):
        HELPERS["require_owned"](directory, owned_state(directory, context=context), labels)


@pytest.mark.parametrize(
    "directory",
    [
        Path("/tmp/weave/platform"),
        Path.home() / ".local/platform",
        OWNED / "build-other/platform",
        OWNED / "tests/live_ai",
        OWNED.parent / "firefly-weave/build/platform",
    ],
    ids=["temporary", "home", "lookalike", "source", "other-checkout"],
)
def test_an_installation_outside_this_checkout_is_refused(directory):
    def labels(project):
        raise AssertionError("Docker was asked about an installation this checkout does not own")

    with pytest.raises(AssertionError, match="build/ or .local/"):
        HELPERS["require_owned"](directory, owned_state(directory), labels)


def test_a_state_file_that_names_another_directory_is_refused():
    directory = OWNED / "build/acceptance/run/private/.local/platform"
    with pytest.raises(AssertionError, match="another directory"):
        HELPERS["require_owned"](directory, owned_state(OWNED / ".local/platform"), labels_for("unused"))


@pytest.mark.parametrize(
    "labels",
    [
        lambda project: [],
        lambda project: [{"com.docker.compose.project": "weave-local-ffffffffffff"}],
        lambda project: [{"com.docker.compose.project": project}, {"com.docker.compose.project": "other"}],
        lambda project: [{"com.docker.compose.service": "api"}],
    ],
    ids=["no-containers", "other-installation", "mixed", "unlabelled"],
)
def test_containers_must_carry_this_installations_compose_project(labels):
    directory = OWNED / "build/acceptance/run/private/.local/platform"
    with pytest.raises(AssertionError, match="Compose project"):
        HELPERS["require_owned"](directory, owned_state(directory), labels)


class GuardedLive(HELPERS["Live"]):
    """Records every command a journey would run; the guard must refuse before any of them."""

    def __init__(self, directory, model):
        super().__init__(directory, model)
        self.commands = []

    def weave(self, *args, timeout=3600):
        self.commands.append(("weave", *args))
        return {}

    def docker(self, *args):
        self.commands.append(("docker", *args))
        if args[:2] == ("ps", "--all"):
            return "c1\nc2\n"
        if args[0] == "inspect":
            return '{"com.docker.compose.project":"weave-local-a1b2c3d4e5f6"}\n' * 2
        raise AssertionError(f"Unexpected docker command {args}")


@pytest.mark.parametrize("context,accepted", [("colima-weave-tests", True), ("colima", False)])
def test_the_live_fixture_checks_ownership_before_any_command(monkeypatch, tmp_path, context, accepted):
    directory = OWNED / "build/acceptance/run/private/.local/platform"
    namespace = HELPERS["open_live"].__globals__
    monkeypatch.setitem(namespace, "local", SimpleNamespace(_load=lambda given: owned_state(given, context=context)))
    monkeypatch.setitem(namespace, "Live", GuardedLive)
    if accepted:
        live = HELPERS["open_live"](directory, "qwen3:4b")
        assert [command[:3] for command in live.commands] == [
            ("docker", "ps", "--all"),
            ("docker", "inspect", "--format"),
        ]
        assert "label=com.docker.compose.project=weave-local-a1b2c3d4e5f6" in live.commands[0]
    else:
        with pytest.raises(AssertionError, match="colima-weave-tests"):
            HELPERS["open_live"](directory, "qwen3:4b")
