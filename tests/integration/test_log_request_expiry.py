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

"""The operations loop deletes container log requests and their snapshots once their hour ends, on real
PostgreSQL."""

import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.access.scheduler import next_scope
from firefly_weave.contracts.deployments import TargetRequest
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.loop import OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration


async def target(services, access_db, admin, scope):
    principal = await access_db[2].create_principal(admin, "application")
    await access_db[2].grant(admin, principal, Grant(role="deployment_planner", scope=scope))
    actor = await access_db[2].load_principal(principal)
    return (
        await services(access_db[0])
        .resolve(DeploymentService)
        .create_target(
            TargetRequest(
                name="local-docker",
                adapter="docker-compose",
                external_identity="owned-" + uuid4().hex[:8],
                boundary="weave",
                runner_principal_id=actor.id,
                capabilities=["observe", "read_logs"],
            ),
            uuid4().hex,
            actor=actor,
            scope=scope,
            context=AuditContext(),
        )
    )


async def seed(access_db, scope, target_id, *, age_minutes):
    """One request made `age_minutes` ago, ready, with its snapshot."""
    identifier = uuid4()
    values = {
        "tenant": scope.tenant_id,
        "project": scope.project_id,
        "environment": scope.environment_id,
        "id": identifier,
        "target": target_id,
        "age": timedelta(minutes=age_minutes),
    }
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_log_requests(tenant_id,project_id,environment_id,id,target_id,requested_by,"
                "component,since_seconds,tail_lines,state,runner_id,claim_token,created_at,expires_at,read_at) "
                "VALUES(:tenant,:project,:environment,:id,:target,gen_random_uuid(),'fixture-worker',900,200,'ready',"
                "gen_random_uuid(),gen_random_uuid(),now()-cast(:age AS interval),"
                "now()-cast(:age AS interval)+interval '1 hour',now()-cast(:age AS interval))"
            ),
            values,
        )
        await session.execute(
            text(
                "INSERT INTO deployment_log_snapshots(tenant_id,project_id,environment_id,request_id,members,"
                "truncated,bytes,read_at) VALUES(:tenant,:project,:environment,:id,"
                '\'[{"member":"weave-worker-1","lines":[]}]\'::jsonb,false,0,now())'
            ),
            values,
        )
    return identifier


async def remaining(access_db):
    async with access_db[1]() as session:
        requests = set((await session.execute(text("SELECT id FROM deployment_log_requests"))).scalars())
        snapshots = set((await session.execute(text("SELECT request_id FROM deployment_log_snapshots"))).scalars())
    return requests, snapshots


async def test_the_loop_deletes_requests_and_snapshots_an_hour_after_the_request(services, access_db, provisioned):
    admin, scopes = provisioned
    mine = await target(services, access_db, admin, scopes[0])
    theirs = await target(services, access_db, admin, scopes[1])
    expired = [await seed(access_db, scopes[0], mine.id, age_minutes=age) for age in (61, 90, 600)]
    current = await seed(access_db, scopes[0], mine.id, age_minutes=59)
    elsewhere = await seed(access_db, scopes[1], theirs.id, age_minutes=120)
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
    requests, snapshots = await remaining(access_db)
    assert requests == snapshots == {current, elsewhere}
    assert not set(expired) & requests
    # A second pass finds nothing more to delete.
    assert await loop.maintain_changes(authority, time.monotonic() + 5) is True
    assert await remaining(access_db) == (requests, snapshots)
