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

"""Optional real-daemon acceptance, isolated to a uniquely named disposable project."""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, PlanStep
from firefly_weave.deployment_runner.command import run_command
from firefly_weave.deployment_runner.compose import ComposeAdapter
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError


@pytest.mark.asyncio
async def test_compose_deploy_scale_and_zero_replica_observation(tmp_path):
    context = os.environ.get("WEAVE_TEST_OPERATIONS_DOCKER_CONTEXT")
    image = os.environ.get("WEAVE_TEST_OPERATIONS_IMAGE")
    if not context or not image:
        pytest.skip("Set an explicitly owned Docker context and cached immutable test image")
    executable = shutil.which("docker")
    assert executable
    identifier = json.loads(await run_command([executable, "--context", context, "info", "--format", "{{json .ID}}"]))
    project = "weave-runner-test-" + uuid4().hex[:10]
    path = tmp_path / "compose.json"
    path.write_text(
        json.dumps(
            {
                "services": {
                    "worker": {
                        "image": image,
                        "pull_policy": "never",
                        "command": ["sleep", "300"],
                        "network_mode": "none",
                        "deploy": {"replicas": 0},
                    }
                }
            }
        )
    )
    policy = DestinationPolicy(
        target_id=uuid4(),
        adapter="docker-compose",
        external_identity=identifier,
        boundary=project,
        context=context,
        executable=executable,
        capabilities=["observe", "deploy", "update", "scale_workers"],
        compose_file=str(path),
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "worker", "kind": "worker", "configuration": "current"}],
        image_repositories=[image.split("@")[0]],
    )
    adapter = ComposeAdapter(policy)
    effects = 0

    async def before_write():
        nonlocal effects
        effects += 1

    def plan(observed, replicas, action):
        now = datetime.now(UTC)
        return DeploymentPlan(
            id=uuid4(),
            scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
            target_id=policy.target_id,
            target_revision=1,
            deployment_id=uuid4(),
            deployment_revision=1,
            adapter=policy.adapter,
            intent=action,
            observation_id=uuid4(),
            observation_digest="a" * 64,
            steps=[
                PlanStep(
                    action=action,
                    expected_version=observed.resources[0].version,
                    component=ComponentSpec(
                        name="worker",
                        kind="worker",
                        image=image,
                        configuration="current",
                        replicas=replicas,
                        worker_release_id=uuid4(),
                        cpu_millis=100,
                        memory_mib=128,
                    ),
                )
            ],
            risks=["external_effects"],
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            digest="b" * 64,
        )

    try:
        initial = await adapter.observe()
        assert initial.resources[0].replicas == 0
        first = plan(initial, 1, "deploy")
        running = await adapter.apply(first, before_write)
        assert running.settled and running.resources[0].ready_replicas == 1
        assert running.resources[0].ownership == "managed"
        # The stale first observation cannot be reused after creating a container.
        with pytest.raises(RunnerPolicyError):
            await adapter.apply(first, before_write)
        await run_command([*adapter._files(), "stop", "--timeout", "5", "worker"], timeout=30)
        interrupted = await adapter.observe()
        assert interrupted.resources[0].replicas == 1
        assert interrupted.resources[0].ready_replicas == 0
        assert not interrupted.settled
        running = await adapter.apply(plan(interrupted, 1, "deploy"), before_write)
        assert running.settled and running.resources[0].ready_replicas == 1
        stopped = await adapter.apply(plan(running, 0, "scale_workers"), before_write)
        assert stopped.settled and stopped.resources[0].replicas == 0
        assert stopped.resources[0].image == image
        assert effects == 3
    finally:
        await run_command([*adapter._files(), "down", "--timeout", "5"], timeout=30)
