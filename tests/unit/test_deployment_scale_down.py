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

from firefly_weave.contracts.deployments import ObservedMember
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
