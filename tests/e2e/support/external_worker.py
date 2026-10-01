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

"""Installed remote protocol worker used only by deterministic fault acceptance.

No runtime/database imports occur here. The receiver's local-only effect probe
proves durable acceptance when an HTTP response is intentionally lost.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit


def sibling(name):
    spec = importlib.util.spec_from_file_location("weave_fault_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def execute(config, channel):
    import httpx

    headers = {"Authorization": "Bearer " + config["token"]}
    async with httpx.AsyncClient(base_url=config["api_url"], headers=headers, trust_env=False, timeout=5) as client:
        async with asyncio.timeout(90):
            while True:
                response = await client.post(
                    config["environment"] + "/tasks/claim",
                    json={
                        "worker_id": config["worker_id"],
                        "limit": 1,
                    },
                )
                response.raise_for_status()
                leases = response.json()
                if leases:
                    assert len(leases) == 1
                    lease = leases[0]
                    break
                await asyncio.sleep(0.2)
            detail = {
                "case": config["case"],
                "scope": config["scope"],
                "task_id": lease["proof"]["task_id"],
                "generation": lease["proof"]["generation"],
                "operation_key": lease["operation_key"],
            }
            with os.fdopen(os.open(config["lease_result"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                json.dump(lease, stream)
            phase = config.get("phase")
            raw = json.dumps(lease["input"]).encode()
            effect_headers = {"Idempotency-Key": lease["operation_key"], "Content-Type": "application/json"}
            if phase == "external_request_in_flight":
                url = urlsplit(config["receiver"])
                reader, writer = await asyncio.open_connection(url.hostname, url.port)
                writer.write(
                    (
                        "POST /effect HTTP/1.1\r\nHost: "
                        + url.netloc
                        + "\r\nIdempotency-Key: "
                        + lease["operation_key"]
                        + "\r\nContent-Length: "
                        + str(len(raw))
                        + "\r\nConnection: close\r\n\r\n"
                    ).encode()
                )
                await writer.drain()
                await channel.pause(phase, detail)
                writer.write(raw)
                await writer.drain()
                await reader.read()
                writer.close()
                await writer.wait_closed()
                output = {}
            else:
                path = "/effect-loss" if phase == "after_external_response_loss" else "/effect"
                async with httpx.AsyncClient(trust_env=False, timeout=5) as remote:
                    try:
                        response = await remote.post(config["receiver"] + path, content=raw, headers=effect_headers)
                        response.raise_for_status()
                        output = response.json()
                    except httpx.RemoteProtocolError:
                        if phase != "after_external_response_loss":
                            raise
                        probe = await remote.get(
                            config["receiver"] + "/accepted", params={"key": lease["operation_key"]}
                        )
                        probe.raise_for_status()
                        assert probe.json() == {"accepted": True}
                        output = {}
                if phase:
                    await channel.pause(phase, detail)
            response = await client.post(
                config["environment"] + "/tasks/complete",
                json={
                    "lease": lease["proof"],
                    "completion_id": config["completion_id"],
                    "output": output,
                },
            )
            response.raise_for_status()
            with os.fdopen(os.open(config["result"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                json.dump({"detail": detail, "receipt": response.json()}, stream)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--barrier-fd", type=int)
    args = parser.parse_args()
    assert sys.flags.isolated
    assert args.config.stat().st_size <= 32768
    config = json.loads(args.config.read_bytes())
    sibling("installed_api").verify_install(Path(config["wheel"]), config["wheel_sha256"])
    channel = None
    if args.barrier_fd is not None:
        channel = sibling("barriers").BarrierChannel(socket.socket(fileno=args.barrier_fd), config["case"])
    asyncio.run(execute(config, channel))


if __name__ == "__main__":
    main()
