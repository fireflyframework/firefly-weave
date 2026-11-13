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

"""A loopback relay to the platform's Keycloak, for clients that sign in over plain HTTP on a private network.

Workers and in-cluster runners reach Keycloak on the platform or cluster network, where only plain HTTP is
offered, but OAuth clients take plain HTTP only on loopback. They sign in through 127.0.0.1 instead, and this relay
forwards each connection to the one destination it was started for, after the private-origin check.
"""

import asyncio
import logging
import socket

from firefly_weave import private_origins

LOG = logging.getLogger("weave.token_relay")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def start_relay(destination: str, *, listen: int = 8080) -> asyncio.Server:
    """A loopback relay to Keycloak on the platform network, so a client can sign in over plain HTTP.

    OAuth clients accept plain HTTP only on loopback, so the client signs in through 127.0.0.1. The relay
    forwards whole connections to the one destination its environment names (any Keycloak path, which gives
    the client no reach it lacks: it is on the platform network). Each connection is checked against the
    process's private-origin entries for worker-auth, as one that carries credentials, before anything is
    forwarded, and goes to the checked address, never to the name resolved again. A refusal is logged with
    its reason and a count only.
    """
    host, _, port = destination.rpartition(":")
    refused = 0

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal refused
        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
            addresses = sorted({item[4][0] for item in found})
            private_origins.active().check("worker-auth", f"http://{host}:{port}/", addresses, sends_credentials=True)
            upstream_reader, upstream_writer = await asyncio.open_connection(addresses[0], int(port))
        except Exception as error:
            refused += 1
            reason = error.reason if isinstance(error, private_origins.PrivateOriginDenied) else "unreachable"
            LOG.warning("The token relay refused a connection (%s); %d refused so far", reason, refused)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer), return_exceptions=True)

    return await asyncio.start_server(relay, "127.0.0.1", listen)
