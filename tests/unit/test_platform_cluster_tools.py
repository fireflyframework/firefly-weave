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

"""kind, k3d and kubectl are pinned per platform and kept only when their published checksum matches."""

import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path

import httpx
import pytest

from firefly_weave.sdk import platform_cluster_tools as tools
from firefly_weave.sdk.platform import PlatformError

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {"darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64", "windows-amd64"}
BINARY = b"\x7fELF pinned kubectl"


@pytest.fixture
def pinned(monkeypatch):
    """kubectl pinned to BINARY for this computer, served by a recording fake transport."""
    key = "linux-amd64"
    monkeypatch.setattr(tools, "platform_key", lambda: key)
    url = "https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl"
    pin = tools.ToolPin("v1.37.2", {key: (url, hashlib.sha256(BINARY).hexdigest())})
    monkeypatch.setitem(tools.TOOLS, "kubectl", pin)
    served = {"body": BINARY, "requests": []}

    def handler(request):
        served["requests"].append(str(request.url))
        if "redirect" in served:
            return httpx.Response(302, headers={"Location": served.pop("redirect")})
        return httpx.Response(200, content=served["body"])

    return httpx.MockTransport(handler), served


def kept(directory):
    folder = directory / "tools"
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def test_a_fetched_tool_is_kept_only_when_its_checksum_matches(tmp_path, pinned):
    transport, served = pinned
    served["body"] = BINARY + b" tampered"
    with pytest.raises(PlatformError) as refused:
        tools.tool(tmp_path, "kubectl", transport=transport)
    assert str(refused.value) == "kubectl v1.37.2 didn't match its published checksum; nothing was kept."
    assert kept(tmp_path) == []
    served["body"] = BINARY
    path = tools.tool(tmp_path, "kubectl", transport=transport)
    assert path == tmp_path / "tools" / "kubectl-v1.37.2" and path.read_bytes() == BINARY
    assert kept(tmp_path) == ["kubectl-v1.37.2"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o700


def test_a_verified_tool_is_reused_without_fetching(tmp_path, pinned):
    transport, served = pinned
    first = tools.tool(tmp_path, "kubectl", transport=transport)
    assert tools.tool(tmp_path, "kubectl", transport=transport) == first
    assert len(served["requests"]) == 1


def test_a_changed_file_on_disk_is_fetched_again(tmp_path, pinned):
    transport, served = pinned
    path = tools.tool(tmp_path, "kubectl", transport=transport)
    path.write_bytes(b"replaced")
    assert tools.tool(tmp_path, "kubectl", transport=transport).read_bytes() == BINARY
    assert len(served["requests"]) == 2


def test_a_redirect_to_plain_http_is_refused(tmp_path, pinned):
    transport, served = pinned
    served["redirect"] = "http://mirror.example/kubectl"
    with pytest.raises(PlatformError, match="over HTTPS; nothing was kept"):
        tools.tool(tmp_path, "kubectl", transport=transport)
    assert served["requests"] == ["https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl"]
    assert kept(tmp_path) == []


def test_a_download_over_the_size_cap_is_refused(tmp_path, pinned, monkeypatch):
    transport, _ = pinned
    monkeypatch.setattr(tools, "MAX_BYTES", len(BINARY) - 1)
    with pytest.raises(PlatformError, match="larger than"):
        tools.tool(tmp_path, "kubectl", transport=transport)
    assert kept(tmp_path) == []


def test_versions_report_only_present_verified_tools(tmp_path, pinned):
    transport, _ = pinned
    assert tools.tool_versions(tmp_path)["kubectl"] == "not fetched"
    path = tools.tool(tmp_path, "kubectl", transport=transport)
    assert tools.tool_versions(tmp_path) == {"kubectl": "v1.37.2", "kind": "not fetched", "k3d": "not fetched"}
    path.write_bytes(b"replaced")
    assert tools.tool_versions(tmp_path)["kubectl"] == "not fetched"


def test_every_pinned_file_names_an_https_url_and_a_checksum():
    assert set(tools.TOOLS) == {"kind", "k3d", "kubectl"}
    for name, pin in tools.TOOLS.items():
        assert set(pin.files) == PLATFORMS, name
        for platform, (url, digest) in pin.files.items():
            assert url.startswith("https://") and pin.version in url and platform.split("-")[1] in url
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (name, platform)
    for image in (tools.KIND_NODE_IMAGE, tools.K3S_IMAGE):
        assert re.fullmatch(r"[a-z0-9./-]+:v1\.37\.[0-9]+(-k3s1)?@sha256:[0-9a-f]{64}", image)


def test_versions_toml_agrees_with_the_pinned_tools_and_images(monkeypatch):
    spec = importlib.util.spec_from_file_location("acceptance_journeys", ROOT / "scripts/acceptance_journeys.py")
    journeys = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "acceptance_journeys", journeys)
    spec.loader.exec_module(journeys)
    cluster = journeys.load_versions(ROOT / "tests/acceptance/versions.toml")["cluster"]
    assert cluster["tools"] == {name: pin.version for name, pin in tools.TOOLS.items()}
    assert cluster["images"] == {"kind_node": tools.KIND_NODE_IMAGE, "k3s": tools.K3S_IMAGE}


def test_the_runner_image_takes_kubectl_from_the_pinned_checksums():
    dockerfile = (ROOT / "Dockerfile").read_text()
    version = tools.TOOLS["kubectl"].version
    for arch, digest in tools.KUBECTL_LINUX.items():
        assert digest == tools.TOOLS["kubectl"].files[f"linux-{arch}"][1]
        line = f"ADD --checksum=sha256:{digest} https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl /kubectl"
        assert dockerfile.count(line) == 1, arch
    assert "COPY --from=kubectl --chmod=0755 /kubectl /usr/local/bin/kubectl" in dockerfile
    assert 'RUN ["/usr/local/bin/kubectl", "version", "--client"]' in dockerfile


def test_an_existing_tools_folder_is_made_private(tmp_path, pinned):
    transport, _ = pinned
    (tmp_path / "tools").mkdir(mode=0o755)
    (tmp_path / "tools").chmod(0o755)
    tools.tool(tmp_path, "kubectl", transport=transport)
    if os.name != "nt":
        assert (tmp_path / "tools").stat().st_mode & 0o777 == 0o700


def test_reusing_a_verified_tool_also_keeps_its_folder_private(tmp_path, pinned):
    transport, served = pinned
    tools.tool(tmp_path, "kubectl", transport=transport)
    (tmp_path / "tools").chmod(0o755)
    tools.tool(tmp_path, "kubectl", transport=transport)
    assert len(served["requests"]) == 1
    if os.name != "nt":
        assert (tmp_path / "tools").stat().st_mode & 0o777 == 0o700
