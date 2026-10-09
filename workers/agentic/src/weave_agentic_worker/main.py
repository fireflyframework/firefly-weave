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

"""Run an independently operated worker against public Weave task APIs."""

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID

import httpx
from firefly_weave import private_origins
from firefly_weave.ai_policy import PolicyFile
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.private_origins import PrivateOrigins
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker
from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider, WorkerTokenAuth, private_transport

from weave_agentic_worker.handler import AgenticTaskHandler, WorkerPolicy

CAPABILITY = "weave-agentic.generate@1.0.0"
POLICY_ENV = "WEAVE_AGENTIC_POLICY_FILE"
MAX_CAPACITY = 16


def read_policy(path: Path, origins: PrivateOrigins | None = None) -> WorkerPolicy:
    """The worker's policy: its mounted AI policy file, re-read on change, and its private-origin policy."""
    allowed = origins if origins is not None else private_origins.active()
    return WorkerPolicy(source=PolicyFile(path, allowed), origins=allowed)


def capacity() -> int:
    """Concurrent tasks this worker accepts; a local Ollama serves one request at a time."""
    raw = os.environ.get("WEAVE_AGENTIC_CAPACITY", "1")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_CAPACITY:
        raise ValueError("WEAVE_AGENTIC_CAPACITY must be a whole number from 1 to 16")
    return int(raw)


class TokenFileAuth(httpx.Auth):
    def __init__(self, path: Path) -> None:
        self.path = path

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.path.stat().st_size > 16384:
            raise ValueError("Invalid worker token")
        token = self.path.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise ValueError("Invalid worker token")
        request.headers["authorization"] = "Bearer " + token
        yield request


def worker_auth(api_origin: str) -> httpx.Auth:
    token_file = os.environ.get("WEAVE_WORKER_TOKEN_FILE")
    oauth_file = os.environ.get("WEAVE_WORKER_OAUTH_CONFIG_FILE")
    if bool(token_file) == bool(oauth_file):
        raise ValueError("Configure exactly one worker authentication mode")
    if oauth_file:
        return WorkerTokenAuth(ClientCredentialsTokenProvider.from_file(Path(oauth_file), api_origin))
    assert token_file
    return TokenFileAuth(Path(token_file))


async def main() -> None:
    origins = private_origins.load()
    private_origins.install(origins)
    policy = read_policy(Path(os.environ[POLICY_ENV]), origins)
    api = os.environ["WEAVE_API_URL"]
    auth = worker_auth(api)
    slots = capacity()
    prefix = os.environ["WEAVE_ENVIRONMENT_URL"].rstrip("/")
    async with httpx.AsyncClient(
        base_url=api,
        auth=auth,
        timeout=10,
        trust_env=False,
        follow_redirects=False,
        # Plain HTTP to the API only through this process's platform-api entry, pinned and peer checked.
        transport=private_transport(api, "platform-api", max_connections=4),
    ) as client:
        response = await client.post(
            prefix + "/workers",
            json={"release_id": os.environ["WEAVE_WORKER_RELEASE_ID"], "task_types": [CAPABILITY], "capacity": slots},
        )
        response.raise_for_status()
        transport = WorkerTransport(client, prefix, UUID(response.json()["id"]))
        worker = Worker(transport, {CAPABILITY: AgenticTaskHandler(transport, policy)}, slots)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, lambda: asyncio.create_task(worker.stop()))
        await worker.run()


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Run the lease-bound Firefly Agentic worker using explicit environment configuration."
    )
    parser.add_argument("--catalog", action="store_true", help="Print the offline catalog lock and exit.")
    parser.add_argument(
        "--release-manifest", action="store_true", help="Print task and credential capabilities and exit."
    )
    args = parser.parse_args()
    if args.catalog:
        definitions = [
            load_definition(value).model_dump(by_alias=True)
            for value in (AGENTIC_DESCRIPTOR.manifest.value, action_definition())
        ]
        print(
            json.dumps(
                {
                    "definitions": [
                        {"document": value, "digest": FrozenDocument.from_value(value).digest} for value in definitions
                    ],
                    "tasks": [task_capability().model_dump(by_alias=True)],
                    "adapters": ["weave-agentic-provider"],
                    "schemas": {},
                }
            )
        )
        return
    if args.release_manifest:
        print(
            json.dumps(
                {"capabilities": [task_capability().model_dump(by_alias=True)], "credential_capabilities": [CAPABILITY]}
            )
        )
        return
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Agentic worker stopped after an operation failed.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
