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

"""Inspect real exact-wheel containers with independent installed dependency closures."""

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.sdk.deployment import read_file, run_command, wheel_licenses

pytestmark = pytest.mark.e2e

PROBE = r"""
import asyncio,hashlib,importlib.metadata,importlib.util,json,os,pathlib,sys
import firefly_weave
mode,expected = sys.argv[1],json.loads(sys.argv[2])
root=pathlib.Path(firefly_weave.__file__).parent
assert 'site-packages' in root.parts
assert os.getuid()==65532
for name,digest in expected.items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
for name in ('sqlalchemy','asyncpg','alembic','starlette','uvicorn'):
    assert (importlib.util.find_spec(name) is not None)==(mode!='worker'),name
assert (importlib.util.find_spec('microsoft_agents') is not None)==(mode=='teams')
assert (importlib.util.find_spec('aiokafka') is not None)==(mode=='kafka')
assert importlib.metadata.version('pyfly')=='26.9.15'
if mode=='worker':
    from firefly_weave.sdk.worker import Worker
    class Empty:
        async def claim(self, maximum):
            await worker.stop()
            return []
    worker=Worker(Empty(),{},1)
    asyncio.run(worker.run())
proof=json.loads(pathlib.Path('/opt/weave/build.json').read_text())
print(json.dumps({'wheel_sha256':proof['wheel_sha256'],'package_members':len(expected),
    'uid':os.getuid(),'python':sys.version.split()[0],
    'packages':sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())}))
"""


@pytest.mark.parametrize(
    "closure,variable",
    [
        ("server", "WEAVE_E2E_IMAGE_ID"),
        ("worker", "WEAVE_E2E_WORKER_IMAGE_ID"),
        ("teams", "WEAVE_E2E_TEAMS_IMAGE_ID"),
        ("kafka", "WEAVE_E2E_KAFKA_IMAGE_ID"),
    ],
)
def test_actual_image_installs_exact_wheel_with_independent_closure(closure, variable, tmp_path):
    context = os.environ.get("WEAVE_TEST_DOCKER_CONTEXT")
    assert context == "colima-weave-tests", "Explicit owned Docker context is required"
    image = os.environ.get(variable, "")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image), "Every exact closure image is required"
    wheel = Path(os.environ.get("WEAVE_E2E_WHEEL", ""))
    assert wheel.is_file(), "Exact prepared wheel is required"
    wheel_data = read_file(wheel, 64 * 1024 * 1024)
    digest = hashlib.sha256(wheel_data).hexdigest()
    assert digest == os.environ.get("WEAVE_E2E_WHEEL_SHA256"), "Wheel identity mismatch"
    wheel_licenses(wheel_data, wheel.name)
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            name.removeprefix("firefly_weave/"): hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("firefly_weave/") and not name.endswith("/")
        }
    docker = ["docker", "--context", context]
    inspected = json.loads(run_command(docker + ["image", "inspect", image]))[0]
    assert inspected["Id"] == image and inspected["Config"]["User"] == "65532:65532"
    tag = "weave-package-" + uuid4().hex
    identifier = (
        run_command(
            docker
            + [
                "create",
                "--name",
                tag,
                "--label",
                "weave.test.owner=" + tag,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                image,
                "python",
                "-I",
                "-c",
                PROBE,
                closure,
                json.dumps(expected, separators=(",", ":")),
            ]
        )
        .decode()
        .strip()
    )
    details = json.loads(run_command(docker + ["inspect", identifier]))[0]
    assert details["Id"] == identifier and details["Image"] == image
    assert details["Config"]["Labels"]["weave.test.owner"] == tag
    assert not details["HostConfig"].get("Binds")
    assert not any(item["Type"] == "bind" for item in details["Mounts"])
    try:
        output = run_command(docker + ["start", "--attach", identifier], timeout=60, limit=1024 * 1024)
        proof = json.loads(output)
        state = json.loads(run_command(docker + ["inspect", identifier]))[0]["State"]
        assert not state["Running"] and state["ExitCode"] == 0
        assert proof["wheel_sha256"] == digest
        assert proof["package_members"] == len(expected)
        destination = Path(os.environ.get("WEAVE_D5_EVIDENCE", str(tmp_path))) / ("image-" + closure + ".json")
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump({"image": image, "container": identifier, "closure": closure, **proof}, stream, indent=2)
    finally:
        details = json.loads(run_command(docker + ["inspect", identifier]))[0]
        assert details["Id"] == identifier and details["Config"]["Labels"]["weave.test.owner"] == tag
        if details["State"]["Running"]:
            run_command(docker + ["stop", "--time", "15", identifier])
