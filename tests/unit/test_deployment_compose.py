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


def test_inspect_reads_only_whitelisted_metadata():
    from firefly_weave.deployment_runner.compose import _INSPECT

    fields = json.loads(
        _INSPECT.replace("{{json .Id}}", '""')
        .replace("{{json .Config.Image}}", '""')
        .replace("{{json .State.Status}}", '""')
        .replace("{{json .State.StartedAt}}", '""')
        .replace("{{json .RestartCount}}", "0")
        .replace('{{json (index .Config.Labels "com.docker.compose.project")}}', '""')
        .replace('{{json (index .Config.Labels "com.docker.compose.service")}}', '""')
        .replace('{{json (index .Config.Labels "com.docker.compose.container-number")}}', '""')
        .replace('{{json (index .Config.Labels "firefly.weave.target")}}', '""')
        .replace('{{with (index .State "Health")}}{{json .Status}}{{else}}""{{end}}', '""')
    )
    assert set(fields) == {
        "id",
        "image",
        "state",
        "started",
        "restart",
        "project",
        "service",
        "target",
        "health",
        "number",
    }
    for secret in ("Env", "Mounts", "Cmd", "Entrypoint", "Secrets", "HostConfig"):
        assert secret not in _INSPECT


def worker_project(config, containers):
    """A Compose project whose worker service has the given containers, keyed by full ID."""

    async def command(argv, **kwargs):
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            return json.dumps(
                {
                    "services": {
                        "worker": {
                            "image": "example.test/worker@sha256:" + "b" * 64,
                            "labels": {"firefly.weave.target": str(config.target_id)},
                            "deploy": {"replicas": len(containers)},
                        }
                    }
                }
            ).encode()
        if "ps" in argv:
            return json.dumps([{"ID": item["id"], "Service": "worker"} for item in containers]).encode()
        assert "inspect" in argv, argv
        return json.dumps(next(item for item in containers if item["id"] == argv[-1])).encode()

    return command


def container(identifier, number, **changes):
    return {
        "id": identifier,
        "image": "example.test/worker@sha256:" + "b" * 64,
        "state": "running",
        "started": "2026-10-03T08:15:30.123456789Z",
        "restart": 0,
        "project": "weave-test",
        "service": "worker",
        "target": None,
        "health": "healthy",
        "number": number,
        **changes,
    }


async def test_worker_members_are_the_live_containers(tmp_path):
    config = policy(tmp_path)
    containers = [
        container("b" * 64, "2", state="exited", started="0001-01-01T00:00:00Z", restart=3, health=""),
        container("a" * 64, "1"),
        container("c" * 64, "3", health="unhealthy", started="2026-10-03T09:00:00Z"),
        container("d" * 64, "4", state="created", started="0001-01-01T00:00:00Z", health=""),
        container("e" * 64, "5", state="dead", health=""),
        container("f" * 64, "6", state="removing", health=""),
        container("0" * 64, "7", state="restarting", restart=5, health=""),
    ]
    report = await ComposeAdapter(config, command=worker_project(config, containers)).observe()
    members = [member.model_dump(mode="json") for member in report.resources[0].members]
    assert members == [
        {
            "host": "0" * 12,
            "ordinal": 7,
            "state": "restarting",
            "ready": False,
            "started_at": "2026-10-03T08:15:30.123456Z",
            "restarts": 5,
            "node": None,
        },
        {
            "host": "a" * 12,
            "ordinal": 1,
            "state": "running",
            "ready": True,
            "started_at": "2026-10-03T08:15:30.123456Z",
            "restarts": 0,
            "node": None,
        },
        {
            "host": "c" * 12,
            "ordinal": 3,
            "state": "running",
            "ready": False,
            "started_at": "2026-10-03T09:00:00Z",
            "restarts": 0,
            "node": None,
        },
        {
            "host": "d" * 12,
            "ordinal": 4,
            "state": "created",
            "ready": False,
            "started_at": None,
            "restarts": 0,
            "node": None,
        },
    ]
    # Replica counts still describe every container; only placement leaves stopped ones out.
    assert report.resources[0].ready_replicas == 1 and report.resources[0].replicas == 7


async def test_member_number_does_not_change_the_reviewed_resource_version(tmp_path):
    config = policy(tmp_path)
    containers = [container("a" * 64, "1")]
    report = await ComposeAdapter(config, command=worker_project(config, containers)).observe()
    configuration = json.loads(await worker_project(config, containers)(["config"]))
    before_members = [{key: value for key, value in containers[0].items() if key != "number"}]
    assert report.resources[0].version == canonical_digest({"configuration": configuration, "members": before_members})


async def test_container_without_a_compose_number_has_no_ordinal(tmp_path):
    config = policy(tmp_path)
    report = await ComposeAdapter(config, command=worker_project(config, [container("a" * 64, "")])).observe()
    assert report.resources[0].members[0].ordinal is None


@pytest.mark.parametrize(
    "changes",
    [
        {"number": "1x"},
        {"number": 1},
        {"number": "\u00b2"},
        {"number": "\u0661"},
        {"number": "12345678"},
        {"started": "yesterday"},
        {"restart": -1},
        {"state": "Running Now"},
    ],
)
async def test_one_malformed_live_container_leaves_all_members_out(tmp_path, changes):
    config = policy(tmp_path)
    containers = [container("a" * 64, "1"), {**container("b" * 64, "2"), **changes}]
    report = await ComposeAdapter(config, command=worker_project(config, containers)).observe()
    assert report.complete and report.resources[0].members == []
    assert "members" not in report.model_dump(mode="json")["resources"][0]


@pytest.mark.parametrize("number", ["\u00b2", "\u0661", "\uff11", "1x", "", "12345678"])
def test_compose_numbers_are_ascii_digits(number):
    from firefly_weave.deployment_runner.compose import _ordinal

    if number == "":
        assert _ordinal(number) is None
    else:
        with pytest.raises(RunnerPolicyError):
            _ordinal(number)
    assert _ordinal("1234567") == 1234567 and _ordinal(None) is None


async def test_more_than_one_hundred_live_worker_containers_leave_members_out(tmp_path):
    config = policy(tmp_path)
    containers = [container(f"{index:064x}", str(index + 1)) for index in range(101)]
    report = await ComposeAdapter(config, command=worker_project(config, containers)).observe()
    assert report.complete and report.resources[0].members == [] and report.resources[0].replicas == 101
    stopped = [container(f"{index + 200:064x}", str(index + 200), state="exited", health="") for index in range(5)]
    report = await ComposeAdapter(config, command=worker_project(config, containers[:100] + stopped)).observe()
    assert len(report.resources[0].members) == 100


async def test_only_worker_components_report_members(tmp_path):
    source = tmp_path / "compose.yaml"
    source.write_text("services: {}")
    config = DestinationPolicy(
        target_id=uuid4(),
        adapter="docker-compose",
        external_identity="daemon-id",
        boundary="weave-test",
        context="owned-test",
        executable="/usr/bin/docker",
        capabilities=["observe", "update"],
        compose_file=str(source),
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "worker", "kind": "api", "configuration": "current"}],
        image_repositories=["example.test/worker"],
    )
    report = await ComposeAdapter(config, command=worker_project(config, [container("a" * 64, "1")])).observe()
    assert report.resources[0].members == []
    assert "members" not in report.model_dump(mode="json")["resources"][0]


async def test_member_observation_stays_read_only(tmp_path):
    config = policy(tmp_path)
    calls = []
    command = worker_project(config, [container("a" * 64, "1")])

    async def recorded(argv, **kwargs):
        calls.append(argv)
        return await command(argv, **kwargs)

    await ComposeAdapter(config, command=recorded).observe()
    verbs = [next(word for word in argv[3:] if word in {"info", "config", "ps", "inspect"}) for argv in calls]
    assert verbs == ["info", "config", "ps", "inspect"]
