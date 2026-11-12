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

"""kind, k3d and kubectl for local clusters, pinned per platform and kept only when their checksum matches.

Weave fetches each tool over HTTPS on first use into ``<platform directory>/tools/<tool>-<version>``, which only
its owner can read and run. Docker is then the only thing a developer installs, and every run uses the same
versions. A checksum the download doesn't match, a redirect away from HTTPS, or a file over the size cap keeps
nothing. The versions match the clusters' Kubernetes minor version, and ``tests/acceptance/versions.toml`` mirrors
them.
"""

from __future__ import annotations

import hashlib
import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from firefly_weave.sdk.platform import PlatformError

MAX_BYTES = 200 * 1024 * 1024
MAX_REDIRECTS = 5
NOT_FETCHED = "not fetched"


@dataclass(frozen=True)
class ToolPin:
    """One tool's version, and its download URL and SHA-256 for each ``<os>-<arch>``."""

    version: str
    files: dict[str, tuple[str, str]]


# Checked against two sources when pinned: each project's published checksums and GitHub's own asset digests (kind,
# k3d), or the SHA-256 of the downloaded binary (kubectl).
TOOLS: dict[str, ToolPin] = {
    "kind": ToolPin(
        "v0.33.0",
        {
            "darwin-arm64": (
                "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-darwin-arm64",
                "0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f",
            ),
            "darwin-amd64": (
                "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-darwin-amd64",
                "5a99f26f57246dc9319dd294803313197a0f34d33c525b3ea8b655db5916ece0",
            ),
            "linux-amd64": (
                "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64",
                "aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d",
            ),
            "linux-arm64": (
                "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-arm64",
                "20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2",
            ),
            "windows-amd64": (
                "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-windows-amd64",
                "4b22adaa135368c5a465d56bbd8e520cbea87272a06ca00b6078e7b81515c9fc",
            ),
        },
    ),
    "k3d": ToolPin(
        "v5.9.0",
        {
            "darwin-arm64": (
                "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-darwin-arm64",
                "fe106541d5d0a3f18debcd4d432a16f8c0ce3e6ddc06f8fbb6f696a122313e00",
            ),
            "darwin-amd64": (
                "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-darwin-amd64",
                "b4aabc37534f95b9c764e7823f2df923f50d57600837aa60a06266cce47db732",
            ),
            "linux-amd64": (
                "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-linux-amd64",
                "06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0",
            ),
            "linux-arm64": (
                "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-linux-arm64",
                "03cde5cf23e6e8e67de5a039ecf26e5b85aca82fba3e5d13dadf904cd218a250",
            ),
            "windows-amd64": (
                "https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-windows-amd64.exe",
                "49d0b9c796f5b8ba6f58a1dd5469e8b7b34bf9b6ce9078633eedb2789680a034",
            ),
        },
    ),
    "kubectl": ToolPin(
        "v1.37.2",
        {
            "darwin-arm64": (
                "https://dl.k8s.io/release/v1.37.2/bin/darwin/arm64/kubectl",
                "55701af0d51815ee335f240e48acf0959c3c23017ed3fa816e880c4a92bae581",
            ),
            "darwin-amd64": (
                "https://dl.k8s.io/release/v1.37.2/bin/darwin/amd64/kubectl",
                "88379aff5c8111de6be0a394adffba2819e44cde269f3ab23cef76de1a48eaa8",
            ),
            "linux-amd64": (
                "https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl",
                "c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4",
            ),
            "linux-arm64": (
                "https://dl.k8s.io/release/v1.37.2/bin/linux/arm64/kubectl",
                "fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509",
            ),
            "windows-amd64": (
                "https://dl.k8s.io/release/v1.37.2/bin/windows/amd64/kubectl.exe",
                "1691938f7de0e931969f9968c3601cf32d6b0d573573afb4ce471f3d28f51984",
            ),
        },
    ),
}
# Node images by digest, at the same Kubernetes minor version as kubectl.
KIND_NODE_IMAGE = "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
K3S_IMAGE = "rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d"
# The runner image's kubectl, one checksum per Linux architecture (the Dockerfile's runner stage).
KUBECTL_LINUX = {arch: TOOLS["kubectl"].files[f"linux-{arch}"][1] for arch in ("amd64", "arm64")}

_SYSTEMS = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}
_MACHINES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "AMD64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "ARM64": "arm64",
}


def platform_key() -> str:
    """This computer as ``<os>-<arch>``, the key of each tool's files."""
    system, machine = _SYSTEMS.get(platform.system()), _MACHINES.get(platform.machine())
    if system is None or machine is None:
        raise PlatformError("Weave's cluster tools run on macOS, Linux and Windows on amd64 or arm64 only.")
    return f"{system}-{machine}"


def _path(directory: Path, name: str) -> Path:
    suffix = ".exe" if platform_key().startswith("windows-") else ""
    return directory / "tools" / f"{name}-{TOOLS[name].version}{suffix}"


def _verified(path: Path, digest: str) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def tool(directory: Path, name: str, *, transport: httpx.BaseTransport | None = None) -> Path:
    """The pinned tool's path, fetched on first use; a file already there is used only while it verifies."""
    pin = TOOLS[name]
    key = platform_key()
    if key not in pin.files:
        raise PlatformError(f"{name} {pin.version} isn't published for {key}.")
    url, digest = pin.files[key]
    path = _path(directory, name)
    if _verified(path, digest):
        return path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir leaves an existing folder's mode as it is.
    os.chmod(path.parent, 0o700)
    label = f"{name} {pin.version}"
    try:
        with httpx.Client(timeout=60, follow_redirects=False, transport=transport) as client:
            _fetch(client, url, digest, path, label)
    except httpx.HTTPError:
        raise PlatformError(
            f"{label} couldn't be downloaded; nothing was kept. Check the network, then try again."
        ) from None
    return path


def _fetch(client: httpx.Client, url: str, digest: str, path: Path, label: str) -> None:
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme != "https":
            raise PlatformError(f"{label} is downloaded only over HTTPS; nothing was kept.")
        with client.stream("GET", url) as response:
            if response.is_redirect and response.next_request is not None:
                url = str(response.next_request.url)
                continue
            if response.status_code != 200:
                raise PlatformError(f"{label} couldn't be downloaded (HTTP {response.status_code}); nothing was kept.")
            _keep(response, digest, path, label)
            return
    raise PlatformError(f"{label} redirected too many times; nothing was kept.")


def _keep(response: httpx.Response, digest: str, path: Path, label: str) -> None:
    """Stream the body to a private temporary file and move it into place only when its checksum matches."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        hasher, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PlatformError(f"{label} is larger than {MAX_BYTES // (1024 * 1024)} MiB; nothing was kept.")
                hasher.update(chunk)
                handle.write(chunk)
        if hasher.hexdigest() != digest:
            raise PlatformError(f"{label} didn't match its published checksum; nothing was kept.")
        # Only the developer runs the tools, from their private platform directory.
        os.chmod(temporary, 0o700)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def tool_versions(directory: Path) -> dict[str, str]:
    """Each tool's pinned version when its file is present and verifies, else "not fetched"; downloads nothing."""
    try:
        key = platform_key()
    except PlatformError:
        return {name: NOT_FETCHED for name in TOOLS}
    return {
        name: pin.version if key in pin.files and _verified(_path(directory, name), pin.files[key][1]) else NOT_FETCHED
        for name, pin in TOOLS.items()
    }
