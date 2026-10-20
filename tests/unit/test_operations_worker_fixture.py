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

"""The sleep fixture worker the local platform runs for Operations: bounded, unprivileged, database-free."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from firefly_weave.contracts.workers import ReleaseRequest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/operations-worker"


def fixture():
    spec = importlib.util.spec_from_file_location("operations_worker_fixture", FIXTURE / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest():
    return json.loads((FIXTURE / "manifest.json").read_text())


async def test_the_fixture_worker_returns_after_sleeping_the_requested_time(monkeypatch):
    module = fixture()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    assert await module.sleep(SimpleNamespace(input={"ms": 25})) == {"slept_ms": 25}
    assert slept == [0.025]


def test_the_fixture_manifest_bounds_the_sleep():
    (capability,) = manifest()["capabilities"]
    assert (capability["taskType"], capability["taskVersion"], capability["sideEffect"]) == (
        "example-sleep",
        "1.0.0",
        "read_only",
    )
    # The manifest is what the platform admits as the fixture worker's release.
    ReleaseRequest.model_validate_json(json.dumps({"image_digest": "sha256:" + "a" * 64, **manifest()}))
    assert manifest()["credential_capabilities"] == []
    schema = capability["inputSchema"]
    jsonschema.validate({"ms": 60000}, schema)
    for refused in ({"ms": 60001}, {"ms": -1}, {"ms": 5, "extra": True}, {}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(refused, schema)
    jsonschema.validate({"slept_ms": 1}, capability["outputSchema"])


@pytest.mark.parametrize("name", ["WEAVE_DATABASE_URL", "PGPASSWORD", "WEAVE_KC_ADMIN_PASSWORD"])
async def test_the_fixture_worker_refuses_database_settings(monkeypatch, name):
    module = fixture()
    monkeypatch.setenv(name, "anything")

    def no_network(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(module.httpx, "AsyncClient", no_network)
    with pytest.raises(RuntimeError, match="database or administrator"):
        await module.main()


def test_the_fixture_worker_prints_its_manifest(capsys):
    module = fixture()
    assert module.cli(["--release-manifest"]) == 0
    assert json.loads(capsys.readouterr().out) == manifest()


@pytest.mark.parametrize(("value", "expected"), [(None, 2), ("1", 1), ("8", 8)])
def test_the_fixture_worker_capacity_is_bounded(value, expected):
    environ = {} if value is None else {"WEAVE_WORKER_CAPACITY": value}
    assert fixture().capacity(environ) == expected
    for refused in ("0", "9", "two"):
        with pytest.raises(ValueError):
            fixture().capacity({"WEAVE_WORKER_CAPACITY": refused})


async def test_the_token_relay_forwards_only_to_an_approved_bridge_origin():
    """OAuth clients accept plain HTTP only on loopback, so the worker reaches Keycloak on the platform network
    through a loopback relay that checks the destination against the worker's private-origin entries."""
    import asyncio

    from firefly_weave import private_origins as po

    async def echo(reader, writer):
        writer.write(b"pong:" + await reader.read(4))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    module = fixture()
    allowed = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [
            po.PrivateOrigin(
                origin=f"http://127.0.0.1:{port}",
                purpose="worker-auth",
                networks=("127.0.0.1/32",),
                credentials="loopback",
            )
        ]
    )
    with po.installed(allowed):
        relay = await module.start_relay(f"127.0.0.1:{port}", listen=0)
        relay_port = relay.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(9) == b"pong:ping"
        writer.close()
        relay.close()
    with po.installed(po.PrivateOrigins.empty()):
        relay = await module.start_relay(f"127.0.0.1:{port}", listen=0)
        relay_port = relay.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"ping")
        await writer.drain()
        # Without an entry for the destination, the relay closes the connection and sends nothing.
        assert await reader.read(9) == b""
        writer.close()
        relay.close()
    server.close()


async def _echo_server():
    import asyncio

    async def echo(reader, writer):
        writer.write(b"pong:" + await reader.read(4))
        await writer.drain()
        writer.close()

    return await asyncio.start_server(echo, "127.0.0.1", 0)


async def _ping(port):
    import asyncio

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"ping")
        await writer.drain()
        return await reader.read(9)
    except ConnectionResetError:
        # A refused connection may be closed while the ping is still on its way.
        return b""
    finally:
        writer.close()


def _entry(origin, credentials):
    from firefly_weave import private_origins as po

    return po.PrivateOrigin(origin=origin, purpose="worker-auth", networks=("127.0.0.1/32",), credentials=credentials)


async def test_the_token_relay_listens_on_loopback_only():
    from firefly_weave import private_origins as po

    module = fixture()
    with po.installed(po.PrivateOrigins.empty()):
        relay = await module.start_relay("127.0.0.1:9", listen=0)
        assert [item.getsockname()[0] for item in relay.sockets] == ["127.0.0.1"]
        relay.close()


async def test_the_token_relay_connects_to_the_address_it_checked_never_the_name_again(monkeypatch):
    """A name that resolves elsewhere after the check is never used: the relay connects to the checked address."""
    import asyncio
    import socket

    from firefly_weave import private_origins as po

    server = await _echo_server()
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    answers = ["127.0.0.1", "192.0.2.1"]

    async def resolve(host, port_number, *args, **kwargs):
        if host != "keycloak.test":
            return await type(loop).getaddrinfo(loop, host, port_number, *args, **kwargs)
        address = answers.pop(0) if len(answers) > 1 else answers[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port_number))]

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    module = fixture()
    allowed = po.PrivateOrigins(platform=po.PLATFORM).with_entries([_entry(f"http://keycloak.test:{port}", "bridge")])
    with po.installed(allowed):
        relay = await module.start_relay(f"keycloak.test:{port}", listen=0)
        assert await asyncio.wait_for(_ping(relay.sockets[0].getsockname()[1]), 5) == b"pong:ping"
        relay.close()
    server.close()


async def test_the_token_relay_carries_credentials_only_where_an_entry_allows_them(caplog):
    """The relay carries a client secret: an entry that allows no credentials over plain HTTP is refused, a bridge
    entry of the local-development policy passes, and a refusal is logged with its reason and a count only."""
    import logging

    from firefly_weave import private_origins as po

    server = await _echo_server()
    port = server.sockets[0].getsockname()[1]
    module = fixture()
    plain = po.PrivateOrigins(platform=po.PLATFORM).with_entries([_entry(f"http://127.0.0.1:{port}", "none")])
    with po.installed(plain):
        relay = await module.start_relay(f"127.0.0.1:{port}", listen=0)
        with caplog.at_level(logging.WARNING):
            assert await _ping(relay.sockets[0].getsockname()[1]) == b""
            assert await _ping(relay.sockets[0].getsockname()[1]) == b""
        relay.close()
    messages = [record.getMessage() for record in caplog.records if record.name == "weave.operations_worker"]
    assert messages == [
        "The token relay refused a connection (credentials); 1 refused so far",
        "The token relay refused a connection (credentials); 2 refused so far",
    ]
    assert all("127.0.0.1" not in message and str(port) not in message for message in messages)
    bridge = po.PrivateOrigins(platform=po.PLATFORM).with_entries([_entry(f"http://127.0.0.1:{port}", "bridge")])
    with po.installed(bridge):
        relay = await module.start_relay(f"127.0.0.1:{port}", listen=0)
        assert await _ping(relay.sockets[0].getsockname()[1]) == b"pong:ping"
        relay.close()
    server.close()
