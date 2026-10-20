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

"""The operations loop keeps change requests tidy, and their transition events carry IDs and states only."""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError

from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ChangeRequest
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent, SubscriptionRequest
from firefly_weave.deployments.change_service import transition_event
from firefly_weave.operations import loop as operations
from firefly_weave.operations.loop import OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"
SCOPE = Scope(tenant_id=UUID(int=2), project_id=UUID(int=3), environment_id=UUID(int=4))
AT = datetime(2026, 10, 20, tzinfo=UTC)


def authority():
    return _SchedulerScope(SCOPE, _CATALOG_AUTHORITY)


def record(**changes) -> ChangeRequest:
    value = {
        "target_id": str(uuid4()),
        "deployment_id": str(uuid4()),
        "expected_deployment_revision": 1,
        "intent": "scale_workers",
        "components": ["fixture-worker"],
        "desired": [{"name": "fixture-worker", "replicas": 3}],
        "title": "Scale fixture-worker to 3",
        "reason": "Morning batch",
        "id": str(uuid4()),
        "scope": SCOPE.model_dump(mode="json"),
        "revision": 4,
        "requested_by": str(uuid4()),
        "created_at": AT.isoformat(),
        "plan_ttl_seconds": 600,
        "deployment_revision": 2,
        "approval_policy": {"distinct_approver": True},
        "state": "approved",
        "plan_id": str(uuid4()),
        "plan_digest": "a" * 64,
        "plan_expires_at": (AT + timedelta(minutes=10)).isoformat(),
        **changes,
    }
    return ChangeRequest.model_validate(value, strict=False)


async def test_an_upkeep_failure_marks_the_quantum_incomplete_and_the_next_environment_still_runs(monkeypatch, caplog):
    tenants = [uuid4(), uuid4()]
    upkept = []

    async def tenant_page(engine, limit=16, *, operations=False):
        return tenants

    async def next_scope(uow, tenant, **kinds):
        return authority()

    async def revalidate(scope, deadline):
        return True

    async def fail(*args, **kwargs):
        upkept.append("expiry")
        if len(upkept) == 1:
            raise RuntimeError("database unavailable")
        return 0

    async def prune(*args, **kwargs):
        upkept.append("retention")
        return 0

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    monkeypatch.setattr("firefly_weave.deployments.change_service.persist_expiry", fail)
    monkeypatch.setattr("firefly_weave.deployments.change_service.retention", prune)
    loop = OperationsLoop(Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER), None, LoopHealth())  # type: ignore[arg-type]
    loop.engine = object()  # type: ignore[assignment]
    monkeypatch.setattr(loop, "revalidate", revalidate)

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation):
        yield object()

    monkeypatch.setattr(loop, "_unit", unit)
    monkeypatch.setattr("firefly_weave.deployments.change_service.DeploymentRepository", lambda tx: Clock())
    with caplog.at_level(logging.ERROR):
        assert await loop.cycle() is False
    # The failed expiry step did not stop retention, and the second environment ran both steps.
    assert upkept == ["expiry", "retention", "expiry", "retention"]
    assert "Operations change request upkeep failed (RuntimeError)" in caplog.text


class Clock:
    async def now(self):
        return AT


def test_transition_events_carry_ids_and_states_only():
    moved = record(state="awaiting_approval", revision=5)
    event = transition_event(moved, AT)
    dumped = event.model_dump(mode="json")
    assert set(dumped) == {
        "version",
        "event_id",
        "type",
        "scope",
        "resource_id",
        "correlation_id",
        "emitted_at",
        "payload",
    }
    assert dumped["type"] == "change_request.transition"
    assert dumped["resource_id"] == dumped["correlation_id"] == str(moved.id)
    assert {key: value for key, value in dumped["payload"].items() if value is not None} == {
        "change_state": "awaiting_approval",
        "revision": 5,
    }
    # The title, reason, people and plan never leave the platform in an event.
    text = event.model_dump_json()
    for secret in ("Morning batch", "Scale fixture-worker", str(moved.requested_by), moved.plan_digest):
        assert secret not in text
    # One event per revision: replaying the same move keeps its ID.
    assert transition_event(moved, AT + timedelta(seconds=1)).event_id == event.event_id
    assert transition_event(record(id=str(moved.id), revision=6), AT).event_id != event.event_id


def test_a_stored_run_transition_event_keeps_its_bytes():
    stored = (
        '{"version":"weave/integration/v1","event_id":"00000000-0000-0000-0000-000000000001",'
        '"type":"run.transition","scope":{"tenant_id":"00000000-0000-0000-0000-000000000002",'
        '"project_id":"00000000-0000-0000-0000-000000000003","environment_id":"00000000-0000-0000-0000-000000000004"},'
        '"resource_id":"00000000-0000-0000-0000-000000000005","correlation_id":"00000000-0000-0000-0000-000000000005",'
        '"emitted_at":"2026-10-20T00:00:00Z","payload":{"status":"running","sequence":3,"revision":null}}'
    )
    event = IntegrationEvent(
        event_id=UUID(int=1),
        type="run.transition",
        scope=SCOPE,
        resource_id=UUID(int=5),
        correlation_id=UUID(int=5),
        emitted_at=AT,
        payload=EventMetadata(status="running", sequence=3),
    )
    assert event.model_dump_json() == stored
    assert IntegrationEvent.model_validate_json(stored).model_dump_json() == stored


def test_subscriptions_accept_five_event_types():
    every = (
        "run.transition",
        "definition.published",
        "activation.activated",
        "activation.retired",
        "change_request.transition",
    )
    base = {
        "name": "notifications",
        "connection_revision_id": str(uuid4()),
        "signing_slot": "token",
        "path": "/weave/events",
    }
    assert SubscriptionRequest.model_validate({**base, "event_types": every}, strict=False).event_types == every
    try:
        SubscriptionRequest.model_validate({**base, "event_types": (*every, "run.transition")}, strict=False)
    except ValidationError:
        pass
    else:
        raise AssertionError("six event types were accepted")
