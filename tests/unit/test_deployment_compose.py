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

"""Compose operations are restricted to the configured daemon and owned project."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, PlanStep
from firefly_weave.deployment_runner.command import CommandError
from firefly_weave.deployment_runner.compose import ComposeAdapter
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError


def policy(tmp_path):
    source = tmp_path / "compose.yaml"
    source.write_text("services: {}")
    return DestinationPolicy(
        target_id=uuid4(),
        adapter="docker-compose",
        external_identity="daemon-id",
        boundary="weave-test",
        context="owned-test",
        executable="/usr/bin/docker",
        capabilities=["observe", "update", "scale_workers"],
        compose_file=str(source),
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "worker", "kind": "worker", "configuration": "current"}],
        image_repositories=["example.test/worker"],
    )


@pytest.mark.asyncio
async def test_observation_never_scans_other_projects_and_reports_evidence(tmp_path):
    calls = []
    config = policy(tmp_path)

    async def command(argv, **kwargs):
        calls.append(argv)
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            return json.dumps(
                {
                    "services": {
                        "worker": {
                            "image": "example.test/worker@sha256:" + "b" * 64,
                            "labels": {"firefly.weave.target": str(config.target_id)},
                        }
                    }
                }
            ).encode()
        if "ps" in argv:
            assert "--project-name" in argv and "weave-test" in argv
            return json.dumps([{"ID": "a" * 64, "Service": "worker"}]).encode()
        assert "inspect" in argv
        return json.dumps(
            {
                "id": "a" * 64,
                "image": "example.test/worker@sha256:" + "b" * 64,
                "state": "running",
                "started": "2026-10-03T00:00:00Z",
                "restart": 0,
                "project": "weave-test",
                "service": "worker",
                "target": str(config.target_id),
                "health": "healthy",
            }
        ).encode()

    report = await ComposeAdapter(config, command=command).observe()
    assert report.complete and report.settled
    fact = report.resources[0]
    assert fact.replicas == fact.ready_replicas == 1
    assert fact.ownership == "managed"
    assert fact.image.endswith("b" * 64)
    assert all(call[:3] == ["/usr/bin/docker", "--context", "owned-test"] for call in calls)


@pytest.mark.asyncio
async def test_wrong_daemon_stops_before_project_access(tmp_path):
    calls = []

    async def command(argv, **kwargs):
        calls.append(argv)
        return b'"other-daemon"'

    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(policy(tmp_path), command=command).observe()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unexpected_service_does_not_escape_inventory_boundary(tmp_path):
    async def command(argv, **kwargs):
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            return b'{"services":{"worker":{}}}'
        return b'[{"ID":"abc","Service":"unapproved"}]'

    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(policy(tmp_path), command=command).observe()


class ComposeProject:
    def __init__(self, config, *, configuration_owner=None, container_owner=None):
        self.configuration = {
            "services": {
                "worker": {
                    "image": "example.test/worker@sha256:" + "b" * 64,
                    "labels": {} if configuration_owner is None else {"firefly.weave.target": configuration_owner},
                }
            }
        }
        self.container = {
            "id": "a" * 64,
            "image": "example.test/worker@sha256:" + "b" * 64,
            "state": "running",
            "started": "2026-10-03T00:00:00Z",
            "restart": 0,
            "project": config.boundary,
            "service": "worker",
            "target": container_owner,
            "health": "healthy",
        }
        self.writes = 0
        self.renewals = 0

    async def command(self, argv, **kwargs):
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            configuration = json.loads(json.dumps(self.configuration))
            files = [argv[index + 1] for index, value in enumerate(argv) if value == "--file"]
            if len(files) == 2:
                override = json.loads(Path(files[-1]).read_text())
                configuration["services"]["worker"].update(override["services"]["worker"])
            return json.dumps(configuration).encode()
        if "ps" in argv:
            return json.dumps([{"ID": self.container["id"], "Service": "worker"}]).encode()
        if "inspect" in argv:
            return json.dumps(self.container).encode()
        assert "up" in argv
        if "--dry-run" not in argv:
            self.writes += 1
            files = [argv[index + 1] for index, value in enumerate(argv) if value == "--file"]
            override = json.loads(Path(files[-1]).read_text())["services"]["worker"]
            self.configuration["services"]["worker"].update(override)
            self.container["target"] = override["labels"]["firefly.weave.target"]
        return b""

    async def before_write(self):
        self.renewals += 1

    def plan(self, config):
        now = datetime.now(UTC)
        return DeploymentPlan(
            id=uuid4(),
            scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
            target_id=config.target_id,
            target_revision=1,
            deployment_id=uuid4(),
            deployment_revision=1,
            adapter="docker-compose",
            intent="scale_workers",
            observation_id=uuid4(),
            observation_digest="a" * 64,
            steps=[
                PlanStep(
                    action="scale_workers",
                    expected_version=canonical_digest(
                        {"configuration": self.configuration, "members": [self.container]}
                    ),
                    component=ComponentSpec(
                        name="worker",
                        kind="worker",
                        image=self.container["image"],
                        configuration="current",
                        replicas=1,
                        worker_release_id=uuid4(),
                    ),
                )
            ],
            risks=["external_effects"],
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            digest="b" * 64,
        )


@pytest.mark.parametrize("foreign_location", ["configuration", "container", "both"])
async def test_observe_rejects_foreign_target_ownership(tmp_path, foreign_location):
    config = policy(tmp_path)
    other = str(uuid4())
    project = ComposeProject(
        config,
        configuration_owner=other if foreign_location in {"configuration", "both"} else None,
        container_owner=other if foreign_location in {"container", "both"} else None,
    )
    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=project.command).observe()


@pytest.mark.parametrize("foreign_location", ["configuration", "container", "both"])
async def test_apply_never_adopts_foreign_target_ownership(tmp_path, foreign_location):
    config = policy(tmp_path)
    other = str(uuid4())
    project = ComposeProject(
        config,
        configuration_owner=other if foreign_location in {"configuration", "both"} else None,
        container_owner=other if foreign_location in {"container", "both"} else None,
    )
    adapter = ComposeAdapter(config, command=project.command)
    with pytest.raises(RunnerPolicyError):
        await adapter.apply(project.plan(config), project.before_write)
    assert project.writes == project.renewals == 0
    assert not adapter.overrides.exists()


@pytest.mark.parametrize("missing_container_owner", [None, ""])
async def test_apply_adopts_unlabelled_resources(tmp_path, missing_container_owner):
    config = policy(tmp_path)
    project = ComposeProject(config, container_owner=missing_container_owner)
    adapter = ComposeAdapter(config, command=project.command)
    initial = await adapter.observe()
    assert initial.resources[0].ownership == "imported"
    result = await adapter.apply(project.plan(config), project.before_write)
    assert result.settled and result.resources[0].ownership == "managed"
    assert project.writes == project.renewals == 1


async def test_restart_preflight_does_not_wait_for_a_stopped_container(tmp_path):
    config = policy(tmp_path)
    project = ComposeProject(config, container_owner=str(config.target_id), configuration_owner=str(config.target_id))
    project.container["state"] = "exited"
    calls = []

    async def command(argv, **kwargs):
        if "up" in argv:
            calls.append((argv, kwargs))
            # Compose dry-run does not start the existing container before its wait check.
            if "--dry-run" in argv and "--wait" in argv:
                raise CommandError()
            if "--dry-run" not in argv:
                assert project.renewals == 1
                assert "--wait" in argv
                assert argv[argv.index("--wait-timeout") + 1] == "90"
                project.container["state"] = "running"
        return await project.command(argv, **kwargs)

    adapter = ComposeAdapter(config, command=command)
    result = await adapter.apply(project.plan(config), project.before_write)
    assert result.settled and result.resources[0].ready_replicas == 1
    assert project.writes == project.renewals == 1
    assert len(calls) == 2
    assert "--dry-run" in calls[0][0] and "--wait" not in calls[0][0]
    assert all(kwargs["timeout"] == 120 for _, kwargs in calls)
