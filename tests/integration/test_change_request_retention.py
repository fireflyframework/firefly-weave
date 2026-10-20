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

"""The operations loop persists expired plans and prunes change requests 90 days after they end, on real PostgreSQL."""

import json
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.access.scheduler import next_scope
from firefly_weave.contracts.deployments import ChangeRequest, TargetRequest
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.loop import OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration


async def targets(services, access_db, admin, scope, count):
    principal = await access_db[2].create_principal(admin, "application")
    for role in ("deployment_planner", "deployment_runner"):
        await access_db[2].grant(admin, principal, Grant(role=role, scope=scope))
    actor = await access_db[2].load_principal(principal)
    service = services(access_db[0]).resolve(DeploymentService)
    return actor, [
        await service.create_target(
            TargetRequest(
                name=f"target-{index}",
                adapter="docker-compose",
                external_identity="owned-" + uuid4().hex[:8],
                boundary="weave",
                runner_principal_id=actor.id,
                capabilities=["observe", "scale_workers"],
            ),
            uuid4().hex,
            actor=actor,
            scope=scope,
            context=AuditContext(),
        )
        for index in range(count)
    ]


async def seed(access_db, scope, target, actor, *, state, plan_age=None, ended_days=None):
    """One stored change request: planned with an expiry in the past, or ended some days ago."""
    async with access_db[1].begin() as session:
        now = await session.scalar(text("SELECT clock_timestamp()"))
        identifier = uuid4()
        record = ChangeRequest.model_validate(
            {
                "target_id": str(target.id),
                "deployment_id": str(uuid4()),
                "expected_deployment_revision": 1,
                "intent": "scale_workers",
                "components": ["fixture-worker"],
                "title": "Scale fixture-worker to 3",
                "id": str(identifier),
                "scope": scope.model_dump(mode="json"),
                "revision": 3,
                "requested_by": str(actor.id),
                "created_at": (now - timedelta(days=100)).isoformat(),
                "plan_ttl_seconds": 600,
                "deployment_revision": 1,
                "approval_policy": {"distinct_approver": True},
                "state": state,
                "plan_id": str(uuid4()),
                "plan_digest": "a" * 64,
                "plan_expires_at": (now - (plan_age or timedelta(days=100))).isoformat(),
                "history": [{"at": (now - timedelta(days=100)).isoformat(), "state": "observing"}],
            },
            strict=False,
        )
        await session.execute(
            text(
                "INSERT INTO deployment_change_requests(id,tenant_id,project_id,environment_id,target_id,"
                "deployment_id,payload,state,ended_at) VALUES(:id,:tenant,:project,:environment,:target,"
                ":deployment,cast(:payload AS jsonb),:state,:ended)"
            ),
            {
                "id": identifier,
                "tenant": scope.tenant_id,
                "project": scope.project_id,
                "environment": scope.environment_id,
                "target": target.id,
                "deployment": record.deployment_id,
                "payload": record.model_dump_json(),
                "state": state,
                "ended": None if ended_days is None else now - timedelta(days=ended_days),
            },
        )
    return identifier


async def stored(access_db):
    async with access_db[1]() as session:
        rows = (await session.execute(text("SELECT id,state,ended_at,payload FROM deployment_change_requests"))).all()
    return {row.id: row for row in rows}


async def test_the_loop_persists_expiry_and_prunes_changes_ended_ninety_days_ago(services, access_db, provisioned):
    admin, scopes = provisioned
    actor, mine = await targets(services, access_db, admin, scopes[0], 4)
    other_actor, theirs = await targets(services, access_db, admin, scopes[1], 2)
    expiring = [
        await seed(access_db, scopes[0], target, actor, state="awaiting_approval", plan_age=timedelta(minutes=age))
        for target, age in zip(mine[:3], (3, 2, 1), strict=True)
    ]
    old = await seed(access_db, scopes[0], mine[3], actor, state="succeeded", ended_days=91)
    recent = await seed(access_db, scopes[0], mine[3], actor, state="declined", ended_days=89)
    elsewhere_expiring = await seed(
        access_db, scopes[1], theirs[0], other_actor, state="approved", plan_age=timedelta(minutes=5)
    )
    elsewhere_old = await seed(access_db, scopes[1], theirs[1], other_actor, state="cancelled", ended_days=200)
    uow = UnitOfWork(access_db[0])
    health = LoopHealth()
    health.register("operations_loop", 60, enabled=True)
    loop = OperationsLoop(
        Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False)),
        uow,
        health,
    )
    authority = await next_scope(uow, scopes[0].tenant_id, operations=True)
    assert authority is not None and authority.scope == scopes[0]
    assert await loop.maintain_changes(authority, time.monotonic() + 5) is True
    rows = await stored(access_db)
    for identifier in expiring:
        assert rows[identifier].state == "expired" and rows[identifier].ended_at is not None
        assert rows[identifier].payload["history"][-1]["state"] == "expired"
        assert rows[identifier].payload["history"][-1].get("actor_id") is None
    assert old not in rows and recent in rows
    # Another environment's change requests are left as they were.
    assert rows[elsewhere_expiring].state == "approved" and elsewhere_old in rows
    async with access_db[1]() as session:
        events = (
            (
                await session.execute(
                    text("SELECT payload FROM integration_events WHERE payload->>'type'='change_request.transition'")
                )
            )
            .scalars()
            .all()
        )
    assert sorted(event["resource_id"] for event in events) == sorted(str(identifier) for identifier in expiring)
    assert {json.dumps(event["payload"], sort_keys=True) for event in events} == {
        json.dumps({"change_state": "expired", "revision": 4, "sequence": None, "status": None}, sort_keys=True)
    }
    # A second pass finds nothing more to do.
    assert await loop.maintain_changes(authority, time.monotonic() + 5) is True
    assert len(await stored(access_db)) == len(rows)


async def test_retention_reads_ended_change_requests_by_index(services, access_db, provisioned):
    admin, scopes = provisioned
    actor, mine = await targets(services, access_db, admin, scopes[0], 1)
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_change_requests(id,tenant_id,project_id,environment_id,target_id,payload,"
                "state,ended_at) SELECT gen_random_uuid(),:tenant,:project,:environment,:target,'{}'::jsonb,"
                "'succeeded',clock_timestamp()-make_interval(days=>n % 180) FROM generate_series(1,2000) n"
            ),
            {
                "tenant": scopes[0].tenant_id,
                "project": scopes[0].project_id,
                "environment": scopes[0].environment_id,
                "target": mine[0].id,
            },
        )
        await session.execute(text("ANALYZE deployment_change_requests"))
    from firefly_weave.deployments.change_service import RETENTION_SQL

    async with access_db[1]() as session:
        plan = "\n".join(
            (
                await session.execute(
                    text("EXPLAIN " + RETENTION_SQL),
                    {
                        "tenant": scopes[0].tenant_id,
                        "project": scopes[0].project_id,
                        "environment": scopes[0].environment_id,
                        "cutoff": await session.scalar(text("SELECT clock_timestamp()-interval '90 days'")),
                        "limit": 100,
                    },
                )
            ).scalars()
        )
    assert "deployment_change_requests_ended" in plan or "Index" in plan
    assert "Seq Scan on deployment_change_requests" not in plan
