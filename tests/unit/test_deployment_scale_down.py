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

"""A scale-down plan names exactly the members it removes, chosen the same way every time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from firefly_weave.contracts.deployments import ObservedMember, ObservedResource
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.service import DeploymentService

START = datetime(2026, 10, 20, tzinfo=UTC)


def member(host, ordinal=None, *, ready=True, minutes=0):
    return ObservedMember(
        host=host,
        ordinal=ordinal,
        state="running",
        ready=ready,
        started_at=START + timedelta(minutes=minutes),
        restarts=0,
        node=None,
    )


def test_compose_removes_the_highest_container_numbers():
    members = [member("aaa", 1), member("ccc", 3), member("bbb", 2)]
    target = SimpleNamespace(adapter="docker-compose")
    assert DeploymentService.removed_members(target, members, 2) == ["ccc", "bbb"]
    assert DeploymentService.removed_members(target, members, 0) == []


def test_kubernetes_removes_unready_pods_first_then_the_newest():
    members = [
        member("old", minutes=0),
        member("new", minutes=9),
        member("unready", ready=False, minutes=1),
        member("middle", minutes=5),
    ]
    target = SimpleNamespace(adapter="kubernetes")
    assert DeploymentService.removed_members(target, members, 3) == ["unready", "new", "middle"]


@pytest.mark.parametrize(
    ("adapter", "members"),
    [
        ("docker-compose", [member("aaa", 1)]),
        ("docker-compose", [member("aaa", 1), member("bbb", None)]),
        ("kubernetes", []),
    ],
)
def test_a_scale_down_without_every_member_observed_must_observe_again(adapter, members):
    with pytest.raises(CatalogError) as refused:
        DeploymentService.removed_members(SimpleNamespace(adapter=adapter), members, 2)
    assert (refused.value.status, refused.value.code) == (409, "WV-PLAN-STALE")


def resource(replicas, *live):
    return ObservedResource(
        name="worker",
        external_identity="compose/weave-test/worker",
        kind="worker",
        image="example.test/worker@sha256:" + "b" * 64,
        replicas=replicas,
        ready_replicas=len(live),
        version="1",
        ownership="managed",
        state="progressing",
        members=list(live),
    )


def test_compose_orders_by_container_number_not_by_host():
    members = [member("zzz", 1), member("aaa", 9), member("mmm", 10), member("bbb", 2)]
    target = SimpleNamespace(adapter="docker-compose")
    assert DeploymentService.removed_members(target, members, 2) == ["mmm", "aaa"]


def test_kubernetes_counts_a_pod_without_a_start_time_as_the_newest():
    unstarted = member("pending", minutes=0).model_copy(update={"started_at": None})
    members = [member("old", minutes=0), member("new", minutes=9), unstarted]
    assert DeploymentService.removed_members(SimpleNamespace(adapter="kubernetes"), members, 1) == ["pending"]


@pytest.mark.parametrize(
    ("replicas", "live", "desired", "expected"),
    [
        # Containers 1 and 2 run and 3 has exited: scaling to 2 removes the exited one, never a running worker.
        (3, [member("zzz", 1), member("aaa", 2)], 2, []),
        # Containers 2 and 3 have exited: scaling to 1 removes both and keeps the running one; never stuck.
        (3, [member("zzz", 1)], 1, []),
        # Container 4 has exited: scaling 4 to 2 removes it and the highest-numbered running worker.
        (4, [member("zzz", 1), member("aaa", 9), member("mmm", 10)], 2, ["mmm"]),
        # Nothing has exited: the highest numbers go, as before.
        (3, [member("zzz", 1), member("aaa", 9), member("mmm", 10)], 1, ["mmm", "aaa"]),
    ],
)
def test_a_compose_scale_down_removes_exited_containers_before_running_workers(replicas, live, desired, expected):
    target = SimpleNamespace(adapter="docker-compose")
    assert DeploymentService.scale_down_members(target, resource(replicas, *live), desired) == expected


def test_kubernetes_scale_down_chooses_among_every_pod():
    target = SimpleNamespace(adapter="kubernetes")
    pods = [member("old", minutes=0), member("new", minutes=9)]
    assert DeploymentService.scale_down_members(target, resource(2, *pods), 1) == ["new"]


def test_a_compose_scale_down_with_running_workers_left_unlisted_must_observe_again():
    unlisted = resource(3, member("zzz", 1)).model_copy(update={"ready_replicas": 3})
    with pytest.raises(CatalogError) as refused:
        DeploymentService.scale_down_members(SimpleNamespace(adapter="docker-compose"), unlisted, 1)
    assert (refused.value.status, refused.value.code) == (409, "WV-PLAN-STALE")
