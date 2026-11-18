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

"""A connector's HTTP call writes one log entry for the task it runs: method, host, status and duration only."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient
from firefly_weave.contracts.workers import LeaseProof, TaskLease, TaskLogAccepted
from firefly_weave.sdk.worker import Worker

SECRET = "synthetic-" + "path-token"


@asynccontextmanager
async def endpoint(status: bytes):
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 " + status + b"\r\nSet-Cookie: session=" + SECRET.encode() + b"\r\n")
            writer.write(b"Content-Length: 2\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


class Transport:
    def __init__(self) -> None:
        self.entries = []

    async def heartbeat(self, proof):
        await asyncio.Event().wait()

    async def log(self, request):
        self.entries.extend(request.entries)
        return TaskLogAccepted(accepted=len(request.entries), dropped=0)

    async def complete(self, proof, completion_id, output):
        pass


def leased() -> TaskLease:
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        input=1,
        operation_key="stable",
        deadline=now + timedelta(seconds=5),
        expires_at=now + timedelta(seconds=5),
        capability="echo@1.2.0",
        worker_release_id=uuid4(),
    )


async def call(status: bytes) -> tuple[Transport, int]:
    transport = Transport()
    async with endpoint(status) as port:
        origin = f"http://127.0.0.1:{port}"

        async def handler(task):
            await SecureHttpClient().request_bounded(
                "POST",
                f"{origin}/v1/{SECRET}/orders?token={SECRET}",
                max_response_bytes=20,
                headers={"Authorization": "Bearer " + SECRET},
                content=json.dumps({"password": SECRET}).encode(),
                egress_policy=EgressPolicy((origin,), ("127.0.0.0/8",)),
            )
            return "done"

        await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    return transport, port


async def test_a_request_logs_its_host_and_status_never_its_path_query_or_headers():
    transport, port = await call(b"201 Created")
    (entry,) = transport.entries
    assert (entry.source, entry.level, entry.code) == ("connector", "info", "HTTP.RESPONSE")
    assert set(entry.fields) == {"method", "host", "status", "duration_ms"}
    assert entry.fields["method"] == "POST"
    assert entry.fields["host"] == f"127.0.0.1:{port}"
    assert entry.fields["status"] == 201
    assert isinstance(entry.fields["duration_ms"], int) and entry.fields["duration_ms"] >= 0
    assert entry.message == f"POST 127.0.0.1:{port} answered 201"
    written = entry.model_dump_json()
    for left_out in (SECRET, "/v1", "orders", "token", "Bearer", "session"):
        assert left_out not in written


async def test_an_error_status_is_a_warning():
    transport, _ = await call(b"503 Service Unavailable")
    (entry,) = transport.entries
    assert (entry.level, entry.fields["status"]) == ("warning", 503)
