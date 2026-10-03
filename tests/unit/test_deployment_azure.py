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

"""ACA observations are allowlisted, evidence based, and never include configuration values."""

import json
from uuid import uuid4

import pytest

from firefly_weave.deployment_runner.azure import AzureContainerAppsAdapter
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError


def settings(tmp_path):
    subscription = str(uuid4())
    environment = f"/subscriptions/{subscription}/resourceGroups/weave/providers/Microsoft.App/managedEnvironments/test"
    policy = DestinationPolicy(
        target_id=uuid4(),
        adapter="azure-container-apps",
        external_identity=environment,
        boundary="weave",
        context=subscription,
        executable="/usr/bin/az",
        capabilities=["observe"],
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "test-worker", "kind": "worker", "configuration": "current", "container": "worker"}],
        image_repositories=["registry.example/worker"],
    )
    image = "registry.example/worker@sha256:" + "a" * 64
    app = {
        "id": f"/subscriptions/{subscription}/resourceGroups/weave/providers/Microsoft.App/containerApps/test-worker",
        "environment": environment,
        "mode": "Single",
        "state": "Succeeded",
        "latest": "test-worker--v1",
        "tags": {},
        "template": {
            "containers": [{"name": "worker", "image": image, "env": [{"name": "SECRET", "value": "PRIVATE"}]}],
            "scale": {"minReplicas": 1, "maxReplicas": 1},
        },
    }
    revisions = [
        {
            "name": "test-worker--v1",
            "replicas": 1,
            "health": "Healthy",
            "state": "Running",
            "provisioning": "Provisioned",
            "images": [image],
        }
    ]
    return policy, app, revisions


@pytest.mark.asyncio
async def test_azure_projects_observation_without_configuration_values(tmp_path):
    policy, app, revisions = settings(tmp_path)
    calls = []

    async def command(argv, **kwargs):
        calls.append(argv)
        return json.dumps(revisions if "revision" in argv else app).encode()

    report = await AzureContainerAppsAdapter(policy, command=command).observe()
    assert report.settled and report.resources[0].ready_replicas == 1
    assert "PRIVATE" not in report.model_dump_json()
    assert all("--subscription" in call and "--name" in call and "test-worker" in call for call in calls)


@pytest.mark.asyncio
async def test_another_managed_environment_is_rejected(tmp_path):
    policy, app, _ = settings(tmp_path)
    app["environment"] += "-other"

    async def command(argv, **kwargs):
        return json.dumps(app).encode()

    with pytest.raises(RunnerPolicyError):
        await AzureContainerAppsAdapter(policy, command=command).observe()


@pytest.mark.asyncio
async def test_old_active_revision_does_not_prove_new_image_ready(tmp_path):
    policy, app, revisions = settings(tmp_path)
    app["latest"] = "test-worker--v2"

    async def command(argv, **kwargs):
        return json.dumps(revisions if "revision" in argv else app).encode()

    report = await AzureContainerAppsAdapter(policy, command=command).observe()
    assert report.complete and not report.settled
    assert report.resources[0].state == "progressing"


@pytest.mark.asyncio
async def test_reviewed_update_preserves_private_configuration_and_waits_for_new_revision(tmp_path):
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, PlanStep

    policy, app, revisions = settings(tmp_path)
    policy = policy.model_copy(update={"capabilities": ["observe", "update", "scale_workers"]})
    writes = []
    renewed = []

    async def before_write():
        renewed.append(True)

    async def command(argv, **kwargs):
        if "rest" in argv:
            assert renewed
            body = json.loads(Path(argv[argv.index("--body") + 1][1:]).read_text())
            writes.append(body)
            app["tags"] = body["tags"]
            app["template"] = body["properties"]["template"]
            app["latest"] = "test-worker--" + app["template"]["revisionSuffix"]
            revisions[0].update(name=app["latest"], replicas=2, images=[app["template"]["containers"][0]["image"]])
            return b"{}"
        return json.dumps(revisions if "revision" in argv else app).encode()

    adapter = AzureContainerAppsAdapter(policy, command=command)
    observed = await adapter.observe()
    now = datetime.now(UTC)
    image = "registry.example/worker@sha256:" + "b" * 64
    plan = DeploymentPlan(
        id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        target_id=policy.target_id,
        target_revision=1,
        deployment_id=uuid4(),
        deployment_revision=1,
        adapter=policy.adapter,
        intent="update",
        observation_id=uuid4(),
        observation_digest="a" * 64,
        steps=[
            PlanStep(
                action="update",
                expected_version=observed.resources[0].version,
                component=ComponentSpec(
                    name="test-worker",
                    kind="worker",
                    image=image,
                    configuration="current",
                    replicas=2,
                    worker_release_id=uuid4(),
                ),
            )
        ],
        risks=["external_effects"],
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        digest="b" * 64,
    )
    result = await adapter.apply(plan, before_write)
    assert result.settled and result.resources[0].ready_replicas == 2
    assert result.resources[0].image == image
    assert result.resources[0].ownership == "managed"
    assert writes[0]["properties"]["template"]["containers"][0]["env"] == [{"name": "SECRET", "value": "PRIVATE"}]
    assert "PRIVATE" not in result.model_dump_json()
    with pytest.raises(RunnerPolicyError):
        await adapter.apply(plan, before_write)
    assert len(writes) == 1
