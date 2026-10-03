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

"""Observed worker capacity never mistakes registration or stale contact for availability."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.contracts.workers import WorkerControlRequest, WorkerInstance
from firefly_weave.workers.models import observed_worker


@pytest.mark.parametrize(
    "age,presence,available", [(None, "unknown", None), (0, "recent", 2), (59, "recent", 2), (60, "stale", None)]
)
def test_presence_expires_and_capacity_is_observation_bound(age, presence, available):
    now = datetime(2026, 10, 3, tzinfo=UTC)
    instance = WorkerInstance(
        id=uuid4(), principal_id=uuid4(), release_id=uuid4(), task_types=["echo@1.0.0"], capacity=3
    )
    result = observed_worker(
        instance,
        last_seen_at=None if age is None else now - timedelta(seconds=age),
        draining=False,
        revision=1,
        active_leases=1,
        observed_at=now,
    )
    assert result.presence == presence
    assert result.available_capacity == available
    assert result.active_leases == 1 and result.capacity == 3 and result.observed_at == now
    assert (result.presence_expires_at is None) == (age is None)


@pytest.mark.parametrize("draining,revoked", [(True, False), (False, True)])
def test_control_fence_has_zero_claim_capacity_without_rewriting_registration(draining, revoked):
    now = datetime(2026, 10, 3, tzinfo=UTC)
    instance = WorkerInstance(
        id=uuid4(), principal_id=uuid4(), release_id=uuid4(), task_types=["echo@1.0.0"], capacity=3, revoked=revoked
    )
    result = observed_worker(
        instance, last_seen_at=now, draining=draining, revision=2, active_leases=1, observed_at=now
    )
    assert result.available_capacity == 0 and result.active_leases == 1 and result.capacity == 3
    assert set(instance.model_dump()) == {"id", "principal_id", "release_id", "task_types", "capacity", "revoked"}


def test_control_requires_positive_observed_revision():
    with pytest.raises(ValueError):
        WorkerControlRequest(expected_revision=0)
