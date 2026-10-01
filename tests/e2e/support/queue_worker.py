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

"""Test-only separate installed worker process for fixed queue measurement."""

import argparse
import asyncio
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

import httpx

from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker


def failure_summary(error):
    """Expose classification only; exception messages and response bodies may contain secrets."""
    result = {"failed": True, "error_type": type(error).__name__}
    if isinstance(error, httpx.HTTPStatusError):
        operation = error.request.url.path.rsplit("/", 1)[-1]
        if operation not in {"claim", "heartbeat", "complete", "fail", "credentials", "workers"}:
            operation = "other"
        try:
            body = error.response.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or code not in {"WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"}:
            code = None
        result.update(status=error.response.status_code, operation=operation, code=code)
    return result


async def main(config):
    spec = importlib.util.spec_from_file_location(
        "queue_installed_verifier", Path(__file__).with_name("installed_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proof = module.verify_install(Path(config["wheel"]), config["wheel_sha256"])
    assert all("DATABASE" not in name and not name.startswith("PG") for name in os.environ)
    async with httpx.AsyncClient(trust_env=False, timeout=15) as identity:
        response = await identity.post(
            config["token_url"],
            data={"grant_type": "client_credentials"},
            auth=("weave-worker", os.environ["WEAVE_WORKER_SECRET"]),
        )
        response.raise_for_status()
        token = response.json()
        assert token["expires_in"] >= 300, "Measured trial requires a sufficient finite token lifetime"
    async with (
        httpx.AsyncClient(
            base_url=config["api_url"],
            trust_env=False,
            timeout=15,
            headers={"Authorization": "Bearer " + token["access_token"]},
        ) as client,
        httpx.AsyncClient(trust_env=False, timeout=15) as receiver,
    ):
        response = await client.post(
            config["environment_url"] + "/workers",
            json={
                "release_id": config["release_id"],
                "task_types": [config["capability"]],
                "capacity": config["concurrency"],
            },
        )
        response.raise_for_status()
        worker_id = UUID(response.json()["id"])
        transport = WorkerTransport(client, config["environment_url"], worker_id)

        async def handle(lease):
            response = await receiver.post(
                config["effect_url"], json=lease.input, headers={"Idempotency-Key": lease.operation_key}
            )
            response.raise_for_status()
            return response.json()

        worker = Worker(transport, {config["capability"]: handle}, config["concurrency"])
        loop = asyncio.get_running_loop()

        async def stop(signum):
            await worker.stop()
            print(json.dumps({"stopping": True, "signal": signum, "active": len(worker.active)}), flush=True)

        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, lambda n=name: asyncio.create_task(stop(n)))
        print(json.dumps({"ready": True, "worker_id": str(worker_id), **proof}), flush=True)
        async with asyncio.timeout(min(token["expires_in"] - 30, 240)):
            await worker.run()
        print(json.dumps({"drained": not worker.active, "worker_id": str(worker_id)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(json.loads(args.config.read_text())))
    except Exception as error:
        print(json.dumps(failure_summary(error)), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
