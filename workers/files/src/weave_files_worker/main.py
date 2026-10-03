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

"""Explicit worker startup and reproducible public catalog exports."""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import re
import signal
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.file_connectors import (
    FILE_DESCRIPTORS,
    NAMES,
    OPERATIONS,
    action_definition,
    task_capability,
)
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker
from pydantic import BaseModel, ConfigDict, Field

from weave_files_worker.handler import FileTaskHandler
from weave_files_worker.policy import WorkerPolicy

CAPABILITIES = [f"{name}.{operation}@1.0.0" for name in NAMES for operation in OPERATIONS]


class PolicyFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    origins: list[str] = Field(min_length=1, max_length=100)
    download_origins: list[str] = Field(default_factory=list, alias="downloadOrigins", max_length=100)
    private_networks: list[str] = Field(default_factory=list, alias="privateNetworks", max_length=100)
    allow_cleartext_ftp: bool = Field(default=False, alias="allowCleartextFtp")
    allow_non_atomic_ftp_destinations: bool = Field(default=False, alias="allowNonAtomicFtpDestinations")


def read_policy(path: Path) -> WorkerPolicy:
    if path.stat().st_size > 65536:
        raise ValueError("Worker policy is too large")
    data = PolicyFile.model_validate_json(path.read_bytes())
    policy = WorkerPolicy(
        frozenset(data.origins),
        frozenset(data.download_origins),
        tuple(data.private_networks),
        data.allow_cleartext_ftp,
        data.allow_non_atomic_ftp_destinations,
    )
    for value in policy.origins:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", urlsplit(value).hostname or ""):
            raise ValueError("Use exact destination hosts")
        policy.destination(value)
    for value in policy.download_origins:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", urlsplit(value).hostname or ""):
            raise ValueError("Use exact download hosts")
        policy.download_url(value)
        if urlsplit(value).path or urlsplit(value).query:
            raise ValueError("Use a download origin without a path")
    for value in policy.private_networks:
        ipaddress.ip_network(value)
    return policy


class TokenFileAuth(httpx.Auth):
    def __init__(self, path: Path):
        self.path = path

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.path.stat().st_size > 16384:
            raise ValueError("Invalid worker token")
        token = self.path.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise ValueError("Invalid worker token")
        request.headers["authorization"] = "Bearer " + token
        yield request


async def main() -> None:
    policy = read_policy(Path(os.environ["WEAVE_FILES_POLICY_FILE"]))
    base = os.environ["WEAVE_API_URL"].rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Worker API requires HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid worker API URL")
    prefix = os.environ["WEAVE_ENVIRONMENT_URL"].rstrip("/")
    if not prefix.startswith("/") or prefix.startswith("//") or any(c in prefix for c in "?#\\"):
        raise ValueError("Use an environment API path")
    # Provider libraries may log remote paths or signed URLs at debug level.
    for name in ("aioftp", "asyncssh", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    async with httpx.AsyncClient(
        base_url=base,
        auth=TokenFileAuth(Path(os.environ["WEAVE_WORKER_TOKEN_FILE"])),
        timeout=15,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.post(
            prefix + "/workers",
            json={"release_id": os.environ["WEAVE_WORKER_RELEASE_ID"], "task_types": CAPABILITIES, "capacity": 1},
        )
        response.raise_for_status()
        transport = WorkerTransport(client, prefix, UUID(response.json()["id"]))
        handler = FileTaskHandler(transport, policy)
        worker = Worker(transport, {capability: handler for capability in CAPABILITIES}, 1)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, lambda: asyncio.create_task(worker.stop()))
        await worker.run()


def run() -> None:
    parser = argparse.ArgumentParser(description="Run a lease-bound file transfer worker.")
    parser.add_argument("--catalog", action="store_true", help="Print an offline catalog lock and exit.")
    parser.add_argument(
        "--release-manifest", action="store_true", help="Print task and credential capabilities and exit."
    )
    args = parser.parse_args()
    capabilities = [
        task_capability(name, operation).model_dump(by_alias=True) for name in NAMES for operation in OPERATIONS
    ]
    if args.catalog:
        documents = [descriptor.manifest.value for descriptor in FILE_DESCRIPTORS.values()]
        documents.extend(action_definition(name, operation) for name in NAMES for operation in OPERATIONS)
        normalized = [load_definition(value).model_dump(by_alias=True) for value in documents]
        print(
            json.dumps(
                {
                    "definitions": [
                        {"document": value, "digest": FrozenDocument.from_value(value).digest} for value in normalized
                    ],
                    "tasks": capabilities,
                    "adapters": list(NAMES),
                    "schemas": {},
                }
            )
        )
        return
    if args.release_manifest:
        print(json.dumps({"capabilities": capabilities, "credential_capabilities": CAPABILITIES}))
        return
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        print("File worker stopped after an operation failed.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
