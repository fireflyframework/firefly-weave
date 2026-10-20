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

"""Compose removes exactly the reviewed workers on a scale-down, and restarts workers in place."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, PlanStep
from firefly_weave.deployment_runner.compose import STOP_GRACE_SECONDS, ComposeAdapter
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError

IMAGE = "example.test/worker@sha256:" + "b" * 64


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
        capabilities=["observe", "deploy", "update", "scale_workers", "restart_workers"],
        compose_file=str(source),
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "worker", "kind": "worker", "configuration": "current"}],
        image_repositories=["example.test/worker"],
    )


class Project:
    """A Compose project with one worker service whose containers change as Compose would change them."""

    def __init__(self, config, count):
        self.config = config
        self.calls = []
        self.serial = 0
        self.containers = [self.new(number) for number in range(1, count + 1)]
        self.replicas = count

    def new(self, number):
        self.serial += 1
        return {
            # Docker's default hostname is the first 12 characters, so they differ per container.
            "id": f"{self.serial:012x}" + "f" * 52,
            "image": IMAGE,
            "state": "running",
            "started": "2026-10-20T08:00:00Z",
            "restart": 0,
            "project": "weave-test",
            "service": "worker",
            "target": str(self.config.target_id),
            "health": "healthy",
            "number": str(number),
        }

    def configuration(self):
        return {
            "services": {
                "worker": {
                    "image": IMAGE,
                    "labels": {"firefly.weave.target": str(self.config.target_id)},
                    "deploy": {"replicas": self.replicas},
                }
            }
        }

    async def command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "info" in argv:
            return b'"daemon-id"'
        if "config" in argv:
            return json.dumps(self.configuration()).encode()
        if "ps" in argv:
            return json.dumps([{"ID": item["id"], "Service": "worker"} for item in self.containers]).encode()
        if "inspect" in argv:
            return json.dumps(next(item for item in self.containers if item["id"] == argv[-1])).encode()
        if "stop" in argv:
            for item in self.containers:
                if item["id"] in argv:
                    item["state"] = "exited"
            return b""
        if "rm" in argv:
            self.containers = [item for item in self.containers if item["id"] not in argv]
            return b""
        assert "up" in argv
        if "--dry-run" in argv:
            return b""
        scale = int(argv[argv.index("--scale") + 1].split("=")[1])
        self.replicas = scale
        if "--force-recreate" in argv:
            numbers = [item["number"] for item in self.containers]
            self.containers = [self.new(int(number)) for number in numbers]
            return b""
        live = sorted(self.containers, key=lambda item: int(item["number"]))
        while len(live) > scale:
            live.pop()
        top = max((int(item["number"]) for item in live), default=0)
        while len(live) < scale:
            top += 1
            live.append(self.new(top))
        for item in live:
            # Compose starts the containers it keeps.
            item["state"] = "running"
        self.containers = live
        return b""

    def host(self, number):
        return next(item["id"][:12] for item in self.containers if item["number"] == str(number))

    def full(self, number):
        return next(item["id"] for item in self.containers if item["number"] == str(number))

    def plan(self, action, replicas):
        now = datetime.now(UTC)
        members = [{key: value for key, value in item.items() if key != "number"} for item in self.containers]
        members.sort(key=lambda item: item["id"])
        return DeploymentPlan(
            id=uuid4(),
            scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
            target_id=self.config.target_id,
            target_revision=1,
            deployment_id=uuid4(),
            deployment_revision=1,
            adapter="docker-compose",
            intent=action,
            observation_id=uuid4(),
            observation_digest="a" * 64,
            steps=[
                PlanStep(
                    action=action,
                    expected_version=canonical_digest({"configuration": self.configuration(), "members": members}),
                    component=ComponentSpec(
                        name="worker",
                        kind="worker",
                        image=IMAGE,
                        configuration="current",
                        replicas=replicas,
                        worker_release_id=uuid4(),
                    ),
                )
            ],
            risks=["external_effects", "worker_drain"],
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            digest="b" * 64,
        )

    def verbs(self):
        return [
            next(word for word in argv[3:] if word in {"info", "config", "ps", "inspect", "stop", "rm", "up"})
            for argv, _ in self.calls
        ]


async def renewed():
    return None


async def test_a_scale_down_stops_and_removes_the_highest_numbered_workers_then_keeps_the_others(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 3)
    # The reviewed rule names the highest container numbers, chosen from the state the plan was made on.
    victims = [project.full(3), project.full(2)]
    keep = project.full(1)
    plan = project.plan("scale_workers", 1)
    project.calls.clear()
    result = await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert [item["id"] for item in project.containers] == [keep]
    assert result.settled and result.resources[0].replicas == 1
    assert [member.host for member in result.resources[0].members] == [keep[:12]]
    stop = next(argv for argv, _ in project.calls if "stop" in argv)
    assert stop[3:] == ["stop", "--time", str(STOP_GRACE_SECONDS), *victims]
    remove = next(argv for argv, _ in project.calls if "rm" in argv)
    assert remove[3:] == ["rm", *victims]
    up = [argv for argv, _ in project.calls if "up" in argv and "--dry-run" not in argv]
    assert len(up) == 1 and "--no-recreate" in up[0] and up[0][up[0].index("--scale") + 1] == "worker=1"
    # Nothing is stopped before the dry run and the second observation prove the reviewed state.
    dry_run = next(index for index, (argv, _) in enumerate(project.calls) if "--dry-run" in argv)
    stopped = next(index for index, (argv, _) in enumerate(project.calls) if "stop" in argv)
    assert dry_run < stopped and "inspect" in project.verbs()[dry_run:stopped]
    stop_kwargs = next(kwargs for argv, kwargs in project.calls if "stop" in argv)
    assert stop_kwargs["timeout"] > STOP_GRACE_SECONDS


async def test_a_worker_without_a_container_number_stops_the_apply_before_any_change(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 2)
    project.containers[1]["number"] = ""
    plan = project.plan("scale_workers", 1)
    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert "stop" not in project.verbs() and "rm" not in project.verbs()
    assert len(project.containers) == 2


async def test_a_scale_down_planned_on_another_state_is_refused(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 3)
    plan = project.plan("scale_workers", 1)
    # A worker exited after the plan was reviewed: the members the rule would pick are not the reviewed ones.
    project.containers[0]["state"] = "exited"
    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert "stop" not in project.verbs()


async def test_a_restart_recreates_every_worker_with_the_same_image_and_count(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 2)
    before = {item["id"] for item in project.containers}
    plan = project.plan("restart_workers", 2)
    project.calls.clear()
    result = await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    after = {item["id"] for item in project.containers}
    assert len(after) == 2 and not before & after
    assert result.settled and result.resources[0].image == IMAGE and result.resources[0].ready_replicas == 2
    up = [argv for argv, _ in project.calls if "up" in argv and "--dry-run" not in argv]
    assert len(up) == 1
    assert "--force-recreate" in up[0] and "--no-deps" in up[0] and "--wait" in up[0]
    assert up[0][up[0].index("--scale") + 1] == "worker=2"


async def test_a_restart_that_leaves_an_old_worker_running_is_not_a_success(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 2)
    original = project.command

    async def stubborn(argv, **kwargs):
        if "up" in argv and "--force-recreate" in argv and "--dry-run" not in argv:
            project.calls.append((argv, kwargs))
            # Compose reported success but kept the first container.
            first = project.containers[0]
            project.containers = [first, project.new(2)]
            return b""
        return await original(argv, **kwargs)

    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=stubborn).apply(project.plan("restart_workers", 2), renewed)


async def test_a_scale_down_removes_exited_containers_before_any_running_worker(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 3)
    project.containers[2]["state"] = "exited"
    exited, kept = project.full(3), [project.full(1), project.full(2)]
    plan = project.plan("scale_workers", 2)
    project.calls.clear()
    result = await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert [item["id"] for item in project.containers] == kept
    assert result.settled and result.resources[0].replicas == 2
    remove = next(argv for argv, _ in project.calls if "rm" in argv)
    assert remove[3:] == ["rm", exited]


async def test_a_scale_down_past_every_exited_container_removes_the_reviewed_running_ones(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 4)
    project.containers[3]["state"] = "exited"
    plan = project.plan("scale_workers", 2)
    kept = [project.full(1), project.full(2)]
    removed = [project.full(3), project.full(4)]
    await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert [item["id"] for item in project.containers] == kept
    remove = next(argv for argv, _ in project.calls if "rm" in argv)
    assert sorted(remove[4:]) == sorted(removed)


async def test_a_scale_down_to_the_running_workers_never_needs_a_running_victim(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 3)
    project.containers[1]["state"] = "exited"
    project.containers[2]["state"] = "exited"
    keep = project.full(1)
    await ComposeAdapter(config, command=project.command).apply(project.plan("scale_workers", 1), renewed)
    assert [item["id"] for item in project.containers] == [keep]
    assert all(keep not in argv for argv, _ in project.calls if "stop" in argv or "rm" in argv)


async def test_a_restart_when_the_worker_count_drifted_is_refused(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 3)
    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=project.command).apply(project.plan("restart_workers", 2), renewed)
    assert not any("--force-recreate" in argv and "--dry-run" not in argv for argv, _ in project.calls)


async def test_exited_containers_go_highest_number_first(tmp_path):
    config = policy(tmp_path)
    project = Project(config, 4)
    project.containers[2]["state"] = "exited"
    project.containers[3]["state"] = "exited"
    keep = [project.full(1), project.full(2), project.full(3)]
    await ComposeAdapter(config, command=project.command).apply(project.plan("scale_workers", 3), renewed)
    assert [item["id"] for item in project.containers] == keep


def test_the_reviewed_members_must_be_live_containers_and_exactly_enough():
    containers = [
        {"id": "a" * 12 + "f" * 52, "state": "running", "number": "1"},
        {"id": "b" * 12 + "f" * 52, "state": "running", "number": "2"},
        {"id": "c" * 12 + "f" * 52, "state": "exited", "number": "3"},
    ]
    component = ComponentSpec(
        name="worker", kind="worker", image=IMAGE, configuration="current", replicas=1, worker_release_id=uuid4()
    )
    assert ComposeAdapter._victims(["b" * 12], containers, 3, component) == [containers[1]["id"], containers[2]["id"]]
    # A stopped container is never a reviewed member, and the count must be exactly what remains to remove.
    for hosts in (["c" * 12], ["a" * 12, "b" * 12], []):
        with pytest.raises(RunnerPolicyError):
            ComposeAdapter._victims(hosts, containers, 3, component)


@pytest.mark.parametrize("action", ["update", "deploy"])
async def test_any_plan_that_lowers_workers_removes_exactly_the_reviewed_members_first(tmp_path, action):
    """An update or deploy to fewer workers is a scale-down: Compose never chooses which workers go."""
    config = policy(tmp_path)
    project = Project(config, 3)
    victims = [project.full(3), project.full(2)]
    keep = project.full(1)
    plan = project.plan(action, 1)
    project.calls.clear()
    result = await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert [item["id"] for item in project.containers] == [keep]
    assert result.settled and result.resources[0].replicas == 1
    stop = next(argv for argv, _ in project.calls if "stop" in argv)
    assert stop[3:] == ["stop", "--time", str(STOP_GRACE_SECONDS), *victims]
    assert next(argv for argv, _ in project.calls if "rm" in argv)[3:] == ["rm", *victims]
    verbs = project.verbs()
    up = max(index for index, verb in enumerate(verbs) if verb == "up")
    assert verbs.index("stop") < up and verbs.index("rm") < up


@pytest.mark.parametrize("action", ["update", "deploy"])
async def test_a_lowering_plan_made_on_another_state_is_refused_before_any_change(tmp_path, action):
    config = policy(tmp_path)
    project = Project(config, 3)
    plan = project.plan(action, 1)
    project.containers[0]["state"] = "exited"
    with pytest.raises(RunnerPolicyError):
        await ComposeAdapter(config, command=project.command).apply(plan, renewed)
    assert "stop" not in project.verbs() and len(project.containers) == 3
