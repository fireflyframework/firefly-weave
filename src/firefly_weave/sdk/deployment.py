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

"""Offline, allowlisted preparation of an exact-wheel worker image context."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
import zipfile
from contextlib import suppress
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.contracts.workers import ReleaseRequest

PYTHON_IMAGE = "python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1"
WHEEL_LABEL = "io.getfirefly.weave.wheel-sha256"
MANIFEST_LABEL = "io.getfirefly.weave.manifest-sha256"


class DeploymentError(ValueError):
    """Value-free local preparation or deployment failure."""


def fail() -> None:
    raise DeploymentError("Worker operation failed; check inputs and prerequisites.")


def real_path(path: Path) -> Path:
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        fail()
    return path


def open_directory(path: Path) -> int:
    """Anchor every component without following replaced directories or symlinks."""
    path = path.absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_file(path: Path, limit: int, *, private: bool = False) -> bytes:
    path = real_path(path)
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            fail()
        if private and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            fail()
        result = stream.read(limit + 1)
        if len(result) > limit:
            fail()
        return result


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail()
        result[key] = value
    return result


def strict_json(data: bytes) -> Any:
    return json.loads(data, object_pairs_hook=_unique)


def manifest_bytes(data: bytes) -> bytes:
    value = strict_json(data)
    if not isinstance(value, dict) or "image_digest" in value:
        fail()
    release = ReleaseRequest.model_validate({**value, "image_digest": "sha256:" + "0" * 64})
    for capability in release.capabilities:
        if validate_schema(capability.input_schema, {}) or validate_schema(capability.output_schema, {}):
            fail()
    return canonical_bytes(release.model_dump(mode="json", by_alias=True, exclude={"image_digest"}))


def requirements_bytes(data: bytes) -> bytes:
    """Accept only pinned hash-bearing exports; pip option injection is forbidden."""
    lines = data.decode("utf-8").replace("\\\n", " ").splitlines()
    accepted: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pieces = re.split(r"\s+--hash=sha256:", line)
        if len(pieces) < 2 or any(not re.fullmatch(r"[a-f0-9]{64}", item.strip()) for item in pieces[1:]):
            fail()
        requirement = pieces[0].strip()
        if " ; " in requirement:
            requirement, marker = requirement.split(" ; ", 1)
            variable = (
                r"(?:python_version|python_full_version|os_name|sys_platform|platform_release|"
                r"platform_system|platform_version|platform_machine|platform_python_implementation|"
                r"implementation_name|implementation_version)"
            )
            comparison = variable + r"\s*(?:==|!=|<=|>=|<|>|~=|not in|in)\s*['\"][A-Za-z0-9_.*+! -]{1,128}['\"]"
            if not re.fullmatch(comparison + r"(?:\s+(?:and|or)\s+" + comparison + r")*", marker):
                fail()
        if " @ " in requirement:
            name, url = requirement.split(" @ ", 1)
            parsed = urlsplit(url)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                fail()
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                fail()
            if parsed.query or parsed.fragment or any(character.isspace() for character in url):
                fail()
            if not parsed.path.endswith((".whl", ".tar.gz", ".zip")):
                fail()
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", requirement):
            fail()
        accepted.append(line)
    if not accepted:
        fail()
    return ("\n".join(accepted) + "\n").encode()


def wheel_licenses(data: bytes, filename: str) -> dict[str, bytes]:
    match = re.fullmatch(r"firefly_weave-([A-Za-z0-9_.+!]+)-py3-none-any\.whl", filename)
    if match is None:
        fail()
    assert match is not None
    version = match.group(1)
    prefix = f"firefly_weave-{version}.dist-info/"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names: set[str] = set()
        total = 0
        for member in archive.infolist():
            decoded = unquote(member.filename, errors="strict")
            path = PurePosixPath(decoded)
            if decoded != member.filename or "\\" in decoded or "\x00" in decoded:
                fail()
            if path.as_posix() != decoded.rstrip("/") or member.orig_filename != member.filename:
                fail()
            if path.is_absolute() or ".." in path.parts or not path.parts or decoded in names:
                fail()
            if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                fail()
            if set(path.parts) & {".superpowers", ".codex", ".agents", ".secrets", ".local", "AGENTS.md", "CLAUDE.md"}:
                fail()
            if path.parts[0] not in {"firefly_weave", prefix.rstrip("/")}:
                fail()
            names.add(decoded)
            total += member.file_size
            if total > 128 * 1024 * 1024 or member.file_size > 16 * 1024 * 1024:
                fail()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if metadata_names != [prefix + "METADATA"]:
            fail()
        metadata = BytesParser().parsebytes(archive.read(prefix + "METADATA"))
        if metadata.get_all("Name") != ["firefly-weave"] or metadata.get_all("Version") != [version]:
            fail()
        return {name: archive.read(prefix + "licenses/" + name) for name in ("LICENSE", "NOTICE")}


def worker_dockerfile(wheel: str, wheel_hash: str, manifest_hash: str) -> bytes:
    return f"""# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
FROM {UV_IMAGE} AS uv
FROM {PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/weave
COPY worker-requirements.txt /opt/weave/worker-requirements.txt
RUN uv pip install --system --no-cache --require-hashes -r worker-requirements.txt
COPY {wheel} /opt/weave/{wheel}
RUN uv pip install --system --no-cache --no-deps /opt/weave/{wheel}
COPY --chmod=0644 main.py manifest.json LICENSE NOTICE /opt/weave/
LABEL {WHEEL_LABEL}="{wheel_hash}" {MANIFEST_LABEL}="{manifest_hash}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["python", "-I", "/opt/weave/main.py"]
""".encode()


def package_worker(
    manifest: Path,
    output: Path,
    *,
    wheel: Path | None = None,
    requirements: Path | None = None,
    entrypoint: Path | None = None,
) -> dict[str, Any]:
    if wheel is None:
        candidates = sorted(Path("dist").glob("firefly_weave-*.whl"))
        if len(candidates) != 1:
            fail()
        wheel = candidates[0]
    requirements = requirements or Path("build/release/worker-requirements.txt")
    entrypoint = entrypoint or manifest.parent / "main.py"
    output = real_path(output)
    inputs = (manifest, wheel, requirements, entrypoint)
    if any(real_path(path).is_relative_to(output) for path in inputs):
        fail()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        fail()
    if not output.parent.is_dir():
        fail()
    manifest_data = manifest_bytes(read_file(manifest, 1024 * 1024))
    wheel_data = read_file(wheel, 64 * 1024 * 1024)
    files = wheel_licenses(wheel_data, wheel.name)
    files.update(
        {
            wheel.name: wheel_data,
            "manifest.json": manifest_data,
            "main.py": read_file(entrypoint, 1024 * 1024),
            "worker-requirements.txt": requirements_bytes(read_file(requirements, 2 * 1024 * 1024)),
        }
    )
    wheel_hash = hashlib.sha256(wheel_data).hexdigest()
    manifest_hash = hashlib.sha256(manifest_data).hexdigest()
    files["Dockerfile"] = worker_dockerfile(wheel.name, wheel_hash, manifest_hash)
    files["compose.json"] = canonical_bytes(
        {
            "services": {
                "worker": {
                    "image": "${WEAVE_WORKER_IMAGE:?exact local image ID required}",
                    "read_only": True,
                    "user": "65532:65532",
                    "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],
                    "stop_grace_period": "30s",
                    "restart": "no",
                    "env_file": [{"path": "${WEAVE_WORKER_ENV_FILE:?private env file required}", "format": "raw"}],
                }
            }
        }
    )
    metadata: dict[str, Any] = {
        "complete": True,
        "format": "weave/worker-context-v1",
        "wheel": wheel.name,
        "wheel_sha256": wheel_hash,
        "manifest_sha256": manifest_hash,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    files["build.json"] = canonical_bytes(metadata)
    parent_fd = open_directory(output.parent)
    try:
        with suppress(FileExistsError):
            os.mkdir(output.name, mode=0o700, dir_fd=parent_fd)
        directory_fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        if os.listdir(directory_fd):
            fail()
        for name, data in files.items():
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        os.close(directory_fd)
    return {"mode": "worker-package", "wheel_sha256": wheel_hash, "manifest_sha256": manifest_hash}


OWNERSHIP_LABEL = "io.getfirefly.weave.deployment"


def run_command(
    argv: list[str], *, timeout: float = 30, limit: int = 1024 * 1024, log_path: Path | None = None
) -> bytes:
    """Bound bytes and wall time, killing only the process group created here."""
    if timeout <= 0 or limit < 1:
        fail()
    log = None
    if log_path is not None:
        parent = open_directory(log_path.absolute().parent)
        try:
            log = os.fdopen(
                os.open(log_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent),
                "wb",
            )
        finally:
            os.close(parent)
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
        )
    except OSError:
        if log is not None:
            log.close()
        raise DeploymentError("Worker operation failed; check inputs and prerequisites.") from None
    result = bytearray()
    deadline = time.monotonic() + timeout
    assert child.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail()
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, limit - len(result) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    result.extend(chunk)
                    if len(result) > limit:
                        fail()
            if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                fail()
        return bytes(result)
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError("Worker operation failed; check inputs and prerequisites.") from None
    finally:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        child.stdout.close()
        if log is not None:
            log.write(result[:limit])
            log.close()


def verified_context(directory: Path) -> dict[str, Any]:
    directory = real_path(directory)
    value = strict_json(read_file(directory / "build.json", 1024 * 1024))
    if (
        not isinstance(value, dict)
        or value.get("complete") is not True
        or value.get("format") != "weave/worker-context-v1"
    ):
        fail()
    if not isinstance(value.get("files"), dict) or not re.fullmatch(
        r"firefly_weave-[A-Za-z0-9_.+!]+-py3-none-any\.whl", value.get("wheel", "")
    ):
        fail()
    expected = {
        "manifest.json",
        "main.py",
        "worker-requirements.txt",
        "Dockerfile",
        "compose.json",
        "LICENSE",
        "NOTICE",
        value["wheel"],
    }
    if set(value["files"]) != expected or {path.name for path in directory.iterdir()} != expected | {"build.json"}:
        fail()
    for name, digest in value["files"].items():
        if hashlib.sha256(read_file(directory / name, 64 * 1024 * 1024)).hexdigest() != digest:
            fail()
    if (
        value.get("wheel_sha256") != value["files"][value["wheel"]]
        or value.get("manifest_sha256") != value["files"]["manifest.json"]
    ):
        fail()
    return dict(value)


def deploy_worker(directory: Path, *, image: str, project: str, context: str, env_file: Path) -> dict[str, Any]:
    """Create without replacement, then start only an exact nonce-owned container."""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        fail()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
        fail()
    metadata = verified_context(directory)
    env_file = real_path(env_file)
    read_file(env_file, 1024 * 1024, private=True)
    docker = ["docker", "--context", context]
    contexts = strict_json(run_command([*docker, "context", "inspect", context]))
    endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        fail()
    images = strict_json(run_command([*docker, "image", "inspect", image]))
    if len(images) != 1 or images[0]["Id"] != image:
        fail()
    labels = images[0]["Config"].get("Labels") or {}
    if labels.get(WHEEL_LABEL) != metadata["wheel_sha256"] or labels.get(MANIFEST_LABEL) != metadata["manifest_sha256"]:
        fail()
    containers = [
        *docker,
        "ps",
        "--all",
        "--no-trunc",
        "--quiet",
        "--filter",
        "label=com.docker.compose.project=" + project,
    ]
    if run_command(containers).strip():
        fail()
    nonce = uuid.uuid4().hex
    deployment = real_path(directory).parent / (".weave-deployment-" + nonce)
    deployment.mkdir(mode=0o700)
    service: dict[str, Any] = {
        "image": image,
        "pull_policy": "never",
        "read_only": True,
        "user": "65532:65532",
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"],
        "stop_grace_period": "30s",
        "restart": "no",
        "env_file": [{"path": str(env_file).replace("$", "$$"), "format": "raw"}],
        "labels": {OWNERSHIP_LABEL: nonce},
    }
    compose_path = deployment / "compose.json"
    with compose_path.open("xb") as stream:
        stream.write(canonical_bytes({"services": {"worker": service}}))
    compose_path.chmod(0o600)
    compose = [*docker, "compose", "--project-name", project, "--file", str(compose_path)]
    run_command([*compose, "config", "--quiet"])
    run_command([*compose, "create", "--no-recreate", "--pull", "never", "worker"], timeout=60)
    ids = run_command(containers).decode("ascii").split()
    if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
        fail()
    inspected = strict_json(run_command([*docker, "container", "inspect", ids[0]]))
    if len(inspected) != 1:
        fail()
    owned = inspected[0]
    owned_labels = owned["Config"].get("Labels") or {}
    if owned.get("Id") != ids[0] or owned.get("Image") != image or owned["Config"].get("Image") != image:
        fail()
    if (
        owned_labels.get(OWNERSHIP_LABEL) != nonce
        or owned_labels.get("com.docker.compose.project") != project
        or owned_labels.get("com.docker.compose.service") != "worker"
        or owned["State"].get("Status") != "created"
    ):
        fail()
    run_command([*docker, "start", ids[0]])
    result: dict[str, Any] = {
        "mode": "worker-deploy",
        "container_id": ids[0],
        "image": image,
        "project": project,
        "context": context,
        "stop_argv": [*docker, "stop", "--time", "30", ids[0]],
    }
    with (deployment / "started.json").open("xb") as stream:
        stream.write(canonical_bytes(result))
    return result
