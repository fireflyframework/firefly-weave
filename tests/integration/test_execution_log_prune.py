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

"""Pruning run log entries on real PostgreSQL: older than the retention period, oldest first, in batches of 5,000,
day counters after 35 days, and only the environment the loop visits. A purged run takes its entries with it, as
test_operations_logs_migration.py shows."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from firefly_weave.access.scheduler import next_scope
from firefly_weave.operations import execution_logs
from firefly_weave.operations.loop import OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration


async def seed(access_db, run_id, *, count, days_old):
    async with access_db[1].begin() as tx:
        await tx.execute(
            text(
                "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
                "instance_key,worker_id,at,received_at,source,level,message,bytes,workflow_name,test) "
                "SELECT t.tenant_id,t.project_id,t.environment_id,gen_random_uuid(),t.run_id,t.task_id,1,t.node_id,"
                "t.instance_key,gen_random_uuid(),now()-make_interval(days=>:days)-g*interval '1 millisecond',now(),"
                "'worker','info','entry',5,r.workflow_name,r.test FROM task_facts t JOIN run_facts r ON "
                "r.run_id=t.run_id CROSS JOIN generate_series(1,:count) g WHERE t.run_id=:run"
            ),
            {"run": run_id, "count": count, "days": days_old},
        )


async def counts(access_db):
    async with access_db[1]() as tx:
        entries = dict(
            (await tx.execute(text("SELECT at<now()-interval '7 days',count(*) FROM execution_logs GROUP BY 1"))).all()
        )
        days = sorted(row.day for row in await tx.execute(text("SELECT day FROM execution_log_days")))
    return entries.get(True, 0), entries.get(False, 0), days


async def test_entries_older_than_the_retention_period_are_deleted_in_batches(worker_setup, access_db, queued_task):
    _, _, _, _, scope, _, _ = worker_setup
    await seed(access_db, queued_task.id, count=12000, days_old=8)
    await seed(access_db, queued_task.id, count=3, days_old=1)
    today = datetime.now(UTC).date()
    async with access_db[1].begin() as tx:
        for day in (today - timedelta(days=40), today - timedelta(days=2)):
            await tx.execute(
                text(
                    "INSERT INTO execution_log_days(tenant_id,project_id,environment_id,day,accepted,dropped) "
                    "VALUES(:tenant,:project,:environment,:day,10,0)"
                ),
                {
                    "tenant": scope.tenant_id,
                    "project": scope.project_id,
                    "environment": scope.environment_id,
                    "day": day,
                },
            )
    uow = UnitOfWork(access_db[0])
    removed = []
    for _ in range(3):
        async with uow.open(scope, mutation=True, admission=False) as tx:
            removed.append(await execution_logs.prune(tx, datetime.now(UTC), 7))
    assert removed == [5000, 5000, 2000]
    assert await counts(access_db) == (0, 3, [today - timedelta(days=2)])


async def test_the_loop_prunes_a_backlog_in_the_environment_it_visits(
    worker_setup, access_db, queued_task, provisioned
):
    _, scopes = provisioned
    await seed(access_db, queued_task.id, count=6000, days_old=8)
    await seed(access_db, queued_task.id, count=2, days_old=2)
    uow = UnitOfWork(access_db[0])
    health = LoopHealth()
    health.register("operations_loop", 60, enabled=True)
    loop = OperationsLoop(
        Settings(
            scheduler_enabled=False,
            database_url=access_db[3].render_as_string(hide_password=False),
            execution_log_retention_days=7,
        ),
        uow,
        health,
    )
    authority = await next_scope(uow, scopes[0].tenant_id, operations=True)
    assert authority is not None and authority.scope == scopes[0]
    assert await loop.maintain_logs(authority, time.monotonic() + 10) is True
    old, recent, _ = await counts(access_db)
    assert (old, recent) == (0, 2)
    # A shorter retention period prunes what the longer one kept.
    shorter = OperationsLoop(
        Settings(
            scheduler_enabled=False,
            database_url=access_db[3].render_as_string(hide_password=False),
            execution_log_retention_days=1,
        ),
        uow,
        health,
    )
    assert await shorter.maintain_logs(authority, time.monotonic() + 10) is True
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM execution_logs")) == 0
