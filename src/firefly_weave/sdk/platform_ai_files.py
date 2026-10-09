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

"""Files that run AI on the local Docker platform: the AI policy, mounted settings and Compose services.

Everything is rendered from the installation state and the private AI receipt (``ai.json``),
so each command can compare a file on disk with what it should be and refuse one changed
outside platform commands. Secret values live only in 0444 copies inside 0755 directories of
the 0700 installation directory: the containers run as UID 65532 and must read them, and a
non-root CLI cannot change a file's owner.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from firefly_weave import ai_policy, private_origins
from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_origins
from firefly_weave.sdk.deployment import DeploymentError, read_file, real_path

CONFIG_DIRECTORY = "ai-config"
SECRETS_DIRECTORY = "ai-secrets"
POLICY = "ai-policy.json"
OAUTH = "worker-oauth.json"
GATEWAY_TOKEN = "gateway-token"
WORKER_SECRET = "worker-client-secret"
COMPOSE = "compose.ai.json"
HOST_OVERRIDE = "compose.ai-host.json"
CONTAINER_CONFIG = "/run/weave-ai/config"
CONTAINER_SECRETS = "/run/weave-ai/secrets"
GATEWAY_ORIGIN = "http://127.0.0.1:8090"
GATEWAY_ENDPOINT = GATEWAY_ORIGIN + "/v1/lumi"
KEYCLOAK_ORIGIN = "http://127.0.0.1:8080"
TOKEN_ENDPOINT = KEYCLOAK_ORIGIN + "/realms/weave/protocol/openid-connect/token"
API_ORIGIN = "http://127.0.0.1:8000"
LOOPBACK = ("127.0.0.1/32",)
CONNECTION = "ollama-local"
OLLAMA_IMAGE = "ollama/ollama:0.12.3@sha256:c622a7adec67cf5bd7fe1802b7e26aa583a955a54e91d132889301f50c3e0bd0"
OLLAMA_ENVIRONMENT = {
    "OLLAMA_CONTEXT_LENGTH": "8192",
    "OLLAMA_HOST": "0.0.0.0:11434",
    "OLLAMA_KEEP_ALIVE": "30m",
    "OLLAMA_NUM_PARALLEL": "1",
}
LABELS = {"container": "Ollama (Weave-managed)", "host": "Ollama on this computer", "url": "Ollama"}
MAX_OUTPUT_TOKENS = 4096
SERVICES = ("ai-gateway", "agentic-worker")
_HARDENED: dict[str, Any] = {
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "user": "65532:65532",
    "tmpfs": ["/tmp:size=16777216,mode=1777"],
    "restart": "unless-stopped",
}


def _shared(path: Path) -> Path:
    """A 0755 directory inside the private installation, mounted read-only into containers."""
    if path.exists():
        real_path(path)
    else:
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    return path


def _publish(path: Path, data: bytes) -> bool:
    """Atomically replace a 0444 container copy; True when its content changed."""
    if path.exists() and read_file(path, 65536) == data:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ai-", delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    temporary.chmod(0o444)
    temporary.replace(path)
    return True


def _private(path: Path, data: bytes) -> bool:
    """Atomically replace a 0600 file the CLI owns; True when its content changed."""
    if path.exists() and read_file(path, 65536, private=True) == data:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ai-", delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)
    return True


def _json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _bind(source: Path | str, target: str) -> dict[str, Any]:
    return {"type": "bind", "source": str(source), "target": target, "read_only": True}


def ai_entries(receipt: dict[str, Any]) -> list[private_origins.PrivateOrigin]:
    """The private-origin entries AI needs: the Ollama origin, the gateway hop and loopback worker sign-in."""
    entry = private_origins.PrivateOrigin
    return [
        entry(
            origin=receipt["ollama_origin"], purpose="model", networks=tuple(receipt["networks"]), credentials="none"
        ),
        entry(origin=GATEWAY_ORIGIN, purpose="model", networks=LOOPBACK, credentials="loopback"),
        entry(origin=KEYCLOAK_ORIGIN, purpose="worker-auth", networks=LOOPBACK, credentials="loopback"),
        entry(origin=API_ORIGIN, purpose="platform-api", networks=LOOPBACK, credentials="loopback"),
    ]


def policy_document(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": CONNECTION,
        "label": LABELS[receipt["mode"]],
        "url": receipt["endpoint"],
        "providers": ["openai-chat"],
        "compat": "ollama",
        "credential": "none",
        "models": receipt["approval"],
        "contextTokens": receipt["context_tokens"],
        "structuredOutput": "native",
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
    }


def write_policy(state: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Write the AI policy the worker, the gateway and the API read; they reload it when it changes."""
    data = ai_policy.render([policy_document(receipt)])
    origins = private_origins.PrivateOrigins(platform=private_origins.PLATFORM).with_entries(ai_entries(receipt))
    # Never write a policy the services would refuse.
    ai_policy.parse(data, origins)
    changed = _publish(_shared(Path(state["directory"]) / CONFIG_DIRECTORY) / POLICY, data)
    receipt["policy_sha256"] = hashlib.sha256(data).hexdigest()
    return changed


def verify_policy(state: dict[str, Any], receipt: dict[str, Any]) -> None:
    try:
        data = read_file(Path(state["directory"]) / CONFIG_DIRECTORY / POLICY, ai_policy.MAX_FILE_BYTES)
    except (OSError, DeploymentError):
        raise local.PlatformError("The AI policy file is missing; run weave platform ai enable again.") from None
    if hashlib.sha256(data).hexdigest() != receipt.get("policy_sha256"):
        raise local.PlatformError(
            "The AI policy file changed outside platform commands. Run weave platform ai enable to restore it, "
            "or change approvals with weave platform ai models."
        )


def write_settings(state: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """The gateway token, the worker's client secret copy and its sign-in settings; True when any changed."""
    directory = Path(state["directory"])
    # Read the worker's secret before writing anything, so a refusal leaves the installation as it was.
    worker = local._env_file(directory / "identity.env").get("WEAVE_WORKER_SECRET")
    if not worker:
        raise local.PlatformError("The local worker client secret is missing from identity.env; nothing was changed.")
    stored = _shared(directory / SECRETS_DIRECTORY)
    config = _shared(directory / CONFIG_DIRECTORY)
    changed = False
    token = stored / GATEWAY_TOKEN
    if not token.exists():
        changed = _publish(token, (secrets.token_urlsafe(32) + "\n").encode())
    changed = _publish(stored / WORKER_SECRET, worker.encode()) or changed
    oauth = {
        "token_endpoint": TOKEN_ENDPOINT,
        "client_id": "weave-worker",
        "scope": "basic",
        "client_secret_file": CONTAINER_SECRETS + "/" + WORKER_SECRET,
    }
    changed = _publish(config / OAUTH, _json(oauth)) or changed
    receipt["settings"] = True
    return changed


def api_settings(state: dict[str, Any], receipt: dict[str, Any] | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The API's AI environment and read-only mounts, once the settings stage has run."""
    if receipt is None or not receipt.get("settings") or receipt.get("stage") == "disabled":
        return {}, []
    directory = Path(state["directory"])
    gateway = {
        "endpoint": GATEWAY_ENDPOINT,
        "token_file": CONTAINER_SECRETS + "/" + GATEWAY_TOKEN,
        "max_concurrency": 4,
    }
    environment = {
        "WEAVE_AI_POLICY_FILE": CONTAINER_CONFIG + "/" + POLICY,
        "WEAVE_LUMI_GATEWAY": json.dumps(gateway, separators=(",", ":")),
    }
    mounts = [
        _bind(directory / CONFIG_DIRECTORY, CONTAINER_CONFIG),
        _bind(directory / SECRETS_DIRECTORY / GATEWAY_TOKEN, CONTAINER_SECRETS + "/" + GATEWAY_TOKEN),
    ]
    return environment, mounts


def service_names(receipt: dict[str, Any]) -> list[str]:
    names = list(SERVICES) if receipt.get("compose") == "all" else []
    return names + (["ollama"] if receipt["mode"] == "container" else [])


def compose_document(state: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    directory = Path(state["directory"])
    services: dict[str, Any] = {}
    if receipt["mode"] == "container":
        services["ollama"] = {
            "image": OLLAMA_IMAGE,
            "restart": "unless-stopped",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "environment": dict(OLLAMA_ENVIRONMENT),
            "ports": [f"127.0.0.1:{receipt['ollama_port']}:11434"],
            "volumes": [{"type": "volume", "source": "ollama-models", "target": "/root/.ollama"}],
            "healthcheck": {"test": ["CMD", "ollama", "list"], "interval": "5s", "timeout": "5s", "retries": 60},
        }
    if receipt.get("compose") == "all":
        shared = [
            _bind(directory / CONFIG_DIRECTORY, CONTAINER_CONFIG),
            _bind(platform_origins.container_copy(state), platform_origins.CONTAINER_PATH),
        ]
        common = {
            **_HARDENED,
            "image": receipt["image_id"],
            "network_mode": "service:keycloak",
            "depends_on": {"keycloak": {"condition": "service_started"}},
        }
        services["ai-gateway"] = {
            **common,
            "entrypoint": ["weave-lumi-gateway"],
            "environment": {
                "WEAVE_LUMI_POLICY_FILE": CONTAINER_CONFIG + "/" + POLICY,
                "WEAVE_PRIVATE_ORIGINS_FILE": platform_origins.CONTAINER_PATH,
                "WEAVE_LUMI_GATEWAY_TOKEN_FILE": CONTAINER_SECRETS + "/" + GATEWAY_TOKEN,
                "WEAVE_LUMI_GATEWAY_HOST": "127.0.0.1",
                "WEAVE_LUMI_GATEWAY_PORT": "8090",
            },
            "volumes": [
                *shared,
                _bind(directory / SECRETS_DIRECTORY / GATEWAY_TOKEN, CONTAINER_SECRETS + "/" + GATEWAY_TOKEN),
            ],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import socket; socket.create_connection(('127.0.0.1', 8090), 2).close()",
                ],
                "interval": "2s",
                "timeout": "3s",
                "retries": 60,
            },
        }
        scope = receipt["scope"]
        services["agentic-worker"] = {
            **common,
            "entrypoint": ["weave-agentic-worker"],
            "environment": {
                "WEAVE_API_URL": API_ORIGIN,
                "WEAVE_ENVIRONMENT_URL": (
                    f"/api/v1/tenants/{scope['tenant_id']}/projects/{scope['project_id']}"
                    f"/environments/{scope['environment_id']}"
                ),
                "WEAVE_WORKER_RELEASE_ID": receipt["release_id"],
                "WEAVE_WORKER_OAUTH_CONFIG_FILE": CONTAINER_CONFIG + "/" + OAUTH,
                "WEAVE_AGENTIC_POLICY_FILE": CONTAINER_CONFIG + "/" + POLICY,
                "WEAVE_PRIVATE_ORIGINS_FILE": platform_origins.CONTAINER_PATH,
                "WEAVE_AGENTIC_CAPACITY": "1",
            },
            "volumes": [
                *shared,
                _bind(directory / SECRETS_DIRECTORY / WORKER_SECRET, CONTAINER_SECRETS + "/" + WORKER_SECRET),
            ],
        }
    document: dict[str, Any] = {"services": services}
    if receipt["mode"] == "container":
        document["volumes"] = {"ollama-models": {"name": f"weave-local-{state['id']}-ollama"}}
    return document


def _host_override() -> dict[str, Any]:
    # Services that share Keycloak's network namespace also share its /etc/hosts.
    return {"services": {"keycloak": {"extra_hosts": ["host.docker.internal:host-gateway"]}}}


def write_compose(state: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Write compose.ai.json (and the host-mode Keycloak override); True when either changed."""
    directory = Path(state["directory"])
    changed = _private(directory / COMPOSE, _json(compose_document(state, receipt)))
    if receipt["mode"] == "host":
        changed = _private(directory / HOST_OVERRIDE, _json(_host_override())) or changed
    return changed


def verified_compose(state: dict[str, Any], receipt: dict[str, Any]) -> list[Path]:
    """The AI Compose files, only when they are exactly what platform commands wrote."""
    directory = Path(state["directory"])
    expected = [(directory / COMPOSE, _json(compose_document(state, receipt)))]
    if receipt["mode"] == "host":
        expected.append((directory / HOST_OVERRIDE, _json(_host_override())))
    for path, data in expected:
        try:
            if read_file(path, 65536, private=True) != data:
                raise ValueError("Changed")
        except (OSError, ValueError):
            raise local.PlatformError(
                "The AI services configuration changed outside platform commands; no services were changed. "
                "Run weave platform ai enable to restore it."
            ) from None
    return [path for path, _ in expected]


def remove_compose(state: dict[str, Any]) -> None:
    directory = Path(state["directory"])
    for name in (COMPOSE, HOST_OVERRIDE):
        (directory / name).unlink(missing_ok=True)
