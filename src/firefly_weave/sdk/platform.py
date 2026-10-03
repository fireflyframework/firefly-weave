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

"""Owned local development lifecycle, using the checkout's existing setup helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from firefly_weave import __version__
from firefly_weave.sdk.deployment import DeploymentError, read_file, real_path, run_command, strict_json

if TYPE_CHECKING:
    import httpx

_SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "compose.yaml",
    "compose.identity.yaml",
    "infra/postgres/init.sql",
    "scripts/setup-local.py",
    "scripts/setup-identity.py",
    "scripts/setup-runtime.py",
    "scripts/prepare_release.py",
    "examples/first_run.py",
)
_FORMAT = "weave/local-platform-v1"
_INTEGRATIONS_FORMAT = "weave/local-integrations-v1"
_HTTP_CONNECTOR = "weave-http@2.0.0"
_HTTP_TASKS = ("weave-connector-http-read@2.0.0", "weave-connector-http-write@2.0.0")
_NATIVE_CAPACITY = 4
# Handles double as file names in the private store: lowercase keeps case-insensitive volumes unambiguous.
_SECRET_HANDLE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
# The server's mounted-file secret provider refuses larger values.
SECRET_VALUE_LIMIT = 65536
_SECRET_PENDING = ".pending-"
_BUILD_MARKER = "WEAVE-LOCAL-BUILD "
# Runs in the installed runtime, so the published manifest and build identity are the server's own.
_BUILD_PROBE = (
    "import json\n"
    "from firefly_weave.connectors.dispatcher import local_build_identity\n"
    "from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR as d\n"
    "print('WEAVE-LOCAL-BUILD ' + json.dumps({'identity': local_build_identity(), 'connector': d.manifest.value,"
    " 'capabilities': [c.model_dump(mode='json', by_alias=True) for c in d.capabilities],"
    " 'bindings': [b.model_dump(mode='json') for b in d.bindings]}, sort_keys=True, separators=(',', ':')))\n"
)
_PROVIDER_ID = "local-keycloak"
_LOGIN_CLIENT = "weave-cli"
# Public name the API publishes for this platform; sign-in settings carry no secrets.
_DISPLAY_NAME = "Local Weave platform"
# Development people get the smallest set that authors, publishes, activates, runs, inspects
# runs, and works on human tasks in the demo workspace; administrators choose others explicitly.
DEFAULT_PERSON_ROLES = ("developer", "deployer", "operator", "viewer", "task_participant")
PERSON_ROLES = (
    "file_reader",
    "file_manager",
    "lumi_user",
    "lumi_manager",
    "tenant_admin",
    "developer",
    "deployer",
    "operator",
    "viewer",
    "task_participant",
    "task_manager",
    "email_reader",
    "email_sender",
    "email_manager",
    "execution_manager",
)


class PlatformError(ValueError):
    """An actionable message containing no supplied credentials or subprocess output."""


def local_client_sign_in() -> str:
    """Return the `WEAVE_CLIENT_SIGN_IN` value for the owned Keycloak's public login client."""
    entry = {
        "provider_id": _PROVIDER_ID,
        "display_name": "Local Keycloak (development)",
        "client_id": _LOGIN_CLIENT,
        "scopes": ["openid"],
    }
    return json.dumps([entry], separators=(",", ":"))


def _client_sign_in(runtime: dict[str, str]) -> str | None:
    if "WEAVE_CLIENT_SIGN_IN" in runtime:
        return runtime["WEAVE_CLIENT_SIGN_IN"]
    # Platforms created before published sign-in settings derive them from the saved local provider.
    try:
        providers = json.loads(runtime.get("WEAVE_OIDC_PROVIDERS", "[]"))
    except ValueError:
        return None
    for provider in providers if isinstance(providers, list) else ():
        if isinstance(provider, dict) and provider.get("provider_id") == _PROVIDER_ID:
            clients = provider.get("clients")
            if isinstance(clients, dict) and clients.get(_LOGIN_CLIENT) == "human":
                return local_client_sign_in()
    return None


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WEAVE_", "DOCKER_", "COMPOSE_", "PYTHON", "PYFLY_"))
    }


def _private_directory(directory: Path) -> Path:
    directory = real_path(directory)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PlatformError("The installation directory must belong to you with permissions 0700.")
    return directory


def _write(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if replace:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-", delete=False) as stream:
            stream.write(data)
            temporary = Path(stream.name)
        temporary.replace(path)
    else:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
            stream.write(data)


def _env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in read_file(path, 65536, private=True).decode().splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, raw = line.partition("=")
        words = shlex.split(raw)
        if not separator or not re.fullmatch(r"WEAVE_[A-Z_]+", name) or len(words) != 1 or name in result:
            raise PlatformError("Private configuration is incomplete or malformed; retain it for recovery.")
        result[name] = words[0]
    return result


def _source(source: Path) -> tuple[Path, str]:
    source = real_path(source)
    metadata = tomllib.loads(read_file(source / "pyproject.toml", 1024 * 1024).decode())
    if metadata.get("project", {}).get("name") != "firefly-weave" or metadata["project"]["version"] != __version__:
        raise PlatformError("Use a Firefly Weave checkout matching this CLI version; select it with --source.")
    digest = hashlib.sha256()
    paths = set(_SOURCE_FILES)
    for relative in ("src", "infra/keycloak", "migrations", "docker", "examples/worker"):
        paths.update(
            str(path.relative_to(source))
            for path in (source / relative).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    paths.update(("Dockerfile", ".dockerignore", "LICENSE", "NOTICE", "scripts/image_identity.py"))
    for name in sorted(paths):
        digest.update(name.encode() + b"\0" + read_file(source / name, 16 * 1024 * 1024))
    return source, digest.hexdigest()


def _docker(context: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
        raise PlatformError("Select a named local Docker context.")
    env = _environment()
    data = strict_json(run_command(["docker", "context", "inspect", context], env=env))
    endpoint = data[0]["Endpoints"]["docker"]["Host"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise PlatformError("Local platform setup requires a Docker context using a local Unix socket.")
    engine = run_command(["docker", "--context", context, "info", "--format", "{{.ID}}"], env=env).decode().strip()
    if not engine:
        raise PlatformError("The selected Docker engine is unavailable.")
    version = run_command(["docker", "--context", context, "compose", "version", "--short"], env=env).decode().strip()
    match = re.match(r"v?(\d+)\.(\d+)", version)
    if match is None or (int(match[1]), int(match[2])) < (2, 30):
        raise PlatformError("Docker Compose 2.30 or later is required.")
    return endpoint, engine


def doctor(source: Path, context: str | None) -> dict[str, Any]:
    source, fingerprint = _source(source)
    run_command(["uv", "--version"], env=_environment())
    if context is None:
        context = run_command(["docker", "context", "show"], env=_environment()).decode().strip()
    endpoint, engine = _docker(context)
    return {
        "ok": True,
        "version": __version__,
        "source": str(source),
        "source_sha256": fingerprint,
        "context": context,
        "endpoint": endpoint,
        "engine": engine,
    }


def _validate_subnet(value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value, strict=True)
        private = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if (
            not isinstance(network, ipaddress.IPv4Network)
            or not 16 <= network.prefixlen <= 28
            or not any(network.subnet_of(block) for block in private)
        ):
            raise ValueError("Private IPv4 subnet required")
        return network
    except ValueError:
        raise PlatformError("Use a canonical RFC1918 IPv4 subnet with prefix length 16 through 28.") from None


def _check_subnet(context: str, value: str) -> str:
    network = _validate_subnet(value)
    docker = ["docker", "--context", context]
    identifiers = run_command([*docker, "network", "ls", "--quiet"], env=_environment()).decode().split()
    if identifiers:
        records = strict_json(run_command([*docker, "network", "inspect", *identifiers], env=_environment()))
        for record in records:
            for config in record.get("IPAM", {}).get("Config") or []:
                existing = ipaddress.ip_network(config.get("Subnet", "0.0.0.0/32"))
                if isinstance(existing, ipaddress.IPv4Network) and network.overlaps(existing):
                    raise PlatformError(
                        "The requested subnet overlaps an existing Docker network. Select another subnet."
                    )
    return str(network)


def _network_override(state: dict[str, Any]) -> bytes:
    subnet = str(_validate_subnet(state["subnet"]))
    return ("networks:\n  default:\n    ipam:\n      config:\n        - subnet: " + subnet + "\n").encode()


def _ports() -> dict[str, int]:
    sockets = []
    try:
        for _ in range(4):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return dict(
            zip(
                ("postgres", "keycloak", "api", "container_api"),
                (sock.getsockname()[1] for sock in sockets),
                strict=True,
            )
        )
    finally:
        for sock in sockets:
            sock.close()


def _load(directory: Path, *, complete: bool = True) -> dict[str, Any]:
    directory = _private_directory(directory)
    state = strict_json(read_file(directory / "platform.json", 65536, private=True))
    if not isinstance(state, dict) or state.get("format") != _FORMAT:
        raise PlatformError("This is not a local platform installation directory.")
    identifier = state.get("id", "")
    if not isinstance(identifier, str) or re.fullmatch(r"[a-f0-9]{24}", identifier) is None:
        raise PlatformError("Installation ownership metadata is invalid.")
    if state.get("directory") != str(directory) or state.get("version") != __version__:
        raise PlatformError("Use this installation's original directory and matching CLI version.")
    source, fingerprint = _source(Path(state["source"]))
    if fingerprint != state.get("source_sha256"):
        raise PlatformError(
            "The source checkout has changed. Restore its original content before resuming this installation."
        )
    ports = state.get("ports", {})
    if (
        set(ports) != {"postgres", "keycloak", "api", "container_api"}
        or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports.values())
        or len(set(ports.values())) != 4
    ):
        raise PlatformError("Installation port metadata is invalid.")
    if complete and state.get("stage") != "ready":
        raise PlatformError("Setup is incomplete. Inspect status and private logs; never repeat provisioning blindly.")
    return state


@contextmanager
def _exclusive(directory: Path, name: str, busy: str) -> Iterator[None]:
    import fcntl

    _private_directory(directory)
    descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlatformError(busy) from None
        yield
    finally:
        os.close(descriptor)


def _lock(directory: Path) -> AbstractContextManager[None]:
    return _exclusive(
        directory,
        ".operation.lock",
        "Another platform command is active. Stop the foreground API with Ctrl-C before stopping services.",
    )


def _check_engine(state: dict[str, Any]) -> None:
    endpoint, engine = _docker(state["context"])
    if (endpoint, engine) != (state["endpoint"], state["engine"]):
        raise PlatformError("The saved Docker context now points to a different engine; no services were changed.")


def _compose(state: dict[str, Any]) -> list[str]:
    directory = Path(state["directory"])
    source = Path(state["source"])
    command = [
        "docker",
        "--context",
        state["context"],
        "compose",
        "--project-name",
        "weave-local-" + state["id"],
        "--env-file",
        str(directory / "postgres.env"),
        "--env-file",
        str(directory / "identity.env"),
        "-f",
        str(source / "compose.yaml"),
        "-f",
        str(source / "compose.identity.yaml"),
    ]

    if state.get("subnet") is not None:
        override = directory / "compose.network.yaml"
        if read_file(override, 4096, private=True) != _network_override(state):
            raise PlatformError("The saved network configuration has changed; no services were modified.")
        command.extend(["-f", str(override)])
    return command


def _compose_env(state: dict[str, Any]) -> dict[str, str]:
    ports = state["ports"]
    return {
        **_environment(),
        "WEAVE_POSTGRES_PORT": str(ports["postgres"]),
        "WEAVE_KEYCLOAK_PORT": str(ports["keycloak"]),
        "WEAVE_CONTAINER_API_PORT": str(ports["container_api"]),
        "WEAVE_POSTGRES_VOLUME": "weave-local-" + state["id"] + "-postgres",
        "WEAVE_KEYCLOAK_VOLUME": "weave-local-" + state["id"] + "-keycloak",
    }


def _run(
    state: dict[str, Any], name: str, command: list[str], *, env: dict[str, str] | None = None, timeout: float = 300
) -> bytes:
    path = Path(state["directory"]) / (name + "-" + uuid4().hex[:8] + ".log")
    try:
        return run_command(
            command,
            env=env or _environment(),
            cwd=Path(state["source"]),
            timeout=timeout,
            limit=8 * 1024 * 1024,
            log_path=path,
        )
    except Exception:
        if path.is_file() and b"all predefined address pools have been fully subnetted" in read_file(
            path, 8 * 1024 * 1024, private=True
        ):
            raise PlatformError(
                "Docker network pools are exhausted. Use a different local context, "
                "or a new directory with an unused --subnet; never prune other projects."
            ) from None
        raise PlatformError(
            f"Stage {name} failed. Inspect the private {name}-*.log file in the installation directory."
        ) from None


def _python(state: dict[str, Any]) -> str:
    return str(Path(state["directory"]) / "runtime/bin/python")


def _verify_runtime(state: dict[str, Any]) -> None:
    directory = Path(state["directory"])
    real_path(directory / "runtime")
    receipt = strict_json(read_file(directory / "release/release.json", 65536))
    wheel = directory / "release/artifacts" / receipt["wheel"]
    if (
        wheel.name != receipt["wheel"]
        or hashlib.sha256(read_file(wheel, 64 * 1024 * 1024)).hexdigest() != receipt["wheel_sha256"]
    ):
        raise PlatformError("The retained server wheel failed its integrity check.")
    # uv can omit archive hashes for a local wheel; compare the installed payload itself.
    code = (
        "import hashlib,importlib.metadata,json,pathlib,sys,zipfile; import firefly_weave; "
        "assert firefly_weave.__version__ == sys.argv[1]; "
        "assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[2]).resolve(); "
        "assert pathlib.Path(firefly_weave.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()); "
        "dist=importlib.metadata.distribution('firefly-weave'); "
        "d=json.loads(dist.read_text('direct_url.json')); "
        "assert not d.get('dir_info',{}).get('editable'); "
        "assert d['url'] == pathlib.Path(sys.argv[3]).as_uri(); "
        "w=zipfile.ZipFile(sys.argv[3]); "
        "assert all(hashlib.sha256(dist.locate_file(n).read_bytes()).digest() == hashlib.sha256(w.read(n)).digest() "
        "for n in w.namelist() if n.startswith('firefly_weave/') and not n.endswith('/'))"
    )
    _run(
        state,
        "runtime-check",
        [_python(state), "-I", "-c", code, state["version"], str(directory / "runtime"), str(wheel)],
    )


def _probe(url: str, issuer: str | None = None) -> bool:
    from urllib.error import URLError
    from urllib.request import ProxyHandler, build_opener

    try:
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            if response.status != 200:
                return False
            return issuer is None or json.loads(response.read(65536)).get("issuer") == issuer
    except (OSError, ValueError, URLError):
        return False


def _wait_identity(state: dict[str, Any]) -> None:
    issuer = f"http://localhost:{state['ports']['keycloak']}/realms/weave"
    for _ in range(90):
        if _probe(issuer + "/.well-known/openid-configuration", issuer):
            return
        time.sleep(1)
    raise PlatformError(
        "Keycloak discovery did not become ready. Inspect the owned dependency services and retry start."
    )


def _phase(state: dict[str, Any], stage: str) -> None:
    state["stage"] = stage
    _write(Path(state["directory"]) / "platform.json", state, replace=True)


def setup(
    directory: Path,
    source: Path,
    context: str | None,
    *,
    subnet: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    directory = real_path(directory)
    if directory.exists():
        raise PlatformError(
            "Installation directory already exists. Use start to resume it, or choose a new --directory."
        )
    state = doctor(source, context)
    source = Path(state["source"])
    if subnet is not None:
        subnet = _check_subnet(state["context"], subnet)
    state["subnet"] = subnet
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    state.update(format=_FORMAT, id=uuid4().hex[:24], directory=str(directory), ports=_ports(), stage="reserved")
    _write(directory / "platform.json", state)
    _session(state)
    if subnet is not None:
        with os.fdopen(
            os.open(directory / "compose.network.yaml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
            "wb",
        ) as stream:
            stream.write(_network_override(state))
    with _lock(directory):
        if progress is not None:
            progress("Building and installing the isolated server package")
        _phase(state, "package")
        _run(
            state,
            "package",
            [
                sys.executable,
                str(source / "scripts/prepare_release.py"),
                "--root",
                state["source"],
                "--output",
                str(directory / "release"),
            ],
        )
        receipt = strict_json(read_file(directory / "release/release.json", 65536))
        wheel = directory / "release/artifacts" / receipt["wheel"]
        if (
            wheel.name != receipt["wheel"]
            or hashlib.sha256(read_file(wheel, 64 * 1024 * 1024)).hexdigest() != receipt["wheel_sha256"]
        ):
            raise PlatformError("The prepared wheel failed its integrity check.")
        _run(state, "environment", ["uv", "venv", "--python", sys.executable, str(directory / "runtime")])
        _run(
            state,
            "dependencies",
            [
                "uv",
                "pip",
                "install",
                "--python",
                _python(state),
                "--require-hashes",
                "-r",
                str(directory / "release/images/server-requirements.txt"),
            ],
        )
        _run(state, "install", ["uv", "pip", "install", "--python", _python(state), "--no-deps", str(wheel)])
        if progress is not None:
            progress("Creating private local credentials")
        _verify_runtime(state)
        _phase(state, "credentials")
        env = _compose_env(state)
        _run(
            state,
            "postgres-config",
            [_python(state), "scripts/setup-local.py", "--output", str(directory / "postgres.env")],
            env=env,
        )
        _run(
            state,
            "identity-config",
            [_python(state), "scripts/setup-identity.py", "--output", str(directory / "identity.env")],
            env=env,
        )
        if progress is not None:
            progress("Starting owned PostgreSQL and Keycloak services")
        _phase(state, "dependencies")
        _run(state, "compose-check", [*_compose(state), "config", "--quiet"], env=env)
        _run(
            state,
            "dependencies-start",
            [
                *_compose(state),
                "up",
                "--detach",
                "--no-recreate",
                "--wait",
                "--wait-timeout",
                "180",
                "postgres",
                "keycloak",
            ],
            env=env,
        )
        _wait_identity(state)
        if progress is not None:
            progress("Provisioning the guarded local database")
        _phase(state, "database")
        provision_env = {
            **env,
            **_env_file(directory / "postgres.env"),
            "WEAVE_KEYCLOAK_TEST_URL": f"http://localhost:{state['ports']['keycloak']}",
        }
        _run(
            state,
            "database",
            [_python(state), "scripts/setup-runtime.py", "--output", str(directory / "runtime.env")],
            env=provision_env,
        )
        if progress is not None:
            progress("Verifying and linking the local host identity")
        _phase(state, "bootstrap")
        _token(state)
        token = strict_json(read_file(directory / "host-token.json", 65536, private=True))
        _run(
            state,
            "bootstrap",
            [
                _python(state),
                "-I",
                "-m",
                "firefly_weave.cli.main",
                "admin",
                "bootstrap",
                "--provider",
                "local-keycloak",
                "--issuer",
                f"http://localhost:{state['ports']['keycloak']}/realms/weave",
                "--subject",
                token["subject"],
                "--kind",
                "application",
                "--output",
                str(directory / "bootstrap.json"),
            ],
            env={**_environment(), **_env_file(directory / "runtime.env")},
        )
        if progress is not None:
            progress("Local platform setup complete")
        _phase(state, "ready")
    return _summary(state)


def _token(state: dict[str, Any]) -> None:
    directory = Path(state["directory"])
    env = {
        **_environment(),
        "WEAVE_OIDC_PROVIDERS": _env_file(directory / "runtime.env")["WEAVE_OIDC_PROVIDERS"],
        "WEAVE_HOST_SECRET": _env_file(directory / "identity.env")["WEAVE_HOST_SECRET"],
    }
    _run(state, "token", [_python(state), "-I", "-m", "firefly_weave.sdk.platform_identity", str(directory)], env=env)


def _session(state: dict[str, Any]) -> None:
    directory = Path(state["directory"])
    ports = state["ports"]
    values = {
        "WEAVE_REPO_DIR": state["source"],
        "WEAVE_WORK_DIR": str(directory),
        "WEAVE_LAUNCH_ID": "weave-local-" + state["id"],
        "WEAVE_DOCKER_CONTEXT": state["context"],
        "WEAVE_PYTHON": _python(state),
        "WEAVE_POSTGRES_PORT": str(ports["postgres"]),
        "WEAVE_KEYCLOAK_PORT": str(ports["keycloak"]),
        "WEAVE_API_PORT": str(ports["api"]),
        "WEAVE_CONTAINER_API_PORT": str(ports["container_api"]),
        "WEAVE_POSTGRES_VOLUME": "weave-local-" + state["id"] + "-postgres",
        "WEAVE_KEYCLOAK_VOLUME": "weave-local-" + state["id"] + "-keycloak",
        "WEAVE_KEYCLOAK_TEST_URL": f"http://localhost:{ports['keycloak']}",
        "WEAVE_API_URL": f"http://127.0.0.1:{ports['api']}",
    }
    with os.fdopen(
        os.open(directory / "session.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w"
    ) as stream:
        for name, value in values.items():
            stream.write(f"export {name}={shlex.quote(value)}\n")


def _demo_receipt(directory: Path) -> dict[str, Any]:
    try:
        receipt = strict_json(read_file(directory / "first-run.json", 65536, private=True))
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "succeeded"
            or receipt.get("output") != {"message": "Hello, Weave"}
        ):
            raise ValueError("Incomplete receipt")
        scope = receipt["scope"]
        for value in (
            receipt["version_id"],
            receipt["activation_id"],
            receipt["run_id"],
            scope["tenant_id"],
            scope["project_id"],
            scope["environment_id"],
        ):
            UUID(value)
        return receipt
    except Exception:
        raise PlatformError(
            "The first-run receipt is incomplete. Retain it and inspect the private demo log; no work was repeated."
        ) from None


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    url = f"http://127.0.0.1:{state['ports']['api']}"
    return {
        "ok": True,
        "directory": state["directory"],
        "stage": state["stage"],
        "api_url": url,
        "docs_url": url + "/docs",
        "context": state["context"],
        "project": "weave-local-" + state["id"],
    }


def status(directory: Path) -> dict[str, Any]:
    state = _load(directory, complete=False)
    _check_engine(state)
    result = _summary(state)
    result["api_ready"] = _probe(result["api_url"] + "/health/ready")
    result["identity_ready"] = _probe(
        f"http://localhost:{state['ports']['keycloak']}/realms/weave/.well-known/openid-configuration",
        f"http://localhost:{state['ports']['keycloak']}/realms/weave",
    )
    try:
        _demo_receipt(directory)
        result["first_run_saved"] = True
    except PlatformError:
        result["first_run_saved"] = False
    try:
        integration = _integrations(directory)
        # Report what start would configure: ready settings for the current demo workspace only.
        enabled = (
            integration is not None
            and integration["stage"] == "ready"
            and integration["scope"] == _demo_receipt(directory)["scope"]
        )
    except PlatformError:
        integration, enabled = None, False
    result["integrations_enabled"] = enabled
    if enabled and integration is not None:
        result["connector_release_ids"] = {integration["connector_version_id"]: integration["release_id"]}
    result["sign_in"] = "weave auth setup " + result["api_url"]
    return result


def _check_api_port(port: int) -> None:
    with socket.socket() as sock:
        # Match Uvicorn's bind semantics so a prior listener's TIME_WAIT can resume.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise PlatformError("The saved API port is already occupied; no process was stopped or replaced.") from None


def start(directory: Path, notice: Callable[[str], None] | None = None) -> None:
    with _lock(directory):
        state = _load(directory)
        _check_engine(state)
        _verify_runtime(state)
        _check_api_port(state["ports"]["api"])
        # Static server settings: handles and executors added later apply at the next start.
        execution = _execution_environment(state, notice or (lambda message: None))
        _run(
            state,
            "dependencies-start",
            [
                *_compose(state),
                "up",
                "--detach",
                "--no-recreate",
                "--wait",
                "--wait-timeout",
                "180",
                "postgres",
                "keycloak",
            ],
            env=_compose_env(state),
        )
        _wait_identity(state)
        _repair_login_client(state, notice or (lambda message: None))
        runtime = _env_file(directory / "runtime.env")
        env = {
            **_environment(),
            **{
                key: runtime[key]
                for key in ("WEAVE_DATABASE_URL", "WEAVE_SCHEDULER_DATABASE_URL", "WEAVE_OIDC_PROVIDERS")
            },
            "WEAVE_DOCS_ENABLED": "true",
            "WEAVE_DISPLAY_NAME": _DISPLAY_NAME,
        }
        sign_in = _client_sign_in(runtime)
        if sign_in is not None:
            env["WEAVE_CLIENT_SIGN_IN"] = sign_in
        env.update(execution)
        child = subprocess.Popen(
            [
                _python(state),
                "-I",
                "-m",
                "uvicorn",
                "firefly_weave.main:create_application",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(state["ports"]["api"]),
            ],
            env=env,
            cwd=directory,
        )
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return
        if code:
            raise PlatformError("The API exited unsuccessfully. Inspect its foreground startup output.")


def _runtime_build(state: dict[str, Any]) -> dict[str, Any]:
    """Ask the installed runtime for its build identity and built-in HTTP descriptor."""
    return _parse_build(_run(state, "build-identity", [_python(state), "-I", "-c", _BUILD_PROBE]))


def _parse_build(output: bytes) -> dict[str, Any]:
    try:
        lines = [line for line in output.decode().splitlines() if line.startswith(_BUILD_MARKER)]
        if len(lines) != 1:
            raise ValueError("One build report required")
        value = strict_json(lines[0][len(_BUILD_MARKER) :].encode())
        connector = value["connector"]
        if (
            not isinstance(value["identity"], str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", value["identity"]) is None
            or connector["metadata"] != {"name": "weave-http", "version": "2.0.0"}
            or connector["spec"]["adapter"] != "weave-http-v2"
            or [binding["task_reference"] for binding in value["bindings"]] != list(_HTTP_TASKS)
            or sorted(f"{item['taskType']}@{item['taskVersion']}" for item in value["capabilities"])
            != list(_HTTP_TASKS)
        ):
            raise ValueError("Unexpected build report")
        return cast(dict[str, Any], value)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PlatformError(
            "The installed runtime did not report its build identity. Inspect the private build-identity log."
        ) from None


def _scope_value(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"tenant_id", "project_id", "environment_id"}:
        raise ValueError("Environment scope required")
    for item in value.values():
        UUID(item)
    return cast(dict[str, str], value)


def _integrations(directory: Path) -> dict[str, Any] | None:
    """Return saved local integration settings, or None when they were never enabled."""
    path = directory / "integrations.json"
    if not os.path.lexists(path):
        return None
    try:
        value = strict_json(read_file(path, 65536, private=True))
        if (
            not isinstance(value, dict)
            or value.get("format") != _INTEGRATIONS_FORMAT
            or value.get("stage") not in {"principal", "ready", "disabled"}
        ):
            raise ValueError("Unknown settings")
        _scope_value(value["scope"])
        UUID(value["principal_id"])
        if value["stage"] == "ready":
            UUID(value["connector_version_id"])
            UUID(value["release_id"])
            if (
                re.fullmatch(r"sha256:[a-f0-9]{64}", value["image_digest"]) is None
                or value["task_types"] != list(_HTTP_TASKS)
                or value["connector"] != _HTTP_CONNECTOR
                or type(value["capacity"]) is not int
                or not 1 <= value["capacity"] <= 100
            ):
                raise ValueError("Incomplete settings")
        return cast(dict[str, Any], value)
    except (OSError, ValueError, KeyError, TypeError):
        raise PlatformError(
            "The saved integration settings are malformed or not private. Retain integrations.json for "
            "inspection; no connector execution was configured."
        ) from None


def _execution_environment(state: dict[str, Any], notice: Callable[[str], None]) -> dict[str, str]:
    """Derive native execution and secret grant settings for the demo environment only."""
    directory = Path(state["directory"])
    handles = _secret_handles(directory)
    integration = _integrations(directory)
    env: dict[str, str] = {}
    if not handles and integration is None:
        return env
    scope = _demo_receipt(directory)["scope"]
    if handles:
        env["WEAVE_SECRET_ROOT"] = str(directory / "secrets")
        env["WEAVE_SECRET_GRANTS"] = json.dumps(
            [{"scope": scope, "handle": handle, "provider": "file", "locator": handle} for handle in handles],
            separators=(",", ":"),
        )
        notice("Secret handles granted to the demo environment: " + ", ".join(handles))
    if integration is None:
        return env
    enable = _command(directory, "integrations enable")
    if integration["stage"] == "disabled":
        notice("Connector actions are disabled. To enable them again with the API running, run: " + enable)
        return env
    if integration["stage"] != "ready" or integration["scope"] != scope:
        notice("Connector actions are disabled: integration setup is incomplete. With the API running, run: " + enable)
        return env
    try:
        identity = _runtime_build(state)["identity"]
    except PlatformError:
        identity = None
    if identity != integration["image_digest"]:
        # Never start an executor the dispatcher would refuse: the API stays usable without it.
        notice(
            "Connector actions are disabled: the runtime build no longer matches the enabled release. "
            "With the API running, run: " + enable + " and then restart the API."
        )
    else:
        executor = {
            "scope": scope,
            "principal_id": integration["principal_id"],
            "release_id": integration["release_id"],
            "task_types": integration["task_types"],
            "capacity": integration["capacity"],
            "build": "local-development",
        }
        env["WEAVE_NATIVE_IMAGE_DIGEST"] = integration["image_digest"]
        env["WEAVE_NATIVE_EXECUTORS"] = json.dumps([executor], separators=(",", ":"))
        notice("Connector actions: enabled in the demo environment (built-in HTTP connector, local development build).")
    return env


def _host_token(state: dict[str, Any]) -> str:
    _token(state)
    token = strict_json(read_file(Path(state["directory"]) / "host-token.json", 65536, private=True))["access_token"]
    if not isinstance(token, str) or not token:
        raise PlatformError("The local host token is unavailable. Run token, then retry.")
    return token


def _ready_api(directory: Path, state: dict[str, Any]) -> str:
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        raise PlatformError("This command requires the client extra: install firefly-weave[client].") from None
    _check_engine(state)
    _verify_runtime(state)
    api = cast(str, _summary(state)["api_url"])
    if not _probe(api + "/health/ready"):
        raise PlatformError("Start the API in another terminal (" + _command(directory, "start") + "), then retry.")
    return api


def _workspace(directory: Path, purpose: str) -> tuple[dict[str, Any], dict[str, str]]:
    if not (directory / "platform.json").exists():
        raise PlatformError("No local platform exists here yet. Create it first: " + _command(directory, "setup"))
    state = _load(directory)
    if not (directory / "first-run.json").exists():
        raise PlatformError(f"Create the demo workspace {purpose} first: " + _command(directory, "demo"))
    return state, _scope_value(_demo_receipt(directory)["scope"])


async def _enable(
    api: str,
    scope: dict[str, str],
    build: dict[str, Any],
    host_token: str,
    saved: dict[str, Any],
    save: Callable[[dict[str, Any]], None],
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.members import MemberGrantRequest, PrincipalCreateRequest
    from firefly_weave.contracts.workers import ReleaseRequest, WorkerRelease
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError
    from firefly_weave.sdk.members import MembersClient

    environment = Scope.model_validate(scope)
    source = json.dumps(build["connector"], sort_keys=True, separators=(",", ":"))
    # A key derived from the exact manifest lets a repeated command replay the same publication.
    key = "weave-platform-http-v2-" + hashlib.sha256(source.encode()).hexdigest()[:40]
    release_request = ReleaseRequest.model_validate_json(
        json.dumps(
            {
                "image_digest": build["identity"],
                "capabilities": build["capabilities"],
                "connector_bindings": build["bindings"],
                "credential_capabilities": list(_HTTP_TASKS),
            }
        )
    )
    async with WeaveClient(api, lambda: host_token, environment, timeout=30, transport=transport) as client:
        members = MembersClient(client)
        try:
            connector = await client.publish("connectors", source, "json", idempotency_key=key)
            release = cast(WorkerRelease, await client.invoke("releases.create", body=release_request))
            principal = saved.get("principal_id")
            if principal is None:
                # The native principal has no identity link: it cannot sign in, only run in-process.
                principal = str((await members.create_principal(PrincipalCreateRequest(kind="worker"))).id)
                save({"format": _INTEGRATIONS_FORMAT, "stage": "principal", "scope": scope, "principal_id": principal})
            await members.grant(
                MemberGrantRequest(
                    principal_id=UUID(principal),
                    role="worker",
                    project_id=environment.project_id,
                    environment_id=environment.environment_id,
                    resources=(str(release.id), *_HTTP_TASKS),
                )
            )
        except WeaveError as error:
            raise PlatformError(
                f"The local API refused the integration setup ({error.code}). No secret was sent; "
                "fix the cause, then rerun this command."
            ) from None
    value = {
        "format": _INTEGRATIONS_FORMAT,
        "stage": "ready",
        "scope": scope,
        "connector": _HTTP_CONNECTOR,
        "connector_version_id": str(connector.id),
        "release_id": str(release.id),
        "principal_id": principal,
        "image_digest": build["identity"],
        "task_types": list(_HTTP_TASKS),
        "capacity": _NATIVE_CAPACITY,
    }
    save(value)
    return value


def integrations_enable(directory: Path, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Let the demo environment run built-in HTTP connector actions on this local platform.

    Publishes the installed `weave-http@2.0.0` manifest, registers the runtime's local build
    identity as a release with the descriptor's own capabilities and bindings, and grants a
    dedicated native principal the worker role for exactly that release and those tasks.
    Repeating it changes nothing that already exists. A restart applies it to the API.
    """
    state, scope = _workspace(directory, "that runs connector actions")
    api = _ready_api(directory, state)
    build = _runtime_build(state)
    with _exclusive(directory, ".integrations.lock", "Another integration command is active; retry after it ends."):
        saved = _integrations(directory) or {}
        if saved and saved["scope"] != scope:
            raise PlatformError(
                "The saved integration settings belong to another workspace; nothing was changed. "
                "Retain integrations.json for inspection."
            )
        host_token = _host_token(state)
        import asyncio

        value = asyncio.run(
            _enable(
                api,
                scope,
                build,
                host_token,
                saved,
                lambda item: _write(directory / "integrations.json", item, replace=True),
                transport,
            )
        )
    restart = saved != value
    return {
        "ok": True,
        "connector": _HTTP_CONNECTOR,
        "connector_version_id": value["connector_version_id"],
        "release_id": value["release_id"],
        "connector_release_ids": {value["connector_version_id"]: value["release_id"]},
        "task_types": value["task_types"],
        "build": "local-development",
        "restart_required": restart,
        "next": [
            _command(directory, "start"),
            _command(directory, "secret set --handle NAME"),
            _command(directory, "integrations grant --connection REVISION_ID --access read"),
        ],
    }


def integrations_disable(directory: Path) -> dict[str, Any]:
    """Stop configuring the local executor at the next start; server records stay and are reused later.

    Needs no running API, so it also recovers an API whose native executor cannot start.
    """
    _load(directory)
    with _exclusive(directory, ".integrations.lock", "Another integration command is active; retry after it ends."):
        saved = _integrations(directory)
        if saved is None:
            raise PlatformError("Integrations were never enabled in this installation; nothing was changed.")
        _write(directory / "integrations.json", {**saved, "stage": "disabled"}, replace=True)
    return {
        "ok": True,
        "disabled": True,
        "restart_required": saved["stage"] == "ready",
        "message": "Restart the API (Ctrl-C, then start) to stop running connector actions. "
        "The local release and its grants remain unused on the server; enable reuses them.",
    }


async def _grant(
    api: str,
    scope: dict[str, str],
    release: str,
    revision: UUID,
    capabilities: list[str],
    host_token: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.workers import CredentialGrantRequest
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError

    async with WeaveClient(api, lambda: host_token, Scope.model_validate(scope), timeout=30, transport=transport) as c:
        try:
            for capability in capabilities:
                request = CredentialGrantRequest(
                    release_id=UUID(release), connection_revision_id=revision, capability=capability
                )
                await c.invoke("workers.grant", body=request)
        except WeaveError as error:
            raise PlatformError(
                f"The local API refused the connection grant ({error.code}). Use a connection revision ID "
                "from the demo environment."
            ) from None


def integrations_grant(
    directory: Path,
    connection: str,
    access: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Allow the local release to lease one connection revision's credentials for reads and/or writes."""
    try:
        revision = UUID(connection)
    except ValueError:
        raise PlatformError("Use the connection revision ID that connection creation returned.") from None
    selected = tuple(access)
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= {"read", "write"}:
        raise PlatformError("Choose --access read, --access write, or both.")
    state, scope = _workspace(directory, "that runs connector actions")
    integration = _integrations(directory)
    if integration is None or integration["stage"] != "ready" or integration["scope"] != scope:
        raise PlatformError("Enable integrations first: " + _command(directory, "integrations enable"))
    api = _ready_api(directory, state)
    capabilities = [f"weave-connector-http-{name}@2.0.0" for name in ("read", "write") if name in selected]
    import asyncio

    asyncio.run(_grant(api, scope, integration["release_id"], revision, capabilities, _host_token(state), transport))
    return {
        "ok": True,
        "connection_revision_id": str(revision),
        "release_id": integration["release_id"],
        "capabilities": capabilities,
    }


def _check_handle(handle: str) -> None:
    if _SECRET_HANDLE.fullmatch(handle) is None:
        raise PlatformError(
            "Use a secret handle of 1 to 64 lowercase letters, digits, '.', '_', or '-', "
            "starting with a letter or digit."
        )


def _secret_value(raw: bytes) -> bytes:
    value = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
    if not value or len(value) > SECRET_VALUE_LIMIT or b"\x00" in value:
        raise PlatformError(f"Provide a non-empty secret value of at most {SECRET_VALUE_LIMIT} bytes, without NUL.")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        raise PlatformError("Provide the secret value as UTF-8 text.") from None
    return value


def _secret_store(directory: Path, *, create: bool) -> Path | None:
    path = directory / "secrets"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return None
        os.mkdir(path, 0o700)
        # Set the mode through a no-follow descriptor: portable, and never applied to a replaced entry.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
        info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PlatformError("The private secret store must be a directory you own with permissions 0700.")
    return path


def _secret_handles(directory: Path) -> list[str]:
    store = _secret_store(directory, create=False)
    if store is None:
        return []
    handles = []
    with os.scandir(store) as entries:
        for entry in entries:
            if entry.name.startswith(_SECRET_PENDING):
                continue
            info = entry.stat(follow_symlinks=False)
            if (
                _SECRET_HANDLE.fullmatch(entry.name) is None
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > SECRET_VALUE_LIMIT
            ):
                raise PlatformError(
                    "The private secret store contains an unexpected entry; values were not read. "
                    "Remove the entry from the secrets directory, then retry."
                )
            handles.append(entry.name)
    return sorted(handles)


def secret_preflight(directory: Path, handle: str) -> dict[str, str]:
    """Check a handle and its demo-environment scope before any value is requested."""
    _check_handle(handle)
    return _workspace(directory, "that this secret is granted to")[1]


def secret_set(directory: Path, handle: str, value: bytes) -> dict[str, Any]:
    """Store a development secret value privately and make the handle available to the demo environment.

    The value is never printed, logged, or passed to a process; connections reference the handle.
    """
    scope = secret_preflight(directory, handle)
    secret = _secret_value(value)
    with _exclusive(directory, ".secrets.lock", "Another secret command is active; retry after it ends."):
        store = cast(Path, _secret_store(directory, create=True))
        for name in os.listdir(store):
            if name.startswith(_SECRET_PENDING):
                os.unlink(store / name)
        target = store / handle
        created = not os.path.lexists(target)
        if not created and not stat.S_ISREG(os.lstat(target).st_mode):
            raise PlatformError("The private secret store contains an unexpected entry; nothing was changed.")
        descriptor, temporary = tempfile.mkstemp(prefix=_SECRET_PENDING, dir=store)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secret)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
    return {
        "ok": True,
        "handle": handle,
        "provider": "file",
        "scope": scope,
        "created": created,
        "restart_required": created,
        "message": (
            "Stored privately. Restart the API (Ctrl-C, then start) so the demo environment can use this handle."
            if created
            else "Replaced privately. An API started after this handle existed reads the new value at its next use."
        ),
    }


def secret_list(directory: Path) -> dict[str, Any]:
    """List stored handle names; values are never read."""
    _load(directory)
    return {"ok": True, "provider": "file", "handles": _secret_handles(directory)}


def secret_remove(directory: Path, handle: str) -> dict[str, Any]:
    """Delete one stored development secret value; a restart withdraws its grant."""
    _check_handle(handle)
    _load(directory)
    with _exclusive(directory, ".secrets.lock", "Another secret command is active; retry after it ends."):
        if handle not in _secret_handles(directory):
            raise PlatformError(f"No local secret named {handle} exists in this installation.")
        os.unlink(directory / "secrets" / handle)
    return {
        "ok": True,
        "handle": handle,
        "removed": True,
        "restart_required": True,
        "message": "Deleted the value. Restart the API (Ctrl-C, then start) to withdraw the handle.",
    }


def stop(directory: Path) -> dict[str, Any]:
    with _lock(directory):
        state = _load(directory, complete=False)
        _check_engine(state)
        if not (directory / "postgres.env").is_file() or not (directory / "identity.env").is_file():
            raise PlatformError("Setup did not reach dependency creation; no services need stopping.")
        _run(
            state,
            "dependencies-stop",
            [*_compose(state), "stop", "--timeout", "30", "postgres", "keycloak", "keycloak-db"],
            env=_compose_env(state),
        )
        return {**_summary(state), "stopped": True, "data_retained": True}


def demo(directory: Path) -> dict[str, Any]:
    # The API holds the operation lock; a separate exclusive receipt guards this one-time demo.
    state = _load(directory)
    _check_engine(state)
    _verify_runtime(state)
    destination = directory / "first-run.json"
    if destination.exists():
        return {"ok": True, "existing": True, "receipt": _demo_receipt(directory)}
    if not _probe(_summary(state)["api_url"] + "/health/ready"):
        raise PlatformError("Start the API in another terminal, then wait for platform status to report api_ready.")
    _token(state)
    _run(
        state,
        "demo",
        [
            _python(state),
            "examples/first_run.py",
            "--api-url",
            _summary(state)["api_url"],
            "--token-file",
            str(directory / "host-token.json"),
            "--bootstrap-receipt",
            str(directory / "bootstrap.json"),
            "--output",
            str(destination),
        ],
    )
    return {"ok": True, "existing": False, "receipt": _demo_receipt(directory)}


def token(directory: Path) -> dict[str, Any]:
    state = _load(directory)
    _check_engine(state)
    _verify_runtime(state)
    _token(state)
    return {
        "ok": True,
        "token_file": str(directory / "host-token.json"),
        "message": "Verified token saved privately; never share its contents.",
    }


def _command(directory: Path, name: str) -> str:
    return "weave platform --directory " + shlex.quote(str(directory)) + " " + name


def _check_username(username: str) -> None:
    # Keycloak stores lowercase usernames; the same value forms a placeholder development email address.
    if not 3 <= len(username) <= 64 or re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", username) is None:
        raise PlatformError(
            "Use a username of 3 to 64 lowercase letters and digits, optionally joined by single '.', '_', or '-'."
        )


def _person_roles(roles: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(roles) or DEFAULT_PERSON_ROLES
    if len(set(selected)) != len(selected) or any(role not in PERSON_ROLES for role in selected):
        raise PlatformError("Choose each role at most once from: " + ", ".join(PERSON_ROLES) + ".")
    return selected


def _person_grants(roles: tuple[str, ...], scope: dict[str, str]) -> list[dict[str, Any]]:
    levels = {"tenant_admin": ("tenant_id",), "developer": ("tenant_id", "project_id")}
    # Definitions are project resources; activation, runs, and tasks belong to the demo environment.
    return [
        {
            "role": role,
            "scope": {key: scope[key] for key in levels.get(role, ("tenant_id", "project_id", "environment_id"))},
        }
        for role in roles
    ]


def _exists(username: str) -> str:
    return (
        f"A local sign-in account named {username} already exists. "
        "Choose another username; existing passwords are never reset."
    )


async def _keycloak(
    client: httpx.AsyncClient, method: str, url: str, *, token: str | None = None, **request: Any
) -> tuple[int, Any]:
    import httpx

    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        async with client.stream(method, url, headers=headers, **request) as response:
            # Identity encoding only: a compressed body would expand before the bound below applies.
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise PlatformError("The local Keycloak returned an unexpected response; nothing was linked.")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 262144:
                    raise PlatformError("The local Keycloak response exceeded its size bound; nothing was linked.")
                content.extend(chunk)
            status = response.status_code
    except httpx.HTTPError:
        raise PlatformError("The local Keycloak service is unavailable. Check platform status, then retry.") from None
    if status != 200:
        return status, None
    try:
        return status, strict_json(bytes(content))
    except ValueError:
        raise PlatformError("The local Keycloak returned an unexpected response; nothing was linked.") from None


async def _keycloak_admin(client: httpx.AsyncClient, keycloak: str, secret: str) -> str:
    status, value = await _keycloak(
        client,
        "POST",
        keycloak + "/realms/master/protocol/openid-connect/token",
        data={"grant_type": "client_credentials", "client_id": "weave-bootstrap", "client_secret": secret},
    )
    token = value.get("access_token") if isinstance(value, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        raise PlatformError(
            "The local Keycloak administrator client was refused. Check platform status; nothing was changed."
        )
    return token


# Keycloak matches any loopback port (RFC 8252) only for entries registered without a port.
_LOOPBACK_CALLBACKS = ("http://127.0.0.1/callback", "http://[::1]/callback")


async def _ensure_loopback_callbacks(
    keycloak: str, admin_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Additively register port-free loopback callbacks for the login client; True when it changed."""
    import httpx

    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        token = await _keycloak_admin(client, keycloak, admin_secret)
        clients = keycloak + "/admin/realms/weave/clients"
        status, value = await _keycloak(client, "GET", clients, token=token, params={"clientId": _LOGIN_CLIENT})
        records = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        if status != 200 or len(records) != 1 or records[0].get("clientId") != _LOGIN_CLIENT:
            raise PlatformError("The local Keycloak login client could not be read; nothing was changed.")
        record = records[0]
        identifier, current = record.get("id"), record.get("redirectUris")
        if not isinstance(identifier, str) or not isinstance(current, list):
            raise PlatformError("The local Keycloak login client could not be read; nothing was changed.")
        missing = [uri for uri in _LOOPBACK_CALLBACKS if uri not in current]
        if not missing:
            return False
        status, _ = await _keycloak(
            client, "PUT", f"{clients}/{identifier}", token=token, json={**record, "redirectUris": current + missing}
        )
        if status not in {200, 204}:
            raise PlatformError("The local Keycloak refused the login client update; nothing was changed.")
        return True


def _repair_login_client(state: dict[str, Any], notice: Callable[[str], None]) -> None:
    """Retained realms are never re-imported; keep browser sign-in working on older installations."""
    import asyncio

    keycloak = f"http://localhost:{state['ports']['keycloak']}"
    try:
        secret = _env_file(Path(state["directory"]) / "identity.env").get("WEAVE_KC_ADMIN_SECRET")
        if not secret:
            raise PlatformError("The local Keycloak administrator secret is missing.")
        if asyncio.run(_ensure_loopback_callbacks(keycloak, secret)):
            notice("Updated the local Keycloak login client so browser sign-in accepts any loopback port.")
    except (PlatformError, DeploymentError, OSError, ValueError):
        notice(
            "Browser sign-in may be refused by the local Keycloak login client. "
            "Sign in with a code instead: weave auth login --flow device"
        )


async def _keycloak_user(client: httpx.AsyncClient, users: str, token: str, username: str) -> str | None:
    status, value = await _keycloak(client, "GET", users, token=token, params={"username": username, "exact": "true"})
    if status != 200 or not isinstance(value, list):
        raise PlatformError("The local Keycloak refused the account lookup; nothing was linked.")
    matches = [item.get("id") for item in value if isinstance(item, dict) and item.get("username") == username]
    if not matches:
        return None
    subject = matches[0]
    try:
        # The Keycloak user id becomes the linked subject; accept only one canonical identifier.
        if len(matches) != 1 or not isinstance(subject, str) or str(UUID(subject)) != subject:
            raise ValueError("Ambiguous account")
    except ValueError:
        raise PlatformError("The local Keycloak returned an unexpected account; nothing was linked.") from None
    return subject


async def _enroll(
    state: dict[str, Any],
    username: str,
    password: str,
    grants: list[dict[str, Any]],
    host_token: str,
    admin_secret: str,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[str, str]:
    import httpx

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.members import MemberGrantRequest, PrincipalCreateRequest, PrincipalIdentityRequest
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError
    from firefly_weave.sdk.members import MembersClient

    keycloak = f"http://localhost:{state['ports']['keycloak']}"
    users = keycloak + "/admin/realms/weave/users"
    tenant = Scope(tenant_id=UUID(grants[0]["scope"]["tenant_id"]))
    async with (
        httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as identity,
        WeaveClient(_summary(state)["api_url"], lambda: host_token, tenant, timeout=15, transport=transport) as client,
    ):
        admin = await _keycloak_admin(identity, keycloak, admin_secret)
        if await _keycloak_user(identity, users, admin, username) is not None:
            raise PlatformError(_exists(username))
        members = MembersClient(client)
        # Grants precede the account: until the final link, nothing here can sign in or act.
        try:
            principal = (await members.create_principal(PrincipalCreateRequest(kind="human"))).id
            for grant in grants:
                scope = {
                    key: UUID(grant["scope"][key]) for key in ("project_id", "environment_id") if key in grant["scope"]
                }
                await members.grant(
                    MemberGrantRequest.model_validate({"principal_id": principal, "role": grant["role"], **scope})
                )
        except WeaveError as error:
            raise PlatformError(
                f"The local API refused the access setup ({error.code}). No sign-in account was created; "
                "any new principal has no linked identity and cannot sign in."
            ) from None
        status, _ = await _keycloak(
            identity,
            "POST",
            users,
            token=admin,
            json={
                "username": username,
                "enabled": True,
                "emailVerified": True,
                # Keycloak asks people to complete missing profile fields at first sign-in otherwise.
                "email": username + "@example.invalid",
                "firstName": username,
                "lastName": "Developer",
                "requiredActions": [],
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
        )
        if status == 409:
            raise PlatformError(_exists(username))
        subject = await _keycloak_user(identity, users, admin, username) if status == 201 else None
        if subject is None:
            raise PlatformError("The local Keycloak refused the new account; no Weave identity was linked.")
        try:
            await members.link_identity(
                principal,
                PrincipalIdentityRequest(provider_id=_PROVIDER_ID, issuer=keycloak + "/realms/weave", subject=subject),
            )
        except WeaveError as error:
            # Remove only the account created moments ago, so the same username can be retried.
            try:
                removed, _ = await _keycloak(identity, "DELETE", users + "/" + subject, token=admin)
            except PlatformError:
                removed = 0
            raise PlatformError(
                f"The local API refused the identity link ({error.code}). "
                + (
                    "The new sign-in account was removed; retry with the same username."
                    if removed == 204
                    else "The new sign-in account could not be removed; choose another username."
                )
            ) from None
    return str(principal), subject


def user(
    directory: Path,
    username: str,
    roles: Sequence[str] = (),
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Create a development-only person who signs in with the owned Keycloak to the demo workspace.

    The generated password is returned once and never written to any file.
    """
    _check_username(username)
    selected = _person_roles(roles)
    if not (directory / "platform.json").exists():
        raise PlatformError("No local platform exists here yet. Create it first: " + _command(directory, "setup"))
    state = _load(directory)
    if not (directory / "first-run.json").exists():
        raise PlatformError("Create the demo workspace this person joins first: " + _command(directory, "demo"))
    grants = _person_grants(selected, _demo_receipt(directory)["scope"])
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        raise PlatformError("Creating people requires the client extra: install firefly-weave[client].") from None
    _check_engine(state)
    _verify_runtime(state)
    api = _summary(state)["api_url"]
    if not _probe(api + "/health/ready"):
        raise PlatformError("Start the API in another terminal (" + _command(directory, "start") + "), then retry.")
    admin_secret = _env_file(directory / "identity.env").get("WEAVE_KC_ADMIN_SECRET")
    if not admin_secret:
        raise PlatformError("Private identity configuration is incomplete; retain it for recovery.")
    _token(state)
    host_token = strict_json(read_file(directory / "host-token.json", 65536, private=True))["access_token"]
    password = secrets.token_urlsafe(24)
    # Imported on use: offline commands start without event-loop, TLS, or HTTP modules.
    import asyncio

    principal, subject = asyncio.run(_enroll(state, username, password, grants, host_token, admin_secret, transport))
    return {
        "ok": True,
        "username": username,
        "password": password,
        "development_only": True,
        "warning": "Development only: this generated password is shown once and is not saved anywhere.",
        "principal_id": principal,
        "provider_id": _PROVIDER_ID,
        "issuer": f"http://localhost:{state['ports']['keycloak']}/realms/weave",
        "subject": subject,
        "grants": grants,
        "api_url": api,
        "next": ["weave auth setup " + api, "weave studio"],
    }
