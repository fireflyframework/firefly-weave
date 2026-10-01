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

"""Remote SDK worker. Only HTTP and explicit environment configuration are used."""

import asyncio
import json
import math
import os
import signal
import sys
import time
from uuid import UUID

import httpx

from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker


def token_drain_delay(expires_in, *, elapsed):
    """Reserve the 180-second task contract plus a 30-second credential margin."""
    if type(expires_in) not in (int, float) or not math.isfinite(expires_in):
        raise ValueError("Identity response requires a finite token lifetime")
    delay = expires_in - elapsed - 210
    if delay <= 0:
        raise ValueError("Token lifetime is too short for a safe worker drain")
    return delay


async def main():
    forbidden = [key for key in os.environ if "DATABASE" in key or key.startswith(("PG", "WEAVE_KC_ADMIN"))]
    if forbidden:
        raise RuntimeError("Remote worker must not receive database or administrator credentials")
    token_started = time.monotonic()
    async with httpx.AsyncClient(timeout=10, trust_env=False) as identity:
        response = await identity.post(
            os.environ["WEAVE_TOKEN_URL"],
            data={"grant_type": "client_credentials"},
            auth=("weave-worker", os.environ["WEAVE_WORKER_SECRET"]),
        )
        if response.status_code != 200:
            raise RuntimeError("Worker token acquisition failed")
        token = response.json()["access_token"]
        expires_in = response.json().get("expires_in")
        token_drain_delay(expires_in, elapsed=time.monotonic() - token_started)
    async with httpx.AsyncClient(
        base_url=os.environ["WEAVE_API_URL"], timeout=10, trust_env=False, headers={"Authorization": "Bearer " + token}
    ) as client:
        prefix = os.environ["WEAVE_ENVIRONMENT_URL"]
        response = await client.post(
            prefix + "/workers",
            json={
                "release_id": os.environ["WEAVE_WORKER_RELEASE_ID"],
                "task_types": ["example-record@1.0.0"],
                "capacity": 1,
            },
        )
        response.raise_for_status()
        transport = WorkerTransport(client, prefix, UUID(response.json()["id"]))

        async def record(lease):
            async with httpx.AsyncClient(timeout=10, trust_env=False) as target:
                response = await target.post(
                    os.environ["WEAVE_EFFECT_URL"], json=lease.input, headers={"Idempotency-Key": lease.operation_key}
                )
                response.raise_for_status()
                output = response.json()
            print(
                json.dumps(
                    {
                        "generation": lease.proof.generation,
                        "operation_key": lease.operation_key,
                        "database_credentials": False,
                        "server_imports": sorted(
                            name
                            for name in sys.modules
                            if name.startswith(
                                ("sqlalchemy", "asyncpg", "firefly_weave.app", "firefly_weave.persistence")
                            )
                        ),
                    }
                ),
                flush=True,
            )
            return output

        worker = Worker(transport, {"example-record@1.0.0": record}, 1)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, lambda: asyncio.create_task(worker.stop()))

        drain_delay = token_drain_delay(expires_in, elapsed=time.monotonic() - token_started)

        async def expire():
            await asyncio.sleep(drain_delay)
            await worker.stop()

        expiration = asyncio.create_task(expire())
        try:
            # Finite invocation: the supervisor may restart after this bounded drain.
            async with asyncio.timeout(expires_in - (time.monotonic() - token_started) - 15):
                await worker.run()
        finally:
            expiration.cancel()
            await asyncio.gather(expiration, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # HTTP exceptions may contain URLs or response data; keep process output non-secret.
        print("Remote worker stopped after an operation failed", file=sys.stderr)
        raise SystemExit(1) from None
