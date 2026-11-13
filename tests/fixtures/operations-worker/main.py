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
import base64
import ipaddress
import json
import os
import signal
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from firefly_weave import private_origins
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.sdk.token_relay import start_relay
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker
from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider, WorkerTokenAuth, private_transport
from firefly_weave.sdk.worker_identity import worker_identity
from firefly_weave.sdk.worker_registration import register_worker_http

TASK_TYPE = "example-sleep@1.0.0"
MANIFEST = Path(__file__).with_name("manifest.json")
# How long the closed probe waits for each address before it counts the address as closed.
PROBE_SECONDS = 3.0


def capacity(environ: Mapping[str, str]) -> int:
    """Concurrent tasks, 1–8; two unless WEAVE_WORKER_CAPACITY says otherwise."""
    raw = environ.get("WEAVE_WORKER_CAPACITY", "2")
    if not raw.isdigit() or not 1 <= int(raw) <= 8:
        raise ValueError("WEAVE_WORKER_CAPACITY must be a whole number from 1 to 8")
    return int(raw)


async def sleep(lease: Any) -> dict[str, int]:
    if lease.input.get("fail") == "unknown" and os.environ.get("WEAVE_FIXTURE_FAULTS") == "1":
        # A test fault: report an outcome the platform cannot know, so it opens an incident. Only a fixture
        # service started with WEAVE_FIXTURE_FAULTS=1 does; every other fixture worker ignores the field and
        # sleeps, so retrying the step on one of them succeeds.
        raise ConnectorFailure("FIXTURE_FAULT", "unknown")
    milliseconds = int(lease.input["ms"])
    await asyncio.sleep(milliseconds / 1000)
    return {"slept_ms": milliseconds}


async def register(
    client: httpx.AsyncClient, prefix: str, body: dict[str, Any], *, seconds: float = 120, pause: float = 2
) -> httpx.Response:
    """Register, trying again while a busy platform answers 429 (out of capacity), for up to `seconds`."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            return await register_worker_http(client, prefix, body)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 429 or asyncio.get_running_loop().time() + pause > deadline:
                raise
        await asyncio.sleep(pause)


def failure(error: BaseException) -> str:
    """The failure's kind, HTTP status and the code that raised it: never its message or a URL."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    found = traceback.extract_tb(error.__traceback__)
    place = f" at {found[-1].name}" if found else ""
    status = f" (HTTP {error.response.status_code})" if isinstance(error, httpx.HTTPStatusError) else ""
    return f"{type(error).__name__}{status}{place}"


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
        response = await register(
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


async def probe_token(environ: Mapping[str, str]) -> str:
    """Sign in exactly as the worker does and give only the token's issuer: the token, its other claims and the
    client secret never leave the pod."""
    private_origins.install(private_origins.load())
    relay = environ.get("WEAVE_TOKEN_RELAY")
    if relay:
        await start_relay(relay)
    api = environ["WEAVE_API_URL"]
    provider = ClientCredentialsTokenProvider.from_file(Path(environ["WEAVE_WORKER_OAUTH_CONFIG_FILE"]), api)
    payload = (await provider.get_access_token(api)).split(".")[1]
    issuer = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["iss"]
    if not isinstance(issuer, str):
        raise ValueError("The token names no issuer")
    return json.dumps({"iss": issuer})


def _addresses(values: list[str]) -> list[tuple[str, int]] | None:
    """Literal IP:port pairs; a name could fail to resolve and look closed, so names are refused."""
    found = []
    for value in values:
        host, separator, port = value.rpartition(":")
        try:
            address = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
        except ValueError:
            return None
        if not separator or not port.isdigit() or not 1 <= int(port) <= 65535:
            return None
        found.append((str(address), int(port)))
    return found


async def _answers(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), PROBE_SECONDS)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


async def probe_closed(values: list[str]) -> int:
    """0 when every address refuses or doesn't answer within PROBE_SECONDS, 1 when one answers, 2 for bad input."""
    addresses = _addresses(values)
    if not addresses:
        return 2
    answered = await asyncio.gather(*(_answers(host, port) for host, port in addresses))
    return 1 if any(answered) else 0


def cli(argv: list[str]) -> int:
    if argv == ["--release-manifest"]:
        sys.stdout.write(MANIFEST.read_text() if MANIFEST.exists() else _packaged_manifest())
        return 0
    if argv[:1] == ["--probe-closed"]:
        return asyncio.run(probe_closed(argv[1:]))
    if argv == ["--probe-token"]:
        try:
            print(asyncio.run(probe_token(os.environ)))
        except Exception as error:
            print("The token probe failed: " + failure(error), file=sys.stderr)
            return 1
        return 0
    try:
        asyncio.run(main())
    except Exception as error:
        # HTTP errors may carry URLs or response data; only the kind, status and place are printed.
        print("The fixture worker stopped after an operation failed: " + failure(error), file=sys.stderr)
        return 1
    return 0


def _packaged_manifest() -> str:
    """The manifest the image keeps next to the worker under its release input name."""
    return (Path(__file__).parent / "operations-worker-manifest.json").read_text()


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
