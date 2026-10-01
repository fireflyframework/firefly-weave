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
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from firefly_weave import __version__
from firefly_weave.sdk.deployment import read_file, real_path, run_command, strict_json

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


class PlatformError(ValueError):
    """An actionable message containing no supplied credentials or subprocess output."""


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
def _lock(directory: Path) -> Iterator[None]:
    import fcntl

    _private_directory(directory)
    descriptor = os.open(directory / ".operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlatformError(
                "Another platform command is active. Stop the foreground API with Ctrl-C before stopping services."
            ) from None
        yield
    finally:
        os.close(descriptor)


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
    return result


def _check_api_port(port: int) -> None:
    with socket.socket() as sock:
        # Match Uvicorn's bind semantics so a prior listener's TIME_WAIT can resume.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise PlatformError("The saved API port is already occupied; no process was stopped or replaced.") from None


def start(directory: Path) -> None:
    with _lock(directory):
        state = _load(directory)
        _check_engine(state)
        _verify_runtime(state)
        _check_api_port(state["ports"]["api"])
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
        runtime = _env_file(directory / "runtime.env")
        env = {
            **_environment(),
            **{
                key: runtime[key]
                for key in ("WEAVE_DATABASE_URL", "WEAVE_SCHEDULER_DATABASE_URL", "WEAVE_OIDC_PROVIDERS")
            },
            "WEAVE_DOCS_ENABLED": "true",
        }
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
