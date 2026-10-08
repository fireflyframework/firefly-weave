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

"""Model requests reach only approved addresses, are pinned to them, and send model paths only."""

import socket

import httpcore2
import httpx2
import pytest
from firefly_weave import private_origins as po
from firefly_weave.ai_policy import PolicyEndpoint

from weave_agentic_worker.egress import ModelEgressDenied, PinnedModelTransport, admit, allowed_path

OLLAMA = PolicyEndpoint(
    id="ollama-local",
    label="Ollama",
    url="http://ollama:11434/v1",
    providers=("openai-chat",),
    compat="ollama",
    credential="none",
    models="served",
)
AZURE = PolicyEndpoint(
    id="azure",
    label="Azure",
    url="https://resource.openai.azure.com",
    providers=("azure-chat",),
    models=("deployment",),
)


def model_origins(networks=("10.246.21.0/24",)):
    entry = po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=networks, credentials="none")
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])


def never_local(address):
    return False


def chain(error):
    found = []
    while error is not None and len(found) < 20:
        found.append(error)
        error = error.__cause__ or error.__context__
    return found


@pytest.mark.parametrize(
    "address,reason",
    [
        ("93.184.216.34", "public-plain-text"),
        ("10.0.0.5", "outside-networks"),
        ("169.254.169.254", "always-refused"),
        ("::ffff:169.254.169.254", "always-refused"),
        ("64:ff9b::a9fe:a9fe", "always-refused"),
        ("2002:a9fe:a9fe::1", "always-refused"),
        ("2001:0:4136:e378:8000:63bf:5601:5601", "always-refused"),
    ],
)
def test_admission_refuses_addresses_outside_the_model_entry(address, reason):
    with pytest.raises(ModelEgressDenied) as refused:
        admit(model_origins(), "http://ollama:11434/v1", (address,), local=never_local)
    assert refused.value.reason == reason


def test_cgnat_needs_an_entry_and_its_metadata_address_never_passes():
    admit(model_origins(("100.64.1.0/24",)), "http://ollama:11434/v1", ("100.64.1.5",), local=never_local)
    with pytest.raises(ModelEgressDenied, match="not permitted"):
        admit(model_origins(("100.100.100.0/24",)), "http://ollama:11434/v1", ("100.100.100.200",), local=never_local)


def test_this_namespace_is_never_a_model_destination():
    with pytest.raises(ModelEgressDenied) as refused:
        admit(model_origins(), "http://ollama:11434/v1", ("10.246.21.5",), local=lambda address: True)
    assert refused.value.reason == "control-plane"


def test_https_reaches_public_addresses_and_private_ones_only_with_an_entry():
    admit(po.PrivateOrigins.empty(), "https://api.openai.com/v1", ("104.18.6.192",), local=never_local)
    with pytest.raises(ModelEgressDenied) as refused:
        admit(po.PrivateOrigins.empty(), "https://models.internal/v1", ("10.0.0.5",), local=never_local)
    assert refused.value.reason == "no-entry"


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        "2001:0:4136:e378:8000:63bf:5601:5601",
        "fd00:ec2::254",
        "100.100.100.200",
    ],
)
def test_https_never_reaches_an_always_refused_address(address):
    with pytest.raises(ModelEgressDenied) as refused:
        admit(po.PrivateOrigins.empty(), "https://models.internal/v1", (address,), local=never_local)
    assert refused.value.reason == "always-refused"


@pytest.mark.parametrize(
    "networks,reachable,refused_addresses",
    [
        (("100.100.100.0/24",), "100.100.100.5", ("100.100.100.200",)),
        (("fd00:ec2::/64",), "fd00:ec2::5", ("fd00:ec2::254",)),
        (("10.246.21.0/24",), "10.246.21.5", ("10.246.21.5", "169.254.169.254")),
    ],
)
def test_an_https_model_entry_never_opens_an_always_refused_address(networks, reachable, refused_addresses):
    entry = po.PrivateOrigin(
        origin="https://models.internal:443", purpose="model", networks=networks, credentials="none"
    )
    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])
    admit(origins, "https://models.internal/v1", (reachable,), local=never_local)
    with pytest.raises(ModelEgressDenied) as refused:
        admit(origins, "https://models.internal/v1", refused_addresses, local=never_local)
    assert refused.value.reason == "always-refused"


def legacy_origins():
    policy = po.PrivateOrigins.empty()
    return policy.with_legacy(("model",), ("10.246.21.0/24",), setting="WEAVE_HTTP_PRIVATE_NETWORKS")


@pytest.mark.parametrize("url", ["https://ollama:11434/v1", "http://ollama:11434/v1"])
def test_a_legacy_setting_never_opens_a_private_model_destination(url):
    assert any(entry.source == "legacy" for entry in legacy_origins().entries)
    with pytest.raises(ModelEgressDenied) as refused:
        admit(legacy_origins(), url, ("10.246.21.5",), local=never_local)
    assert refused.value.reason == "no-entry"


def test_a_legacy_setting_leaves_public_https_model_destinations_reachable():
    admit(legacy_origins(), "https://api.openai.com/v1", ("104.18.6.192",), local=never_local)


@pytest.mark.parametrize(
    "endpoint,path,allowed",
    [
        (OLLAMA, "/v1/chat/completions", True),
        (OLLAMA, "/v1/models", True),
        (OLLAMA, "/api/tags", True),
        (OLLAMA, "/api/show", True),
        (OLLAMA, "/api/version", True),
        (OLLAMA, "/api/pull", False),
        (OLLAMA, "/api/delete", False),
        (OLLAMA, "/api/create", False),
        (OLLAMA, "/v1/../api/pull", False),
        (OLLAMA, "/v1/embeddings", False),
        (AZURE, "/openai/deployments/deployment/chat/completions", True),
        (AZURE, "/api/tags", False),
    ],
)
def test_only_model_paths_are_sent(endpoint, path, allowed):
    assert allowed_path(endpoint, path) is allowed


class PeerStream(httpcore2.AsyncMockStream):
    def __init__(self, buffer, peer):
        super().__init__(buffer)
        self.peer = peer
        self.written = bytearray()

    async def write(self, buffer, timeout=None):  # noqa: ASYNC109
        self.written.extend(buffer)

    def get_extra_info(self, info):
        return (self.peer, 11434) if info == "server_addr" else super().get_extra_info(info)


class Backend(httpcore2.AsyncNetworkBackend):
    def __init__(self, peer="10.246.21.5"):
        self.peer, self.connected, self.streams = peer, [], []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109
        self.connected.append((host, port))
        stream = PeerStream([b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}"], self.peer)
        self.streams.append(stream)
        return stream

    async def sleep(self, seconds):
        return None


def resolver(*addresses):
    async def resolve(host, port):
        return addresses

    return resolve


def transport(backend, addresses=("10.246.21.5",)):
    return PinnedModelTransport(
        OLLAMA, model_origins(), resolver=resolver(*addresses), local=never_local, backend=backend
    )


async def test_requests_connect_to_the_validated_address():
    backend = Backend()
    async with httpx2.AsyncClient(transport=transport(backend)) as client:
        response = await client.get("http://ollama:11434/v1/models")
    assert response.status_code == 200
    assert backend.connected == [("10.246.21.5", 11434)]
    assert bytes(backend.streams[0].written).startswith(b"GET /v1/models")


async def test_a_peer_that_changed_after_resolution_gets_no_bytes():
    backend = Backend(peer="10.246.22.9")
    async with httpx2.AsyncClient(transport=transport(backend)) as client:
        with pytest.raises(httpx2.ConnectError) as refused:
            await client.get("http://ollama:11434/v1/models")
    assert any(isinstance(item, ModelEgressDenied) for item in chain(refused.value))
    assert bytes(backend.streams[0].written) == b""


async def test_a_listed_origin_resolving_to_a_public_address_is_refused():
    backend = Backend()
    async with httpx2.AsyncClient(transport=transport(backend, ("93.184.216.34",))) as client:
        with pytest.raises(httpx2.ConnectError) as refused:
            await client.post("http://ollama:11434/v1/chat/completions", json={})
    denied = [item for item in chain(refused.value) if isinstance(item, ModelEgressDenied)]
    assert denied and denied[0].reason == "public-plain-text" and backend.connected == []


@pytest.mark.parametrize("path", ["/api/pull", "/api/delete", "/api/push"])
async def test_pull_and_other_management_paths_are_never_sent(path):
    backend = Backend()
    async with httpx2.AsyncClient(transport=transport(backend)) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.post("http://ollama:11434" + path, json={"model": "qwen3:4b"})
    assert backend.connected == []


async def test_another_origin_is_never_reached_through_this_transport():
    backend = Backend()
    async with httpx2.AsyncClient(transport=transport(backend)) as client:
        with pytest.raises(httpx2.ConnectError):
            await client.get("http://keycloak:8080/v1/models")
    assert backend.connected == []


async def test_an_unresolvable_endpoint_is_unreachable_not_refused():
    async def fail(host, port):
        raise socket.gaierror("no such name")

    backend = Backend()
    pinned = PinnedModelTransport(OLLAMA, model_origins(), resolver=fail, local=never_local, backend=backend)
    async with httpx2.AsyncClient(transport=pinned) as client:
        with pytest.raises(httpx2.ConnectError) as failed:
            await client.get("http://ollama:11434/v1/models")
    found = chain(failed.value)
    assert not any(isinstance(item, ModelEgressDenied) for item in found)
    assert any(isinstance(item, socket.gaierror) for item in found)
