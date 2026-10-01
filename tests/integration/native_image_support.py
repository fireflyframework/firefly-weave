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

"""Private native-image evidence and exact-image bridge target checks for integration tests."""

import asyncio
import ipaddress
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from firefly_weave.sdk.deployment import run_command


def write_image_proof(base: Path, kind: str, value: dict) -> Path:
    """Keep each image gate's evidence without replacing another gate or a prior attempt."""
    if kind not in {"http", "sql", "kafka"}:
        raise ValueError("Unknown native image proof kind")
    path = base.with_name(base.stem + "-" + kind + base.suffix)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    return path


async def native_postgres_host(image: str) -> str:
    """Resolve and check the owned target from the same bridge namespace as the executor."""
    if os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") != "colima-weave-tests":
        raise ValueError("Native SQL probe requires owned colima-weave-tests context")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Native SQL probe requires an exact image ID")
    configured = os.environ.get("WEAVE_D1_NATIVE_PG_HOST")
    if configured is not None:
        try:
            if str(ipaddress.IPv4Address(configured)) != configured:
                raise ValueError
        except ValueError:
            raise ValueError("WEAVE_D1_NATIVE_PG_HOST must be a literal IPv4 address") from None
    target = configured if configured is not None else "host.docker.internal"
    docker = ["docker", "--context", "colima-weave-tests"]
    # The product pins literal IPs. Resolve inside the exact installed image;
    # never relax its policy or substitute the host machine's DNS answer.
    code = (
        "import ipaddress,socket,sys; "
        "address=str(ipaddress.IPv4Address(socket.gethostbyname(sys.argv[1]))); "
        "connection=socket.create_connection((address,55433),timeout=3); "
        "connection.close(); print(address)"
    )

    def probe() -> str:
        identifier = (
            run_command(
                [*docker, "create", "--name", "weave-d1-network-" + uuid4().hex, image, "python", "-c", code, target],
                timeout=15,
                limit=65536,
            )
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise ValueError("Native SQL probe returned an invalid owned container ID")
        try:
            value = run_command([*docker, "start", "--attach", identifier], timeout=15, limit=65536).decode().strip()
            address = str(ipaddress.IPv4Address(value))
            if configured is not None and address != configured:
                raise ValueError("Native SQL probe changed the explicit target")
            return address
        except Exception:
            raise RuntimeError("Native PostgreSQL target is unreachable or did not resolve to literal IPv4") from None
        finally:
            # Retain the exact owned container; stopping it also bounds a stalled DNS probe.
            run_command([*docker, "stop", "--time", "1", identifier], timeout=5, limit=65536)

    return await asyncio.to_thread(probe)
