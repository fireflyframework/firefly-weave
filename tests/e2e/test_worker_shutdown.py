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

"""Signal a separate installed worker while a real accepted HTTP effect is in flight."""

import asyncio
import json
import os
import signal
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


async def test_ctrl_c_drains_active_task_without_claiming_more(tmp_path):
    assert os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") == "colima-weave-tests", "Owned backend required"
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("drain_queue", ROOT / "tests/benchmarks/test_queue_load.py")
    queue = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = queue
    spec.loader.exec_module(queue)
    matrix = queue.load_matrix()
    destination = Path(os.environ.get("WEAVE_D5_EVIDENCE", str(tmp_path))) / "active-sigint-drain"
    destination.mkdir(mode=0o700)
    trial = matrix.InstalledSlice(destination)
    accepted, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects, errors = [], []
    server = None

    async def receiver(reader, writer):
        try:
            async with asyncio.timeout(20):
                head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
                headers = {k.lower(): v for line in head[1:] if ": " in line for k, v in [line.split(": ", 1)]}
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
                effects.append({"operation_key": headers["idempotency-key"], "input": body})
                accepted.set()
                await release.wait()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
                )
                await writer.drain()
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    try:
        await trial.bootstrap()
        name, worker_release, activation = await queue.prepare(trial)
        server = await asyncio.start_server(receiver, "127.0.0.1", 0, limit=16384)
        port = server.sockets[0].getsockname()[1]
        config = {
            "wheel": str(trial.artifact),
            "wheel_sha256": trial.artifact_sha,
            "api_url": trial.api_url,
            "environment_url": trial.host.environment_url,
            "token_url": trial.token_url,
            "release_id": worker_release["id"],
            "capability": name + "@1.0.0",
            "concurrency": 1,
            "effect_url": f"http://127.0.0.1:{port}/effect",
        }
        path = destination / "worker.json"
        matrix.private_json(path, config)
        process = await trial.spawn(
            "drain-worker",
            [
                os.environ["WEAVE_E2E_WORKER_PYTHON"],
                "-I",
                str(ROOT / "tests/e2e/support/queue_worker.py"),
                "--config",
                str(path),
            ],
            {**trial.base_env, "WEAVE_WORKER_SECRET": os.environ["WEAVE_WORKER_SECRET"]},
        )

        async def start(customer):
            return await trial.host.post(
                trial.host.environment_url + "/runs",
                {
                    "activation_id": activation["id"],
                    "input": {"customer": customer},
                    "correlation_key": str(uuid4()),
                },
            )

        first = await start("active-before-sigint")
        async with asyncio.timeout(15):
            await accepted.wait()
        process.send_signal(signal.SIGINT)
        async with asyncio.timeout(5):
            while True:
                records = [json.loads(line) for line in (destination / "drain-worker.log").read_text().splitlines()]
                stopping = [record for record in records if record.get("stopping")]
                if stopping:
                    assert stopping == [{"stopping": True, "signal": signal.SIGINT, "active": 1}]
                    break
                await asyncio.sleep(0.02)
        second = await start("pending-after-sigint")
        assert process.returncode is None
        release.set()
        assert await trial.owner.joined(process, 15) == 0
        await asyncio.wait_for(finished.wait(), 5)
        assert not errors and len(effects) == 1
        first_state = (await trial.client.get(trial.host.environment_url + "/runs/" + first["id"])).json()
        second_state = (await trial.client.get(trial.host.environment_url + "/runs/" + second["id"])).json()
        assert first_state["state"]["status"] == "succeeded"
        assert second_state["state"]["status"] == "waiting"
        assert '"drained": true' in (destination / "drain-worker.log").read_text()
        matrix.private_json(
            destination / "result.json",
            {
                "signal": "SIGINT",
                "active_drained": 1,
                "effects": effects,
                "unclaimed_run": second["id"],
                "wheel_sha256": trial.artifact_sha,
            },
        )
    finally:
        release.set()
        if server is not None:
            server.close()
            await server.wait_closed()
        await trial.close()
