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

"""weave platform ai: stages in order, idempotent reruns, consent, the three Ollama modes, status and disable."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ai_platform_support import (  # noqa: F401
    OWNER,
    SCOPE,
    SERVER_IMAGE,
    SUBNET,
    WORKER_SECRET,
    catalog_output,
    manifest_output,
    owned_fixture,
)

from firefly_weave import ai_policy, ollama
from firefly_weave.contracts.public import Problem
from firefly_weave.sdk import platform, platform_ai, platform_ai_files, platform_docker, platform_origins
from firefly_weave.sdk.errors import WeaveError

API = "http://127.0.0.1:55204"
IMAGE = "sha256:" + "e" * 64
CONNECTOR = "66666666-6666-4666-8666-666666666666"
RELEASE = "44444444-4444-4444-8444-444444444444"
REVISION = "77777777-7777-4777-8777-777777777777"
MODEL = "qwen2.5:1.5b"
REBUILT = "sha256:" + "a" * 64
# The real Docker start, kept before the harness replaces it.
DOCKER_START = platform_docker.start


class Server:
    """The server steps, recorded; the HTTP operations behind them are tested on their own."""

    def __init__(self):
        self.calls, self.connections = [], set()
        self.scope_visible = False
        self.answer = {
            "ok": True,
            "code": "ok",
            "latency_ms": 900,
            "model": MODEL,
            "tool_calling": "supported",
            "tested_at": "2026-10-08T10:00:00+00:00",
        }

    async def ensure_worker_scope(self, keycloak, admin_secret, transport=None):
        changed = not self.scope_visible
        self.scope_visible = True
        return changed

    async def publish(self, client, collection, document, digest):
        self.calls.append(("publish", collection))
        return CONNECTOR if collection == "connectors" else str(uuid4())

    async def admit_release(self, client, image, manifest):
        self.calls.append(("release", image))
        return RELEASE

    async def worker_principal(self, client, scope, *, release_id, issuer, subject, receipt, save):
        if receipt.get("granted_release_id") == release_id:
            return False
        save({"principal_id": str(uuid4()), "linked": True, "granted_release_id": release_id})
        return True

    async def ensure_connection(self, client, request):
        endpoint = request.config["endpoint"]
        created = endpoint not in self.connections
        self.connections.add(endpoint)
        self.calls.append(("connection", endpoint, request.secret_refs["apiKey"]))
        return REVISION, created

    async def wait_online(self, client, release_id):
        return {"presence": "recent", "last_seen_at": "2026-10-08T10:00:00+00:00"}

    async def presence(self, client, release_id):
        return {"presence": "recent", "last_seen_at": "2026-10-08T10:00:00+00:00"}

    async def test_connection(self, client, revision_id, model):
        self.calls.append(("test", model))
        return dict(self.answer)

    async def smoke(self, client, scope, *, release_id, revision_id, model):
        self.calls.append(("smoke", model))
        return {"run_id": str(uuid4()), "status": "succeeded", "requests": 1, "replay": "consistent"}


@pytest.fixture(name="harness")
def harness_fixture(owned, monkeypatch):
    directory, _, runner = owned
    identifier = str(uuid4())
    platform._write(
        directory / "first-run.json",
        {
            "status": "succeeded",
            "run_id": identifier,
            "version_id": identifier,
            "activation_id": identifier,
            "output": {"message": "Hello, Weave"},
            "scope": SCOPE,
        },
    )
    context = directory / "release" / "worker-images" / "agentic"
    context.mkdir(parents=True)
    (context / "Dockerfile").write_bytes(b"FROM scratch\n")
    manifest = json.dumps({"inputs": {"Dockerfile": hashlib.sha256(b"FROM scratch\n").hexdigest()}}).encode()
    (context / "release.json").write_bytes(manifest)
    (directory / "release" / "release.json").write_text(
        json.dumps({"workers": {"agentic": {"context_sha256": hashlib.sha256(manifest).hexdigest()}}})
    )
    h = SimpleNamespace(directory=directory, runner=runner, served=[], pulls=[], starts=[], addresses=["10.246.21.9"])
    h.server = Server()
    h.contexts, h.tools = {}, {}

    def probe(command):
        reachable = h.addresses is not None
        report = {
            "addresses": h.addresses or [],
            "version": "0.12.3" if reachable else None,
            "models": [
                ollama.ServedModel(
                    name=name, size_bytes=1, context_tokens=h.contexts.get(name, 32768), tools=h.tools.get(name, "yes")
                ).model_dump(mode="json")
                for name in h.served
            ],
        }
        return (ollama.MARKER + json.dumps(report)).encode()

    def build(command):
        path = command[command.index("--iidfile") + 1]
        with open(path, "w") as stream:
            stream.write(IMAGE)
        return b""

    network = [
        {
            "Name": f"weave-local-{OWNER}_default",
            "Labels": {"com.docker.compose.project": f"weave-local-{OWNER}"},
            "IPAM": {"Config": [{"Subnet": SUBNET}]},
        }
    ]
    runner.answers.update(
        {
            "ai-network": json.dumps(network).encode(),
            "ai-probe": probe,
            "ai-image-build": build,
            "ai-image-inspect": json.dumps([{"Id": IMAGE, "Config": {"User": "65532:65532"}}]).encode(),
            "ai-image-check": IMAGE.encode(),
            "ai-catalog": catalog_output(),
            "ai-release-manifest": manifest_output(),
            "ai-service-state": b"running\n",
        }
    )

    def pull(base, name, progress):
        h.pulls.append((base, name))
        h.served.append(name)

    monkeypatch.setattr(ollama, "pull", pull)
    monkeypatch.setattr(ollama, "probe", lambda origin, opener=None, budget=10.0: {"version": None, "models": []})
    monkeypatch.setattr(platform, "_ready_api", lambda directory, state: API)
    monkeypatch.setattr(platform, "_host_token", lambda state: "host-token")
    monkeypatch.setattr(platform, "_probe", lambda url, issuer=None: True)
    monkeypatch.setattr(
        platform_docker, "start", lambda state, notice: h.starts.append(platform_docker.environment(state, {}))
    )
    monkeypatch.setattr(
        platform_docker, "inspect", lambda state: {"state": "running", "id": "f" * 64, "network_mode": "x"}
    )
    for name in (
        "ensure_worker_scope",
        "publish",
        "admit_release",
        "worker_principal",
        "ensure_connection",
        "wait_online",
        "presence",
        "test_connection",
        "smoke",
    ):
        monkeypatch.setattr(platform_ai.setup, name, getattr(h.server, name))
    return h


def enable(h, **changes):
    arguments = {"ollama_mode": "container", "models": [MODEL], "confirm": lambda text: True}
    arguments.update(changes)
    return platform_ai.enable(h.directory, **arguments)


def test_a_refused_managed_oauth_scope_update_never_starts_the_worker(harness, monkeypatch):
    async def refused(*args):
        raise platform.PlatformError("The local Keycloak refused the worker scope update.")

    monkeypatch.setattr(platform_ai.setup, "ensure_worker_scope", refused, raising=False)
    with pytest.raises(platform.PlatformError, match="refused.*worker scope"):
        enable(harness)
    assert "ai-services" not in harness.runner.names()


def saved(h):
    return json.loads((h.directory / "ai.json").read_text())


def in_order(names, expected):
    position = 0
    for name in names:
        if position < len(expected) and name == expected[position]:
            position += 1
    return position == len(expected)


def test_enable_runs_every_stage_in_order_and_saves_a_ready_receipt(harness):
    h = harness
    result = enable(h, verify=True)
    assert result["stage"] == "ready" and result["mode"] == "container"
    assert result["endpoint"] == "http://ollama:11434/v1" and result["weave_ai"] == "not_available"
    assert result["changed"] == [
        "ollama",
        "models",
        "origins",
        "policy",
        "settings",
        "image",
        "authentication",
        "catalog",
        "release",
        "principal",
        "connection",
        "services",
    ]
    assert result["smoke"]["status"] == "succeeded" and result["test"]["ok"] is True
    receipt = saved(h)
    assert h.pulls == [(f"http://127.0.0.1:{receipt['ollama_port']}", MODEL)]
    assert receipt["networks"] == [SUBNET] and receipt["models"] == [MODEL] and receipt["context_tokens"] == 8192
    assert (
        receipt["release_id"] == RELEASE
        and receipt["connection_revision_id"] == REVISION
        and receipt["image_id"] == IMAGE
    )
    assert in_order(
        h.runner.names(),
        [
            "ai-network",
            "ai-ollama",
            "ai-probe",
            "ai-probe",
            "ai-image-build",
            "ai-catalog",
            "ai-release-manifest",
            "ai-services",
        ],
    )
    assert ("connection", "http://ollama:11434/v1", "no-credential") in h.server.calls
    assert json.loads(h.starts[-1]["WEAVE_LUMI_GATEWAY"])["endpoint"] == "http://127.0.0.1:8090/v1/lumi"
    entries = json.loads((h.directory / "platform.json").read_text())["private_origins"]["ai"]["entries"]
    assert {(entry["origin"], entry["purpose"]) for entry in entries} == {
        ("http://ollama:11434", "model"),
        ("http://127.0.0.1:8090", "model"),
        ("http://127.0.0.1:8080", "worker-auth"),
        ("http://127.0.0.1:8000", "platform-api"),
    }
    token = (h.directory / "ai-secrets" / "gateway-token").read_text().strip()
    raw = (h.directory / "ai.json").read_text()
    assert WORKER_SECRET not in raw and token not in raw and "host-token" not in raw


def test_rerunning_enable_reports_no_changes(harness):
    h = harness
    enable(h)
    builds = h.runner.names().count("ai-image-build")
    again = enable(h, confirm=platform_ai.refuse_confirmation)
    assert again["changed"] == [] and again["stage"] == "ready"
    assert len(h.pulls) == 1 and h.runner.names().count("ai-image-build") == builds


def test_a_non_interactive_run_without_yes_changes_nothing(harness):
    h = harness
    with pytest.raises(platform.PlatformError, match="--yes"):
        enable(h, confirm=platform_ai.refuse_confirmation)
    assert not (h.directory / "ai.json").exists() and h.pulls == []
    assert "ai-ollama" not in h.runner.names()
    assert "private_origins" not in json.loads((h.directory / "platform.json").read_text())


def test_another_mode_is_refused_until_disable(harness):
    h = harness
    enable(h)
    with pytest.raises(platform.PlatformError, match="weave platform ai disable"):
        enable(h, ollama_mode="host")
    assert saved(h)["mode"] == "container"


def test_an_interrupted_pull_keeps_the_receipt_before_models(harness, monkeypatch):
    h = harness

    def interrupted(base, name, progress):
        raise ValueError(f"Ollama could not pull {name}: no space left on device")

    monkeypatch.setattr(ollama, "pull", interrupted)
    with pytest.raises(platform.PlatformError, match="no space left on device.*rerun to resume"):
        enable(h)
    assert saved(h)["stage"] == "ollama"
    assert not (h.directory / "ai-config" / "ai-policy.json").exists()


@pytest.mark.parametrize(
    "system,expected", [("linux", platform_ai.LINUX_BINDING), ("darwin", platform_ai.MACOS_BINDING)]
)
def test_host_mode_explains_an_ollama_bound_to_loopback(harness, monkeypatch, system, expected):
    h = harness
    h.addresses = None
    monkeypatch.setattr(platform_ai.sys, "platform", system)
    with pytest.raises(platform.PlatformError) as refused:
        enable(h, ollama_mode="host")
    assert str(refused.value) == expected
    probe = next(command for name, command in h.runner.calls if name == "ai-probe")
    assert "--add-host" in probe and "host.docker.internal:host-gateway" in probe


def test_host_mode_uses_the_host_gateway_address_and_a_keycloak_override(harness):
    h = harness
    h.addresses, h.served = ["192.168.5.2"], [MODEL]
    result = enable(h, ollama_mode="host")
    receipt = saved(h)
    assert result["endpoint"] == "http://host.docker.internal:11434/v1" and receipt["networks"] == ["192.168.5.2/32"]
    assert h.pulls == [] and "ai-ollama" not in h.runner.names() and "ai-keycloak" in h.runner.names()
    assert (h.directory / "compose.ai-host.json").exists()
    assert any("OLLAMA_CONTEXT_LENGTH" in warning for warning in result["warnings"])


def test_url_mode_never_pulls_and_needs_served_models(harness):
    h = harness
    h.addresses = ["10.246.21.50"]
    with pytest.raises(platform.PlatformError, match="Pull it on that server"):
        enable(h, ollama_mode=None, ollama_url="http://ollama.acceptance.test:11434")
    h.served = [MODEL]
    result = enable(h, ollama_mode=None, ollama_url="http://ollama.acceptance.test:11434")
    assert result["stage"] == "ready" and saved(h)["networks"] == ["10.246.21.50/32"] and h.pulls == []


@pytest.mark.parametrize(
    "url",
    ["http://keycloak:8080", "http://127.0.0.1:11434", "https://ollama.acceptance.test:11434", "http://metadata:80"],
)
def test_url_mode_refuses_platform_loopback_tls_and_metadata_origins(harness, url):
    h = harness
    with pytest.raises(platform.PlatformError, match="ollama-url"):
        enable(h, ollama_mode=None, ollama_url=url)
    assert h.runner.names() == []


def test_auto_prefers_ollama_on_this_computer(harness, monkeypatch):
    h = harness
    h.addresses, h.served = ["192.168.5.2"], [MODEL]
    monkeypatch.setattr(ollama, "probe", lambda origin, opener=None, budget=10.0: {"version": "0.34.4", "models": []})
    assert enable(h, ollama_mode="auto")["mode"] == "host"


def test_auto_falls_back_to_a_container(harness):
    assert enable(harness, ollama_mode="auto")["mode"] == "container"


def test_status_reports_unknown_when_the_platform_is_stopped(harness, monkeypatch):
    h = harness
    assert platform_ai.status(h.directory)["stage"] == "not_enabled"
    enable(h)
    h.runner.answers["ai-service-state"] = platform.PlatformError("Stage ai-service-state failed.")
    monkeypatch.setattr(platform, "_probe", lambda url, issuer=None: False)
    value = platform_ai.status(h.directory)
    assert value["gateway"]["state"] == "unknown" and value["worker"]["presence"] == "unknown"
    assert value["ollama"]["state"] == "unknown" and value["enabled"] is True


def test_status_reports_presence_and_a_policy_changed_outside_commands(harness):
    h = harness
    enable(h)
    value = platform_ai.status(h.directory)
    assert value["worker"]["presence"] == "recent" and value["gateway"]["state"] == "running"
    policy = h.directory / "ai-config" / "ai-policy.json"
    policy.chmod(0o644)
    policy.write_text(policy.read_text().replace('"served"', '["qwen3:4b"]'))
    assert any(
        "changed outside platform commands" in warning for warning in platform_ai.status(h.directory)["warnings"]
    )


def test_disable_removes_services_entries_and_api_settings(harness):
    h = harness
    enable(h)
    result = platform_ai.disable(h.directory, remove_model_data=True)
    assert result["disabled"] is True and result["api_restarted"] is True and result["model_data_removed"] is True
    removal = next(command for name, command in h.runner.calls if name == "ai-remove")
    assert {"ai-gateway", "agentic-worker", "ollama"} <= set(removal)
    assert "ai-volume-remove" in h.runner.names()
    assert not (h.directory / "compose.ai.json").exists()
    assert "private_origins" not in json.loads((h.directory / "platform.json").read_text())
    assert saved(h)["stage"] == "disabled" and "WEAVE_LUMI_GATEWAY" not in h.starts[-1]
    assert enable(h)["stage"] == "ready"


def test_models_switch_between_served_and_an_exact_list(harness):
    h = harness
    h.served = ["gemma3:270m"]
    enable(h)
    exact = platform_ai.models_approve(h.directory, provider="openai-chat", model="weave-missing:1b")
    assert exact["approved"] == ["weave-missing:1b"]
    policy = json.loads((h.directory / "ai-config" / "ai-policy.json").read_text())
    assert policy["endpoints"][0]["models"] == ["weave-missing:1b"]
    assert platform_ai.models_remove(h.directory, provider="openai-chat", model="weave-missing:1b")["approved"] == []
    assert platform_ai.models_approve(h.directory, provider="openai-chat", served=True)["approval"] == "served"
    remaining = platform_ai.models_remove(h.directory, provider="openai-chat", model=MODEL)["approved"]
    assert remaining == ["gemma3:270m"]
    with pytest.raises(platform.PlatformError, match="not approved"):
        platform_ai.models_remove(h.directory, provider="openai-chat", model="other:1b")
    with pytest.raises(platform.PlatformError, match="openai-chat"):
        platform_ai.models_approve(h.directory, provider="anthropic", model="x:1b")


def test_models_refresh_and_pull_update_served_models(harness):
    h = harness
    enable(h)
    h.served.append("gemma3:270m")
    assert [item["name"] for item in platform_ai.models_refresh(h.directory)["served"]] == [MODEL, "gemma3:270m"]
    pulled = platform_ai.models_pull(h.directory, "qwen3:4b", confirm=lambda text: True)
    assert pulled["pulled"] == "qwen3:4b" and h.pulls[-1][1] == "qwen3:4b"


def test_start_and_stop_reapply_and_stop_ai_services_only_when_ready(harness):
    h = harness
    state = platform._load(h.directory)
    platform_ai.start_services(state, lambda message: None)
    platform_ai.stop_services(state)
    assert h.runner.names() == []
    enable(h)
    state = platform._load(h.directory)
    platform_ai.start_services(state, lambda message: None)
    platform_ai.stop_services(state)
    assert h.runner.names()[-2:] == ["ai-services", "ai-stop"]
    assert str(h.directory / "compose.ai.json") in platform._compose(state)
    path = h.directory / "compose.ai.json"
    path.write_text(path.read_text().replace("65532:65532", "0:0"))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform._compose(state)


def meanwhile(monkeypatch, change):
    """Run ``change`` as if another command finished just before this one took the installation lock."""
    original = platform._lock

    @contextmanager
    def lock(directory):
        change()
        with original(directory):
            yield

    monkeypatch.setattr(platform, "_lock", lock)


def test_entries_come_from_the_installation_with_credentials_by_purpose_and_match_the_policy(harness):
    h = harness
    enable(h)
    receipt = saved(h)
    stored = json.loads((h.directory / "platform.json").read_text())["private_origins"]["ai"]["entries"]
    assert stored == platform_origins.entry_records(platform_ai_files.ai_entries(receipt))
    assert {(entry["origin"], tuple(entry["networks"]), entry["credentials"]) for entry in stored} == {
        ("http://ollama:11434", (SUBNET,), "none"),
        ("http://127.0.0.1:8090", ("127.0.0.1/32",), "loopback"),
        ("http://127.0.0.1:8080", ("127.0.0.1/32",), "loopback"),
        ("http://127.0.0.1:8000", ("127.0.0.1/32",), "loopback"),
    }
    (endpoint,) = json.loads((h.directory / "ai-config" / "ai-policy.json").read_text())["endpoints"]
    assert endpoint["url"] == receipt["endpoint"] == "http://ollama:11434/v1" and endpoint["credential"] == "none"


def test_the_api_container_mounts_ai_settings_only_while_enabled(harness):
    h = harness
    enable(h)

    def api():
        platform_docker._configuration(platform._load(h.directory), {})
        return json.loads((h.directory / "compose.api.json").read_text())["services"]["api"]

    targets = {volume["target"] for volume in api()["volumes"]}
    assert {"/run/weave-ai/config", "/run/weave-ai/secrets/gateway-token", platform_origins.CONTAINER_PATH} <= targets
    platform_ai.disable(h.directory)
    assert "volumes" not in api()
    settings = (h.directory / "api-container.env").read_text()
    assert "WEAVE_PRIVATE_ORIGINS_FILE" not in settings and "WEAVE_LUMI_GATEWAY" not in settings
    assert "WEAVE_PRIVATE_ORIGINS_FILE" not in h.starts[-1]


def test_enable_reads_the_installation_under_its_lock(harness, monkeypatch):
    h = harness
    enable(h)

    def disabled():
        state = platform._load(h.directory)
        platform_ai_files.remove_compose(state)
        platform_origins.remove_ai_entries(state)
        value = saved(h)
        value.pop("compose")
        value.update(stage="disabled", settings=False)
        platform._write(h.directory / "ai.json", value, replace=True)

    asked = []
    meanwhile(monkeypatch, disabled)
    assert enable(h, confirm=lambda text: asked.append(text) or True)["stage"] == "ready"
    assert any("private origins" in text for text in asked)
    assert "ai" in json.loads((h.directory / "platform.json").read_text())["private_origins"]


def test_disable_reads_the_installation_under_its_lock(harness, monkeypatch):
    h = harness
    enable(h)
    entries = platform_ai_files.ai_entries(saved(h))
    platform_origins.remove_ai_entries(platform._load(h.directory))
    meanwhile(monkeypatch, lambda: platform_origins.set_ai_entries(platform._load(h.directory), entries))
    assert platform_ai.disable(h.directory)["disabled"] is True
    assert "private_origins" not in json.loads((h.directory / "platform.json").read_text())


def test_a_smaller_measured_context_bounds_the_policy_with_a_status_warning(harness):
    h = harness

    def small(command):
        model = ollama.ServedModel(name=MODEL, size_bytes=1, context_tokens=4096, tools="yes").model_dump(mode="json")
        return (ollama.MARKER + json.dumps({"addresses": h.addresses, "version": "0.12.3", "models": [model]})).encode()

    h.runner.answers["ai-probe"] = small
    enable(h)
    (endpoint,) = json.loads((h.directory / "ai-config" / "ai-policy.json").read_text())["endpoints"]
    assert saved(h)["context_tokens"] == endpoint["contextTokens"] == 4096
    assert any("4,096 tokens" in warning for warning in platform_ai.status(h.directory)["warnings"])


def test_a_receipt_without_its_release_is_refused_clearly(harness):
    h = harness
    enable(h)
    value = saved(h)
    del value["release_id"]
    platform._write(h.directory / "ai.json", value, replace=True)
    with pytest.raises(platform.PlatformError, match="ai.json"):
        platform._compose(platform._load(h.directory))


def test_a_rerun_that_changes_the_entries_keeps_the_services_configuration_current(harness, monkeypatch):
    h = harness
    enable(h)
    network = json.loads(h.runner.answers["ai-network"])
    network[0]["IPAM"]["Config"] = [{"Subnet": "10.246.22.0/24"}]
    h.runner.answers["ai-network"] = json.dumps(network).encode()
    h.addresses = ["10.246.22.9"]
    # The real start runs Compose with the AI files, as start and stop do.
    monkeypatch.setattr(platform_docker, "start", lambda state, notice: platform._compose(state))
    result = enable(h)
    assert "origins" in result["changed"] and saved(h)["networks"] == ["10.246.22.0/24"]
    assert str(h.directory / "compose.ai.json") in platform._compose(platform._load(h.directory))


@pytest.mark.parametrize("damage", ["policy-directory", "shared-identity"])
def test_file_failures_read_as_platform_errors(harness, damage):
    h = harness
    if damage == "policy-directory":
        (h.directory / "ai-config" / "ai-policy.json").mkdir(parents=True)
    else:
        (h.directory / "identity.env").chmod(0o644)
    with pytest.raises(platform.PlatformError, match="rerun weave platform ai enable"):
        enable(h)


@pytest.mark.parametrize(
    "code,advice",
    [
        ("WV-AI-RATE-LIMITED", "wait a minute"),
        ("WV-LUMI-CAPACITY", "wait a minute"),
        ("WV-AI-GATEWAY-TIMEOUT", "status"),
    ],
)
def test_a_busy_connection_test_says_what_to_do(harness, monkeypatch, code, advice):
    h = harness

    async def busy(client, revision_id, model):
        raise WeaveError(Problem(status=429, code=code, message="Busy"))

    monkeypatch.setattr(platform_ai.setup, "test_connection", busy)
    with pytest.raises(platform.PlatformError, match=advice) as refused:
        enable(h)
    assert code in str(refused.value) and "fix the cause" not in str(refused.value)
    assert saved(h)["stage"] == "online"


@pytest.mark.parametrize(
    "mode,reported,expected",
    [("host", "::ffff:192.168.5.2", "192.168.5.2/32"), ("url", "fd12::5%eth0", "fd12::5/128")],
)
def test_reported_addresses_become_plain_networks(harness, mode, reported, expected):
    h = harness
    h.addresses, h.served = [reported, expected.split("/")[0]], [MODEL]
    choice = (
        {"ollama_mode": "host"}
        if mode == "host"
        else {"ollama_mode": None, "ollama_url": "http://ollama.acceptance.test:11434"}
    )
    enable(h, **choice)
    assert saved(h)["networks"] == [expected]


@pytest.mark.parametrize(
    "reported",
    ["169.254.169.254", "::ffff:169.254.169.254", "100.100.100.200", "127.0.0.1", "::ffff:127.0.0.1", "8.8.8.8"],
)
def test_url_mode_refuses_loopback_public_and_always_refused_addresses(harness, reported):
    h = harness
    h.addresses, h.served = [reported], [MODEL]
    with pytest.raises(platform.PlatformError, match="private, non-loopback"):
        enable(h, ollama_mode=None, ollama_url="http://ollama.acceptance.test:11434")
    assert not (h.directory / "ai.json").exists()


def test_container_mode_needs_ollama_inside_the_platform_network(harness):
    h = harness
    h.addresses = ["10.246.99.9"]
    with pytest.raises(platform.PlatformError, match="outside the platform network"):
        enable(h)
    assert h.pulls == [] and not (h.directory / "ai-config" / "ai-policy.json").exists()


def test_the_probe_runs_read_only_in_the_server_image_with_a_hard_timeout(harness, monkeypatch):
    h = harness
    timeouts = []

    def recording(state, name, command, **kwargs):
        timeouts.append((name, kwargs.get("timeout")))
        return h.runner(state, name, command, **kwargs)

    monkeypatch.setattr(platform, "_run", recording)
    h.addresses, h.served = ["192.168.5.2"], [MODEL]
    enable(h, ollama_mode="host")
    probe = next(command for name, command in h.runner.calls if name == "ai-probe")
    assert ("ai-probe", 90) in timeouts and "--read-only" in probe and probe[probe.index("--cap-drop") + 1] == "ALL"
    assert probe[-6:] == [
        SERVER_IMAGE,
        "python",
        "-I",
        "-m",
        "firefly_weave.ollama",
        "http://host.docker.internal:11434",
    ]


def test_auto_keeps_a_saved_ollama_url(harness):
    h = harness
    h.addresses, h.served = ["10.246.21.50"], [MODEL]
    enable(h, ollama_mode=None, ollama_url="http://ollama.acceptance.test:11434")
    again = enable(h, ollama_mode="auto", confirm=platform_ai.refuse_confirmation)
    assert again["mode"] == "url" and again["changed"] == []
    assert again["endpoint"] == "http://ollama.acceptance.test:11434/v1"


def test_disable_refuses_a_docker_context_that_points_elsewhere(harness, monkeypatch):
    h = harness
    enable(h)
    calls = len(h.runner.calls)
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///other.sock", "other-engine"))
    with pytest.raises(platform.PlatformError, match="different engine"):
        platform_ai.disable(h.directory, remove_model_data=True)
    assert len(h.runner.calls) == calls and saved(h)["stage"] == "ready"


def test_a_disable_stopped_before_the_entries_leaves_platform_commands_working(harness, monkeypatch):
    h = harness
    enable(h)
    original = platform_origins.remove_ai_entries

    def interrupted(state):
        raise platform.PlatformError("The private-origin file changed outside platform commands.")

    monkeypatch.setattr(platform_origins, "remove_ai_entries", interrupted)
    with pytest.raises(platform.PlatformError):
        platform_ai.disable(h.directory)
    assert saved(h)["stage"] == "disabled"
    assert str(h.directory / "compose.ai.json") not in platform._compose(platform._load(h.directory))
    monkeypatch.setattr(platform_origins, "remove_ai_entries", original)
    assert platform_ai.disable(h.directory)["disabled"] is True
    assert "private_origins" not in json.loads((h.directory / "platform.json").read_text())


def test_a_platform_network_broader_than_its_own_subnet_is_refused_clearly(harness):
    h = harness
    network = json.loads(h.runner.answers["ai-network"])
    network[0]["IPAM"]["Config"] = [{"Subnet": "172.16.0.0/12"}]
    h.runner.answers["ai-network"] = json.dumps(network).encode()
    with pytest.raises(platform.PlatformError, match="/16 or narrower"):
        enable(h)
    assert not (h.directory / "ai.json").exists() and "ai-ollama" not in h.runner.names()


def test_a_policy_the_services_would_refuse_reads_as_a_platform_error(harness):
    h = harness
    enable(h)
    value = saved(h)
    value["approval"] = [f"model-{index}:1b" for index in range(ai_policy.MAX_MODELS)]
    platform._write(h.directory / "ai.json", value, replace=True)
    with pytest.raises(platform.PlatformError, match="endpoints/0/models.*rerun weave platform ai models approve"):
        platform_ai.models_approve(h.directory, provider="openai-chat", model="one-more:1b")


class Images:
    """Docker's images for the Agentic worker: built, inspected, removed, and needed by Compose to start it."""

    def __init__(self, h):
        self.present, self.next = {IMAGE}, IMAGE

        def build(command):
            with open(command[command.index("--iidfile") + 1], "w") as stream:
                stream.write(self.next)
            self.present.add(self.next)
            return b""

        def check(command):
            image = command[command.index("inspect") + 1]
            if image not in self.present:
                raise platform.PlatformError("Stage ai-image-check failed.")
            return image.encode()

        def services(command):
            worker = json.loads((h.directory / "compose.ai.json").read_text())["services"]["agentic-worker"]
            if worker["image"] not in self.present:
                raise platform.PlatformError("Stage ai-services failed.")
            return b""

        h.runner.answers.update(
            {
                "ai-image-build": build,
                "ai-image-inspect": lambda command: json.dumps(
                    [{"Id": command[-1], "Config": {"User": "65532:65532"}}]
                ).encode(),
                "ai-image-check": check,
                "ai-services": services,
            }
        )


def docker_start(h, monkeypatch):
    """Use the real Docker start, with its identity and build steps answered."""
    monkeypatch.setattr(platform_docker, "start", DOCKER_START)
    monkeypatch.setattr(platform_docker, "_build", lambda state, notice: None)
    monkeypatch.setattr(platform, "_wait_identity", lambda state: None)
    monkeypatch.setattr(platform, "_repair_login_client", lambda state, notice: None)
    monkeypatch.setattr(platform, "_execution_environment", lambda state, notice: {})
    h.runner.answers["identity-container"] = b"c" * 64


def policy(h):
    (endpoint,) = json.loads((h.directory / "ai-config" / "ai-policy.json").read_text())["endpoints"]
    return endpoint


@pytest.mark.parametrize("mode", ["host", "url"])
def test_a_rebuild_stopped_before_its_release_leaves_every_command_working(harness, monkeypatch, mode):
    h = harness
    docker_start(h, monkeypatch)
    images = Images(h)
    h.addresses, h.served = ["192.168.5.2"], [MODEL]
    choice = (
        {"ollama_mode": "host"}
        if mode == "host"
        else {"ollama_mode": None, "ollama_url": "http://ollama.acceptance.test:11434"}
    )
    enable(h, **choice)
    images.present.clear()
    images.next = REBUILT
    h.runner.answers["ai-catalog"] = platform.PlatformError("Stage ai-catalog failed.")
    with pytest.raises(platform.PlatformError, match="ai-catalog"):
        enable(h, **choice)
    assert saved(h)["image_id"] == REBUILT
    platform.start(h.directory)
    platform.stop(h.directory)
    h.runner.answers["ai-catalog"] = catalog_output()
    assert enable(h, **choice)["stage"] == "ready"
    platform.start(h.directory)
    assert platform_ai.disable(h.directory)["disabled"] is True


def test_disable_restores_the_services_file_from_the_receipt_before_removing(harness):
    h = harness
    enable(h)
    path = h.directory / "compose.ai.json"
    path.write_text(path.read_text().replace("65532:65532", "0:0"))
    assert platform_ai.disable(h.directory)["disabled"] is True
    removal = next(command for name, command in h.runner.calls if name == "ai-remove")
    assert "agentic-worker" in removal and not path.exists()


def test_enable_rebuilds_a_removed_worker_image_and_start_names_the_way_out(harness, monkeypatch):
    h = harness
    docker_start(h, monkeypatch)
    images = Images(h)
    enable(h)
    images.present.clear()
    images.next = REBUILT
    services = h.runner.names().count("ai-services")
    with pytest.raises(platform.PlatformError, match="Run weave platform ai enable"):
        platform.start(h.directory)
    assert h.runner.names().count("ai-services") == services
    result = enable(h)
    assert result["stage"] == "ready" and "image" in result["changed"] and saved(h)["image_id"] == REBUILT
    platform.start(h.directory)
    command = [command for name, command in h.runner.calls if name == "ai-services"][-1]
    assert command[command.index("--pull") + 1] == "never"


def test_platform_start_starts_ai_services_after_the_api_is_ready_and_stop_stops_them_first(harness, monkeypatch):
    h = harness
    docker_start(h, monkeypatch)
    enable(h)
    before = len(h.runner.calls)
    platform.start(h.directory)
    assert in_order(h.runner.names()[before:], ["dependencies-start", "api-start", "ai-image-check", "ai-services"])
    before = len(h.runner.calls)
    platform.stop(h.directory)
    assert in_order(h.runner.names()[before:], ["api-stop", "ai-stop", "dependencies-stop"])
    monkeypatch.setattr(platform, "_probe", lambda url, issuer=None: False)
    before = len(h.runner.calls)
    with pytest.raises(platform.PlatformError, match="not ready"):
        platform.start(h.directory)
    assert "ai-services" not in h.runner.names()[before:]


def test_the_context_bound_follows_the_approved_models_and_names_the_one_that_lowers_it(harness):
    h = harness
    h.served, h.contexts = [MODEL, "nomic-embed-text:v1.5"], {"nomic-embed-text:v1.5": 2048}
    enable(h)
    assert policy(h)["contextTokens"] == 2048 and saved(h)["context_tokens"] == 2048
    warnings = platform_ai.status(h.directory)["warnings"]
    assert any("nomic-embed-text:v1.5" in warning and "2,048" in warning for warning in warnings)
    platform_ai.models_approve(h.directory, provider="openai-chat", model=MODEL)
    assert policy(h)["contextTokens"] == 8192
    assert not any("nomic-embed-text" in warning for warning in platform_ai.status(h.directory)["warnings"])
    platform_ai.models_approve(h.directory, provider="openai-chat", model="nomic-embed-text:v1.5")
    assert policy(h)["contextTokens"] == 2048
    platform_ai.models_remove(h.directory, provider="openai-chat", model="nomic-embed-text:v1.5")
    assert policy(h)["contextTokens"] == 8192
    h.served.append("small:1b")
    h.contexts["small:1b"] = 4096
    platform_ai.models_approve(h.directory, provider="openai-chat", served=True)
    platform_ai.models_refresh(h.directory)
    assert policy(h)["contextTokens"] == 2048


def test_a_model_under_512_tokens_is_left_out_of_the_approval_with_a_warning(harness):
    h = harness
    h.served, h.contexts = [MODEL, "all-minilm:22m"], {"all-minilm:22m": 256}
    assert enable(h)["stage"] == "ready"
    assert policy(h)["models"] == [MODEL] and policy(h)["contextTokens"] == 8192
    assert any("all-minilm:22m" in warning for warning in platform_ai.status(h.directory)["warnings"])
    exact = platform_ai.models_approve(h.directory, provider="openai-chat", model="all-minilm:22m")
    assert exact["approved"] == [] and policy(h)["models"] == []
    assert any("all-minilm:22m" in warning for warning in exact["warnings"])


def test_the_connection_test_uses_an_approved_chat_model(harness):
    h = harness
    h.served, h.tools = ["all-minilm:22m", MODEL], {"all-minilm:22m": "no"}
    enable(h, models=[])
    assert ("test", MODEL) in h.server.calls and ("test", "all-minilm:22m") not in h.server.calls
    h.served.insert(0, "aaa:1b")
    platform_ai.models_refresh(h.directory)
    platform_ai.models_approve(h.directory, provider="openai-chat", model=MODEL)
    enable(h, models=[], verify=True)
    assert h.server.calls[-2:] == [("test", MODEL), ("smoke", MODEL)]


@pytest.mark.parametrize("command", ["status", "refresh", "pull"])
def test_status_and_model_reads_refuse_a_docker_context_that_points_elsewhere(harness, monkeypatch, command):
    h = harness
    enable(h)
    calls = len(h.runner.calls)
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///other.sock", "other-engine"))
    run = {
        "status": lambda: platform_ai.status(h.directory),
        "refresh": lambda: platform_ai.models_refresh(h.directory),
        "pull": lambda: platform_ai.models_pull(h.directory, "qwen3:4b", confirm=lambda text: True),
    }[command]
    with pytest.raises(platform.PlatformError, match="different engine"):
        run()
    assert len(h.runner.calls) == calls and len(h.pulls) == 1
