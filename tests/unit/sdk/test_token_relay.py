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

"""The loopback token relay workers and in-cluster runners sign in through over plain HTTP."""

from firefly_weave.sdk import token_relay


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
    module = token_relay
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

    module = token_relay
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
    module = token_relay
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
    module = token_relay
    plain = po.PrivateOrigins(platform=po.PLATFORM).with_entries([_entry(f"http://127.0.0.1:{port}", "none")])
    with po.installed(plain):
        relay = await module.start_relay(f"127.0.0.1:{port}", listen=0)
        with caplog.at_level(logging.WARNING):
            assert await _ping(relay.sockets[0].getsockname()[1]) == b""
            assert await _ping(relay.sockets[0].getsockname()[1]) == b""
        relay.close()
    messages = [record.getMessage() for record in caplog.records if record.name == "weave.token_relay"]
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
