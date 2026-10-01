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

"""Own HTTP diagnostics before INFO/DEBUG handlers without muting other tasks."""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient


@asynccontextmanager
async def endpoint():
    received = []

    async def handle(reader, writer):
        try:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nX-Echo: synthetic-path-token\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", received
    finally:
        server.close()
        await server.wait_closed()


async def test_real_transport_never_logs_secret_url_or_response_headers(caplog):
    caplog.set_level(logging.DEBUG)
    async with endpoint() as (url, received):
        response = await SecureHttpClient().request_bounded(
            "GET",
            url + "/botsynthetic-path-token/sendMessage",
            max_response_bytes=20,
            egress_policy=EgressPolicy((url,), ("127.0.0.0/8",)),
        )
    assert response.json() == {}
    assert len(received) == 1
    assert b"synthetic-path-token" in received[0]
    assert "synthetic-path-token" not in caplog.text
    logging.getLogger("httpx").info("unrelated-after")
    assert "unrelated-after" in caplog.text


async def test_nested_overlapping_contexts_restore_logs_and_cancelled_child_cleanup(caplog):
    from firefly_weave.connectors.http_logging import protected_http_diagnostics

    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    release = asyncio.Event()
    emitters = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks")

    async def owned():
        with protected_http_diagnostics():
            for name in emitters:
                logging.getLogger(name).warning("owned-secret", extra={"secret": "owned-secret"})
            with protected_http_diagnostics():
                logging.getLogger("httpx").info("nested-secret")
            logging.getLogger("httpx").info("still-owned-secret")
            started.set()
            try:
                await release.wait()
            finally:

                async def cleanup():
                    logging.getLogger("httpcore.http11").debug("cleanup-secret")

                await asyncio.create_task(cleanup())

    one = asyncio.create_task(owned())
    two = asyncio.create_task(owned())
    await started.wait()
    logging.getLogger("httpx").info("unrelated-overlap")
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    release.set()
    await two
    logging.getLogger("httpx").info("unrelated-restored")
    assert "secret" not in caplog.text
    assert "unrelated-overlap" in caplog.text
    assert "unrelated-restored" in caplog.text


async def test_transport_cancellation_restores_context_and_preserves_exception(caplog):
    caplog.set_level(logging.DEBUG)
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    try:
        task = asyncio.create_task(
            SecureHttpClient().request_bounded(
                "GET",
                url + "/synthetic-path-token",
                max_response_bytes=20,
                egress_policy=EgressPolicy((url,), ("127.0.0.0/8",)),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
        logging.getLogger("httpx").info("after-cancel")
        assert "synthetic-path-token" not in caplog.text
        assert "after-cancel" in caplog.text
    finally:
        listener.close()
        await listener.wait_closed()
