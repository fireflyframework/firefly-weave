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

"""Sleep fixture worker for local Operations: example-sleep@1.0.0 sleeps the requested time and reports it.

It signs in with a mounted client-credentials file, reaches the API only through private-origin entries the
platform wrote, registers with its own host so placement and drains can find it, and finishes its active tasks
when stopped. It never accepts database or administrator settings.
"""

import asyncio
import os
import signal
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from firefly_weave import private_origins
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker
from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider, WorkerTokenAuth, private_transport
from firefly_weave.sdk.worker_identity import worker_identity
from firefly_weave.sdk.worker_registration import register_worker_http

TASK_TYPE = "example-sleep@1.0.0"
MANIFEST = Path(__file__).with_name("manifest.json")


def capacity(environ: Mapping[str, str]) -> int:
    """Concurrent tasks, 1–8; two unless WEAVE_WORKER_CAPACITY says otherwise."""
    raw = environ.get("WEAVE_WORKER_CAPACITY", "2")
    if not raw.isdigit() or not 1 <= int(raw) <= 8:
        raise ValueError("WEAVE_WORKER_CAPACITY must be a whole number from 1 to 8")
    return int(raw)


async def sleep(lease: Any) -> dict[str, int]:
    milliseconds = int(lease.input["ms"])
    await asyncio.sleep(milliseconds / 1000)
    return {"slept_ms": milliseconds}


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def start_relay(destination: str, *, listen: int = 8080) -> asyncio.Server:
    """A loopback relay to Keycloak on the platform network, for the token endpoint only.

    OAuth clients accept plain HTTP only on loopback, so the worker signs in through 127.0.0.1. Each
    connection is checked against the worker's private-origin entries for worker-auth before anything is
    forwarded, and goes to the checked address.
    """
    host, _, port = destination.rpartition(":")

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
            addresses = sorted({item[4][0] for item in found})
            private_origins.active().check("worker-auth", f"http://{host}:{port}/", addresses, sends_credentials=True)
            upstream_reader, upstream_writer = await asyncio.open_connection(addresses[0], int(port))
        except Exception:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer), return_exceptions=True)

    return await asyncio.start_server(relay, "127.0.0.1", listen)


async def main() -> None:
    forbidden = [key for key in os.environ if "DATABASE" in key or key.startswith(("PG", "WEAVE_KC_ADMIN"))]
    if forbidden:
        raise RuntimeError("The fixture worker must not receive database or administrator settings")
    concurrent = capacity(os.environ)
    private_origins.install(private_origins.load())
    relay = os.environ.get("WEAVE_TOKEN_RELAY")
    if relay:
        await start_relay(relay)
    api = os.environ["WEAVE_API_URL"]
    provider = ClientCredentialsTokenProvider.from_file(Path(os.environ["WEAVE_WORKER_OAUTH_CONFIG_FILE"]), api)
    async with httpx.AsyncClient(
        base_url=api,
        timeout=10,
        trust_env=False,
        auth=WorkerTokenAuth(provider),
        transport=private_transport(api, "platform-api"),
    ) as client:
        prefix = os.environ["WEAVE_ENVIRONMENT_URL"]
        response = await register_worker_http(
            client,
            prefix,
            {
                "release_id": os.environ["WEAVE_WORKER_RELEASE_ID"],
                "task_types": [TASK_TYPE],
                "capacity": concurrent,
                "identity": worker_identity(runtime="container").model_dump(mode="json"),
            },
        )
        worker = Worker(WorkerTransport(client, prefix, UUID(response.json()["id"])), {TASK_TYPE: sleep}, concurrent)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            # Stopping claims no new task and finishes the active ones.
            loop.add_signal_handler(signum, lambda: asyncio.create_task(worker.stop()))
        await worker.run()


def cli(argv: list[str]) -> int:
    if argv == ["--release-manifest"]:
        sys.stdout.write(MANIFEST.read_text() if MANIFEST.exists() else _packaged_manifest())
        return 0
    try:
        asyncio.run(main())
    except Exception:
        # HTTP errors may carry URLs or response data; keep the output free of them.
        print("The fixture worker stopped after an operation failed", file=sys.stderr)
        return 1
    return 0


def _packaged_manifest() -> str:
    """The manifest the image keeps next to the worker under its release input name."""
    return (Path(__file__).parent / "operations-worker-manifest.json").read_text()


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
