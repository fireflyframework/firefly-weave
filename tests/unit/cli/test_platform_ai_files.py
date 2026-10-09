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

"""The AI policy, mounted settings and Compose services are rendered from the receipt and checked on reuse."""

import json
import stat

import pytest
from ai_platform_support import OWNER, SCOPE, SUBNET, WORKER_SECRET, owned_fixture  # noqa: F401

from firefly_weave import ai_policy
from firefly_weave import private_origins as po
from firefly_weave.sdk import platform, platform_origins
from firefly_weave.sdk import platform_ai_files as files

IMAGE = "sha256:" + "e" * 64
RELEASE = "44444444-4444-4444-8444-444444444444"


def receipt(**changes):
    value = {
        "format": "weave/local-ai-v1",
        "stage": "services",
        "scope": SCOPE,
        "mode": "container",
        "ollama_origin": "http://ollama:11434",
        "endpoint": "http://ollama:11434/v1",
        "networks": [SUBNET],
        "approval": "served",
        "context_tokens": 8192,
        "ollama_port": 55299,
        "compose": "all",
        "image_id": IMAGE,
        "release_id": RELEASE,
    }
    value.update(changes)
    return value


def prepared(directory, value):
    state = platform._load(directory)
    platform_origins.set_ai_entries(state, files.ai_entries(value))
    return platform._load(directory)


def test_the_policy_is_one_ollama_endpoint_the_services_accept(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    assert files.write_policy(state, value) is True
    path = directory / "ai-config" / "ai-policy.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert stat.S_IMODE((directory / "ai-config").stat().st_mode) == 0o755
    origins = po.parse((directory / "private-origins.json").read_bytes())
    entry = ai_policy.parse(path.read_bytes(), origins).entry_for("http://ollama:11434/v1")
    assert entry.served and entry.compat == "ollama" and entry.credential == "none" and entry.context_tokens == 8192
    assert files.write_policy(state, value) is False
    files.verify_policy(state, value)
    path.chmod(0o644)
    path.write_text(path.read_text().replace("8192", "4096"))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        files.verify_policy(state, value)


def test_settings_copy_secrets_once_and_keep_them_out_of_the_receipt(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    assert files.write_settings(state, value) is True
    token = (directory / "ai-secrets" / "gateway-token").read_text().strip()
    assert len(token) >= 40 and value["settings"] is True
    assert (directory / "ai-secrets" / "worker-client-secret").read_text() == WORKER_SECRET
    for name in ("gateway-token", "worker-client-secret"):
        assert stat.S_IMODE((directory / "ai-secrets" / name).stat().st_mode) == 0o444
    oauth = json.loads((directory / "ai-config" / "worker-oauth.json").read_text())
    assert oauth == {
        "client_id": "weave-worker",
        "client_secret_file": "/run/weave-ai/secrets/worker-client-secret",
        "scope": "basic",
        "token_endpoint": "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/token",
    }
    assert files.write_settings(state, value) is False
    assert (directory / "ai-secrets" / "gateway-token").read_text().strip() == token
    assert token not in json.dumps(value) and WORKER_SECRET not in json.dumps(value)


def test_settings_change_nothing_without_the_worker_client_secret(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    identity = directory / "identity.env"
    lines = identity.read_text().splitlines(keepends=True)
    identity.write_text("".join(line for line in lines if not line.startswith("WEAVE_WORKER_SECRET=")))
    with pytest.raises(platform.PlatformError, match="nothing was changed"):
        files.write_settings(state, value)
    assert not (directory / "ai-secrets").exists() and not (directory / "ai-config").exists()
    assert "settings" not in value


def test_the_api_gets_the_gateway_and_policy_only_after_the_settings_stage(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    assert files.api_settings(state, None) == ({}, [])
    assert files.api_settings(state, value) == ({}, [])
    files.write_settings(state, value)
    environment, mounts = files.api_settings(state, value)
    assert environment["WEAVE_AI_POLICY_FILE"] == "/run/weave-ai/config/ai-policy.json"
    assert json.loads(environment["WEAVE_LUMI_GATEWAY"]) == {
        "endpoint": "http://127.0.0.1:8090/v1/lumi",
        "token_file": "/run/weave-ai/secrets/gateway-token",
        "max_concurrency": 4,
    }
    assert {mount["target"] for mount in mounts} == {"/run/weave-ai/config", "/run/weave-ai/secrets/gateway-token"}
    assert all(mount["read_only"] for mount in mounts)
    assert files.api_settings(state, {**value, "stage": "disabled"}) == ({}, [])


def test_compose_runs_hardened_services_in_keycloaks_namespace_without_secret_values(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    files.write_settings(state, value)
    assert files.write_compose(state, value) is True
    raw = (directory / "compose.ai.json").read_text()
    services = json.loads(raw)["services"]
    assert set(services) == {"ollama", "ai-gateway", "agentic-worker"}
    for name in ("ai-gateway", "agentic-worker"):
        service = services[name]
        assert service["image"] == IMAGE and service["network_mode"] == "service:keycloak"
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"] and service["user"] == "65532:65532"
    assert services["ai-gateway"]["environment"]["WEAVE_LUMI_GATEWAY_HOST"] == "127.0.0.1"
    worker = services["agentic-worker"]["environment"]
    assert worker["WEAVE_API_URL"] == "http://127.0.0.1:8000" and worker["WEAVE_WORKER_RELEASE_ID"] == RELEASE
    assert worker["WEAVE_ENVIRONMENT_URL"].endswith("/environments/" + SCOPE["environment_id"])
    ollama = services["ollama"]
    assert ollama["image"] == files.OLLAMA_IMAGE and ollama["ports"] == ["127.0.0.1:55299:11434"]
    assert ollama["environment"]["OLLAMA_CONTEXT_LENGTH"] == "8192"
    assert json.loads(raw)["volumes"] == {"ollama-models": {"name": f"weave-local-{OWNER}-ollama"}}
    token = (directory / "ai-secrets" / "gateway-token").read_text().strip()
    assert WORKER_SECRET not in raw and token not in raw
    assert files.write_compose(state, value) is False
    assert files.verified_compose(state, value) == [directory / "compose.ai.json"]


def test_the_first_compose_phase_runs_only_ollama(owned):
    directory, _, _ = owned
    value = receipt(compose="ollama", image_id=None, release_id=None)
    state = prepared(directory, value)
    files.write_compose(state, value)
    assert set(json.loads((directory / "compose.ai.json").read_text())["services"]) == {"ollama"}
    assert files.service_names(value) == ["ollama"]
    assert files.service_names(receipt()) == ["ai-gateway", "agentic-worker", "ollama"]


def test_host_mode_teaches_keycloak_the_host_gateway_and_runs_no_ollama(owned):
    directory, _, _ = owned
    value = receipt(mode="host", ollama_origin="http://host.docker.internal:11434", networks=["192.168.5.2/32"])
    value.pop("ollama_port")
    state = prepared(directory, value)
    files.write_settings(state, value)
    files.write_compose(state, value)
    assert set(json.loads((directory / "compose.ai.json").read_text())["services"]) == set(files.SERVICES)
    override = json.loads((directory / "compose.ai-host.json").read_text())
    assert override == {"services": {"keycloak": {"extra_hosts": ["host.docker.internal:host-gateway"]}}}
    assert files.verified_compose(state, value) == [directory / "compose.ai.json", directory / "compose.ai-host.json"]


def test_a_changed_compose_file_is_never_used(owned):
    directory, _, _ = owned
    value = receipt()
    state = prepared(directory, value)
    files.write_settings(state, value)
    files.write_compose(state, value)
    path = directory / "compose.ai.json"
    path.write_text(path.read_text().replace("65532:65532", "0:0"))
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        files.verified_compose(state, value)
    files.remove_compose(state)
    assert not path.exists()
