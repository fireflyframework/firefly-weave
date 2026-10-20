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

"""Every change request transition, expiry, the drain window and plan lifetimes, without a database."""

import json
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ChangeRequest, ChangeRequestCreate, ChangeState
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments import change_requests as states

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ALL = get_args(ChangeState.__value__)
ALLOWED = {
    ("observing", "ready_to_plan"),
    ("observing", "failed"),
    ("observing", "cancelled"),
    ("observing", "superseded"),
    ("ready_to_plan", "awaiting_approval"),
    ("ready_to_plan", "failed"),
    ("ready_to_plan", "cancelled"),
    ("ready_to_plan", "superseded"),
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "declined"),
    ("awaiting_approval", "cancelled"),
    ("awaiting_approval", "expired"),
    ("awaiting_approval", "superseded"),
    ("approved", "draining"),
    ("approved", "applying"),
    ("approved", "declined"),
    ("approved", "cancelled"),
    ("approved", "expired"),
    ("approved", "superseded"),
    ("draining", "applying"),
    ("draining", "cancelled"),
    ("draining", "expired"),
    ("draining", "superseded"),
    ("applying", "succeeded"),
    ("applying", "failed"),
    ("applying", "reconciliation_required"),
    ("expired", "observing"),
    ("failed", "observing"),
    ("superseded", "observing"),
}


def record(state="observing", **changes):
    request = ChangeRequestCreate.model_validate_json(
        json.dumps(
            {
                "target_id": str(uuid4()),
                "deployment_id": str(uuid4()),
                "expected_deployment_revision": 1,
                "intent": "scale_workers",
                "components": ["fixture-worker"],
                "desired": [{"name": "fixture-worker", "replicas": 1}],
                "drain": {"mode": "drain_first", "timeout_seconds": 300},
                "title": "Scale fixture-worker to 1",
            }
        )
    )
    value = {
        **request.model_dump(),
        "id": uuid4(),
        "scope": Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        "revision": 1,
        "requested_by": uuid4(),
        "created_at": NOW,
        "plan_ttl_seconds": 900,
        "deployment_revision": 2,
        "approval_policy": {"distinct_approver": True},
        "state": state,
        **changes,
    }
    return ChangeRequest.model_validate(value, strict=False)


@pytest.mark.parametrize("before", ALL)
@pytest.mark.parametrize("after", ALL)
def test_every_transition_is_either_allowed_or_refused_with_a_state_error(before, after):
    actor = uuid4()
    if (before, after) in ALLOWED:
        moved = states.transition(record(before), after, actor=actor, at=NOW)
        assert moved.state == after and moved.revision == 2
        assert moved.history[-1].model_dump() == {"at": NOW, "state": after, "actor_id": actor}
    else:
        with pytest.raises(CatalogError) as refused:
            states.transition(record(before), after, actor=actor, at=NOW)
        assert (refused.value.status, refused.value.code) == (409, "WV-CHANGE-STATE")


def test_the_active_and_ended_states_partition_every_state_and_match_the_table_constraint():
    assert set(states.ACTIVE) | set(states.ENDED) == set(ALL)
    assert not set(states.ACTIVE) & set(states.ENDED)
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0034_operations_change_requests.py"
    spec = importlib.util.spec_from_file_location("change_request_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert ",".join(f"'{state}'" for state in states.ACTIVE) == migration.ACTIVE
    assert ",".join(f"'{state}'" for state in states.ENDED) == migration.ENDED


def test_history_keeps_the_latest_fifty_entries():
    value = record("expired")
    for index in range(30):
        value = states.transition(value, "observing", actor=None, at=NOW + timedelta(seconds=index))
        value = states.transition(value, "superseded", actor=None, at=NOW + timedelta(seconds=index))
    assert len(value.history) == 50
    assert value.history[-1].state == "superseded" and value.history[0].at == NOW + timedelta(seconds=5)


@pytest.mark.parametrize("state", ["awaiting_approval", "approved", "draining"])
def test_a_plan_past_its_expiry_reads_as_expired_until_the_next_write_persists_it(state):
    value = record(state, plan_expires_at=NOW)
    assert states.effective(value, NOW - timedelta(seconds=1)).state == state
    expired = states.effective(value, NOW)
    assert expired.state == "expired" and expired.revision == value.revision
    assert states.effective(record("applying", plan_expires_at=NOW), NOW).state == "applying"
    assert states.effective(record("observing"), NOW).state == "observing"


def test_a_drain_starts_only_while_the_plan_outlives_the_timeout_plus_two_minutes():
    value = record("approved", plan_expires_at=NOW + timedelta(seconds=420))
    states.require_drain_window(value, NOW)
    with pytest.raises(CatalogError) as refused:
        states.require_drain_window(value, NOW + timedelta(seconds=1))
    assert (refused.value.status, refused.value.code) == (409, "WV-DRAIN-WINDOW")


def test_the_approval_deadline_leaves_time_to_drain():
    expires = NOW + timedelta(seconds=900)
    assert states.approve_by(record("awaiting_approval", plan_expires_at=expires)) == expires - timedelta(seconds=420)
    plain = record("awaiting_approval", plan_expires_at=expires, drain={"mode": "none", "timeout_seconds": 300})
    assert states.approve_by(plain) == expires


@pytest.mark.parametrize(
    ("requested", "drains", "timeout", "expected"),
    [
        (None, False, 300, 600),
        (None, True, 300, 900),
        (480, True, 300, 480),
        (120, False, 300, 120),
    ],
)
def test_plan_lifetimes_default_by_drain_and_must_cover_a_drain(requested, drains, timeout, expected):
    assert states.plan_lifetime(requested, drains=drains, timeout=timeout) == expected


@pytest.mark.parametrize(("requested", "timeout"), [(479, 300), (600, 600), (30, 60)])
def test_a_plan_lifetime_shorter_than_the_drain_plus_three_minutes_is_refused(requested, timeout):
    with pytest.raises(CatalogError) as refused:
        states.plan_lifetime(requested, drains=True, timeout=timeout)
    assert (refused.value.status, refused.value.code) == (422, "WV-VALIDATION")


def test_only_precondition_failures_may_be_planned_again():
    assert states.replannable(record("expired"))
    assert states.replannable(record("superseded"))
    assert states.replannable(record("failed", failure={"code": "WV-PLAN-STALE", "message": "Target changed"}))
    assert states.replannable(record("failed", failure={"code": "WV-PRECONDITION-FAILED", "message": "Changed"}))
    assert states.replannable(record("failed", failure={"code": "WV-DRAIN-WINDOW", "message": "Too late"}))
    assert states.replannable(record("failed", failure={"code": "WV-JOB-CANCELLED", "message": "Cancelled"}))
    assert not states.replannable(record("failed", failure={"code": "WV-PROVIDER-FAILED", "message": "Refused"}))
    assert not states.replannable(record("declined"))
