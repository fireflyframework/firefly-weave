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

"""Detached API transport for an already guarded, owned local installation."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from firefly_weave.sdk import platform as local
from firefly_weave.sdk.deployment import read_file, real_path, strict_json


def _private_bytes(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".docker-", delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    temporary.replace(path)


def dependencies(state: dict[str, Any]) -> Path:
    path = Path(state["directory"]) / "compose.persistence.json"
    document = {"services": {name: {"restart": "unless-stopped"} for name in ("postgres", "keycloak", "keycloak-db")}}
    expected = (json.dumps(document, sort_keys=True) + "\n").encode()
    if path.exists():
        if read_file(path, 4096, private=True) != expected:
            raise local.PlatformError("The saved Docker lifecycle configuration has changed.")
    else:
        local._write(path, document)
        # Use the same canonical representation when checking subsequent commands.
        _private_bytes(path, expected)
    return path


def environment(state: dict[str, Any], execution: dict[str, str]) -> dict[str, str]:
    runtime = local._env_file(Path(state["directory"]) / "runtime.env")
    values = {"WEAVE_DOCS_ENABLED": "true", "WEAVE_DISPLAY_NAME": local._DISPLAY_NAME}
    for key, role in (("WEAVE_DATABASE_URL", "weave_runtime_"), ("WEAVE_SCHEDULER_DATABASE_URL", "weave_scheduler_")):
        parsed = urlsplit(runtime[key])
        if (
            parsed.scheme != "postgresql+asyncpg"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.port != state["ports"]["postgres"]
            or not (parsed.username or "").startswith(role)
            or not parsed.password
            or not parsed.path.startswith("/weave_b2_dev_")
            or parsed.query
            or parsed.fragment
        ):
            raise local.PlatformError("The saved runtime database is outside this installation.")
        authority = parsed.netloc.rsplit("@", 1)[0]
        values[key] = urlunsplit((parsed.scheme, authority + "@postgres:5432", parsed.path, "", ""))
    providers = json.loads(runtime["WEAVE_OIDC_PROVIDERS"])
    issuer = f"http://localhost:{state['ports']['keycloak']}/realms/weave"
    if (
        not isinstance(providers, list)
        or len(providers) != 1
        or providers[0].get("provider_id") != local._PROVIDER_ID
        or providers[0].get("issuer") != issuer
        or providers[0].get("jwks_uri") != issuer + "/protocol/openid-connect/certs"
        or providers[0].get("local_development") is not True
    ):
        raise local.PlatformError("The saved identity configuration is outside this installation.")
    providers[0]["jwks_uri"] = "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/certs"
    values["WEAVE_OIDC_PROVIDERS"] = json.dumps(providers, separators=(",", ":"))
    sign_in = local._client_sign_in(runtime)
    if sign_in is not None:
        values["WEAVE_CLIENT_SIGN_IN"] = sign_in
    allowed = {"WEAVE_SECRET_ROOT", "WEAVE_SECRET_GRANTS", "WEAVE_NATIVE_IMAGE_DIGEST", "WEAVE_NATIVE_EXECUTORS"}
    if set(execution) - allowed:
        raise local.PlatformError("Unexpected container execution configuration.")
    values.update(execution)
    if "WEAVE_SECRET_ROOT" in values:
        values["WEAVE_SECRET_ROOT"] = "/run/weave-secrets"
    from firefly_weave.sdk import platform_ai

    ai, _ = platform_ai.api_environment(state)
    if set(ai) - {"WEAVE_AI_POLICY_FILE", "WEAVE_LUMI_GATEWAY"}:
        raise local.PlatformError("Unexpected AI configuration for the API container.")
    values.update(ai)
    if state.get("private_origins"):
        from firefly_weave.sdk import platform_origins

        values["WEAVE_PRIVATE_ORIGINS_FILE"] = platform_origins.CONTAINER_PATH
    return values


def _image(state: dict[str, Any]) -> str:
    value = state.get("docker_image")
    if not isinstance(value, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
        raise local.PlatformError("The retained Docker image is missing; run platform up to build it.")
    actual = local._run(
        state,
        "image-check",
        ["docker", "--context", state["context"], "image", "inspect", value, "--format", "{{.Id}}"],
    )
    if actual.decode().strip() != value:
        raise local.PlatformError("The retained Docker image identity changed.")
    return value


def _build(state: dict[str, Any], notice: Callable[[str], None]) -> None:
    if state.get("docker_image") is not None:
        _image(state)
        return
    directory = Path(state["directory"])
    release = strict_json(read_file(directory / "release/release.json", 65536))
    context = directory / "release/images"
    if not release.get("complete"):
        raise local.PlatformError("The retained release is incomplete.")
    for name, digest in release["inputs"].items():
        if Path(name).name != name or hashlib.sha256(read_file(context / name, 64 * 1024 * 1024)).hexdigest() != digest:
            raise local.PlatformError("The retained Docker build inputs changed.")
    if strict_json(read_file(context / "release.json", 65536)) != release:
        raise local.PlatformError("The Docker build receipt differs from the retained release.")
    notice("Building the retained server image for this Docker platform")
    receipt = directory / "api-image.id"
    if receipt.exists():
        raise local.PlatformError("An earlier image build is incomplete; inspect api-image.id before recovery.")
    local._run(
        state,
        "image-build",
        [
            "docker",
            "--context",
            state["context"],
            "build",
            "--target",
            "server",
            "--iidfile",
            str(receipt),
            str(context),
        ],
        timeout=900,
    )
    value = read_file(receipt, 128).decode().strip()
    if re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
        raise local.PlatformError("Docker did not report an immutable image identity.")
    state["docker_image"] = value
    local._write(directory / "platform.json", state, replace=True)
    _image(state)


def build_identity(state: dict[str, Any]) -> dict[str, Any]:
    image = _image(state)
    value = local._parse_build(
        local._run(
            state,
            "image-build-identity",
            [
                "docker",
                "--context",
                state["context"],
                "run",
                "--rm",
                "--network",
                "none",
                image,
                "python",
                "-I",
                "-c",
                local._BUILD_PROBE,
            ],
        )
    )
    value["identity"] = image
    return value


def _configuration(state: dict[str, Any], execution: dict[str, str]) -> Path:
    directory = Path(state["directory"])
    values = environment(state, execution)
    if any("\n" in v or "\r" in v for v in values.values()):
        raise local.PlatformError("Container configuration contains unsupported line breaks.")
    env_file = directory / "api-container.env"
    _private_bytes(env_file, "".join(f"{k}={v}\n" for k, v in values.items()).encode())
    service: dict[str, Any] = {
        "image": state["docker_image"],
        "restart": "unless-stopped",
        "network_mode": "service:keycloak",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:size=67108864,mode=1777"],
        "env_file": [{"path": str(env_file), "format": "raw"}],
        "depends_on": {"postgres": {"condition": "service_healthy"}, "keycloak": {"condition": "service_started"}},
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)",
            ],
            "interval": "2s",
            "timeout": "3s",
            "retries": 60,
        },
    }
    if "WEAVE_SECRET_ROOT" in values:
        # Only selected provider values are mounted, never the installation or identity files.
        store = directory / "container-secrets"
        if store.exists():
            real_path(store)
        else:
            store.mkdir(mode=0o755)
        index_path = directory / "container-secrets.json"
        index = strict_json(read_file(index_path, 65536, private=True)) if index_path.exists() else {}
        mounts = []
        for handle in local._secret_handles(directory):
            value = read_file(directory / "secrets" / handle, local.SECRET_VALUE_LIMIT, private=True)
            digest = hashlib.sha256(value).hexdigest()
            previous = index.get(handle)
            if previous is not None:
                name = previous.get("file", "")
                if not isinstance(name, str) or re.fullmatch(r"[a-f0-9]{32}", name) is None:
                    raise local.PlatformError("The container secret receipt is invalid.")
                copied = read_file(store / name, local.SECRET_VALUE_LIMIT)
                if hashlib.sha256(copied).hexdigest() != previous.get("sha256"):
                    raise local.PlatformError("A retained container secret changed outside platform commands.")
            if previous is None or previous["sha256"] != digest:
                # A new opaque mount source makes Compose refresh this API on rotation, without
                # exposing a credential hash in container metadata or changing the live old mount.
                previous = {"file": uuid4().hex, "sha256": digest}
                destination = store / previous["file"]
                _private_bytes(destination, value)
                destination.chmod(0o444)
                index[handle] = previous
            destination = store / previous["file"]
            mounts.append(
                {
                    "type": "bind",
                    "source": str(destination),
                    "target": "/run/weave-secrets/" + handle,
                    "read_only": True,
                }
            )
        local._write(index_path, index, replace=index_path.exists())
        service["volumes"] = mounts
    if state.get("private_origins"):
        from firefly_weave.sdk import platform_origins

        # The API reads a read-only copy; the installation's own file stays private (0600).
        service.setdefault("volumes", []).append(
            {
                "type": "bind",
                "source": str(platform_origins.container_copy(state)),
                "target": platform_origins.CONTAINER_PATH,
                "read_only": True,
            }
        )
    from firefly_weave.sdk import platform_ai

    _, mounts = platform_ai.api_environment(state)
    if mounts:
        service.setdefault("volumes", []).extend(mounts)
    path = directory / "compose.api.json"
    local._write(path, {"services": {"api": service}}, replace=path.exists())
    return path


def _command(state: dict[str, Any]) -> list[str]:
    path = Path(state["directory"]) / "compose.api.json"
    read_file(path, 65536, private=True)
    return [*local._compose(state), "-f", str(path)]


def inspect(state: dict[str, Any]) -> dict[str, str]:
    # Query by Compose's exact ownership labels; never act on a name selected by external input.
    project = "weave-local-" + state["id"]
    docker = ["docker", "--context", state["context"]]
    ids = (
        local._run(
            state,
            "api-inventory",
            [
                *docker,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=api",
            ],
        )
        .decode()
        .split()
    )
    if not ids:
        return {"state": "absent"}
    if len(ids) != 1 or re.fullmatch(r"[a-f0-9]{12,64}", ids[0]) is None:
        raise local.PlatformError("The owned API container inventory is ambiguous.")
    records = strict_json(local._run(state, "api-inspect", [*docker, "inspect", ids[0]]))
    record = records[0]
    labels = record["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "api"
        or record["Image"] != state.get("docker_image")
    ):
        raise local.PlatformError("The owned API container identity differs; no container was changed.")
    return {
        "state": record["State"]["Status"],
        "id": record["Id"],
        "network_mode": record["HostConfig"]["NetworkMode"],
    }


def start(state: dict[str, Any], notice: Callable[[str], None]) -> None:
    _build(state, notice)
    existing = inspect(state)
    local._run(
        state,
        "dependencies-start",
        [
            *local._compose(state),
            "up",
            "--detach",
            "--no-recreate",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
            "keycloak",
        ],
        env=local._compose_env(state),
    )
    local._wait_identity(state)
    local._repair_login_client(state, notice)
    execution = local._execution_environment(state, notice)
    _configuration(state, execution)
    local._run(state, "api-compose-check", [*_command(state), "config", "--quiet"], env=local._compose_env(state))
    recreate: list[str] = []
    if existing["state"] != "absent":
        identity = (
            local._run(
                state,
                "identity-container",
                [*local._compose(state), "ps", "--quiet", "keycloak"],
                env=local._compose_env(state),
            )
            .decode()
            .strip()
        )
        if re.fullmatch(r"[a-f0-9]{64}", identity) is None:
            raise local.PlatformError("The owned Keycloak container inventory is incomplete.")
        if existing["network_mode"] != "container:" + identity:
            recreate = ["--force-recreate"]
    notice("Starting the owned Docker API; it will keep running after this command exits")
    local._run(
        state,
        "api-start",
        [
            *_command(state),
            "up",
            "--detach",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            *recreate,
            "api",
        ],
        env=local._compose_env(state),
    )
    if not local._probe(local._summary(state)["api_url"] + "/health/ready"):
        raise local.PlatformError(
            "The Docker API is not ready. Inspect platform logs; retained services were not removed."
        )
    from firefly_weave.sdk import platform_ai

    platform_ai.start_services(state, notice)


def stop(state: dict[str, Any]) -> None:
    container = inspect(state)
    if container["state"] != "absent":
        local._run(
            state, "api-stop", ["docker", "--context", state["context"], "stop", "--time", "30", container["id"]]
        )


def logs(state: dict[str, Any], lines: int) -> str:
    container = inspect(state)
    if container["state"] == "absent":
        raise local.PlatformError("The owned API container has not been created yet; inspect private setup logs.")
    return local._run(
        state, "api-logs", ["docker", "--context", state["context"], "logs", "--tail", str(lines), container["id"]]
    ).decode(errors="replace")
