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

"""Actual immutable image executor acceptance; explicit dependency failures, no source mount."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from native_image_support import write_image_proof
from sqlalchemy import make_url
from test_connector_execution import connector_setup as connector_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("connector_setup", ["image"], indirect=True)
async def test_built_peer_executes_scoped_connector(connector_setup, access_db, provisioned, tmp_path, scheduler_url):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, effects = connector_setup
    image = os.environ["WEAVE_B8_IMAGE_ID"]
    admin = await access_db[2].load_principal(provisioned[0].id)
    executor_id = await access_db[2].create_principal(admin, "worker")
    await access_db[2].grant(
        admin,
        executor_id,
        Grant(role="worker", scope=scope, resources=(str(instance.release_id), *instance.task_types)),
    )
    config = [
        {
            "scope": scope.model_dump(mode="json"),
            "principal_id": str(executor_id),
            "release_id": str(instance.release_id),
            "task_types": instance.task_types,
            "capacity": 1,
        }
    ]
    envfile = tmp_path / "executor.env"
    envfile.write_text(
        "\n".join(
            [
                "WEAVE_DATABASE_URL="
                + access_db[3].set(host="host.docker.internal").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_DATABASE_URL="
                + make_url(scheduler_url).set(host="host.docker.internal").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_ENABLED=false",
                "WEAVE_NATIVE_IMAGE_DIGEST=" + image,
                "WEAVE_NATIVE_EXECUTORS=" + json.dumps(config),
                'WEAVE_HTTP_PRIVATE_NETWORKS=["0.0.0.0/0"]',
            ]
        )
    )
    envfile.chmod(0o600)
    name = "weave-b8-proof-" + uuid4().hex[:12]

    docker_context = os.environ.get("WEAVE_TEST_DOCKER_CONTEXT", "colima")
    assert docker_context in {"colima", "colima-weave-tests"}

    async def docker(*args):
        process = await asyncio.create_subprocess_exec(
            "docker", "--context", docker_context, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, "Task-owned Docker command failed (details withheld to protect configuration)"
        return stdout.decode().strip()

    identifier = await docker("run", "-d", "--name", name, "--env-file", str(envfile), image)
    try:
        for _ in range(120):
            process = await asyncio.create_subprocess_exec(
                "docker",
                "--context",
                docker_context,
                "exec",
                identifier,
                "python",
                "-c",
                "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status==200",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait() == 0:
                break
            state = await docker("inspect", "--format", "{{.State.Running}}", identifier)
            assert state == "true", "Built executor failed startup; inspect scoped container logs locally"
            await asyncio.sleep(0.25)
        else:
            pytest.fail("Built executor did not become ready")
        runtime = graph.resolve(RuntimeService)
        run = await runtime.start(
            actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
        )
        for _ in range(100):
            view = await runtime.read(actor, scope, run.id, context=AuditContext())
            if view.state.status == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert view.state.output == {"status": 200, "body": {"ok": True}}
        assert len(effects) == 1
        actual = await docker("inspect", "--format", "{{.Image}}", identifier)
        assert actual == image
        assert await docker("inspect", "--format", "{{json .Mounts}}", identifier) == "[]"
        evidence = {
            "image_id": actual,
            "container": identifier,
            "run_id": str(run.id),
            "executor_principal": str(executor_id),
            "scope": scope.model_dump(mode="json"),
            "release_id": str(instance.release_id),
            "effects": len(effects),
            "source_mount": False,
            "identity_kind": "local Docker image ID; not registry manifest digest",
        }
        proof = (
            Path(os.environ["WEAVE_IMAGE_PROOF_PATH"])
            if os.environ.get("WEAVE_IMAGE_PROOF_PATH")
            else tmp_path / "native-image-proof.json"
        )
        write_image_proof(proof, "http", evidence)
    finally:
        await docker("stop", "--time", "15", identifier)
        envfile.unlink(missing_ok=True)
