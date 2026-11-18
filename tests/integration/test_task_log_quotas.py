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

"""Log quotas on real PostgreSQL: 200 entries or 64 KiB per attempt, 2,000 entries or 1 MiB per run, and the
environment's daily count. A batch over a limit keeps what fits; one that finds the limit spent is refused."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from test_task_log_ingestion import write

from firefly_weave.contracts.workers import TaskLogEntry
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


def entries(count, *, size=20):
    # An empty fields object stores as "{}", so each entry weighs its message's bytes plus 2.
    return [
        TaskLogEntry(at=datetime.now(UTC), source="worker", level="debug", message="m" * size) for _ in range(count)
    ]


@pytest.fixture
async def lease(task_service, transaction_factory, queued_task, worker_ids):
    async with transaction_factory() as tx:
        return (await task_service.claim(tx, worker_ids[0], 1))[0]


async def seed(access_db, lease, *, count, bytes_each=22):
    """Entries an earlier attempt of the same task wrote, so they count toward the run and not this attempt."""
    async with access_db[1].begin() as tx:
        await tx.execute(
            text(
                "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
                "instance_key,worker_id,at,received_at,source,level,message,bytes,workflow_name,test) "
                "SELECT t.tenant_id,t.project_id,t.environment_id,gen_random_uuid(),t.run_id,t.task_id,99,t.node_id,"
                "t.instance_key,:owner,now(),now(),'worker','debug','earlier',:bytes,r.workflow_name,r.test "
                "FROM task_facts t JOIN run_facts r ON r.run_id=t.run_id CROSS JOIN generate_series(1,:count) "
                "WHERE t.task_id=:task"
            ),
            {"task": lease.proof.task_id, "owner": lease.proof.owner, "count": count, "bytes": bytes_each},
        )


async def spent(worker_setup, access_db, lease, *, size=20, **options):
    with pytest.raises(CatalogError) as error:
        await write(worker_setup, access_db, lease.proof, entries(1, size=size), **options)
    assert (error.value.status, error.value.code) == (429, "WV-LOG-QUOTA")
    return error.value


async def test_an_attempt_writes_at_most_200_entries(worker_setup, access_db, lease):
    for _ in range(4):
        accepted = await write(worker_setup, access_db, lease.proof, entries(50))
        assert (accepted.accepted, accepted.dropped) == (50, 0)
    assert (await spent(worker_setup, access_db, lease)).retry_after is None


async def test_an_attempt_writes_at_most_64_kib_and_a_batch_over_it_keeps_what_fits(worker_setup, access_db, lease):
    # 502 bytes an entry: 130 fit in 65,536.
    for _ in range(2):
        assert (await write(worker_setup, access_db, lease.proof, entries(50, size=500))).accepted == 50
    cut = await write(worker_setup, access_db, lease.proof, entries(50, size=500))
    assert (cut.accepted, cut.dropped) == (30, 20)
    # 276 bytes are left: another 502-byte entry doesn't fit.
    await spent(worker_setup, access_db, lease, size=500)
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT sum(bytes) FROM execution_logs")) == 130 * 502


async def test_a_run_writes_at_most_2000_entries(worker_setup, access_db, lease):
    await seed(access_db, lease, count=1990)
    cut = await write(worker_setup, access_db, lease.proof, entries(50))
    assert (cut.accepted, cut.dropped) == (10, 40)
    await spent(worker_setup, access_db, lease)


async def test_a_run_writes_at_most_one_mib(worker_setup, access_db, lease):
    await seed(access_db, lease, count=1, bytes_each=1024 * 1024 - 1000)
    cut = await write(worker_setup, access_db, lease.proof, entries(5, size=400))
    assert (cut.accepted, cut.dropped) == (2, 3)


async def test_a_spent_daily_limit_says_when_to_retry(worker_setup, access_db, lease):
    cut = await write(worker_setup, access_db, lease.proof, entries(5), dropped=7, daily_limit=3)
    assert (cut.accepted, cut.dropped) == (3, 2)
    refusal = await spent(worker_setup, access_db, lease, daily_limit=3)
    assert refusal.retry_after is not None and 0 < refusal.retry_after <= 86400
    async with access_db[1]() as tx:
        day = (await tx.execute(text("SELECT day,accepted,dropped FROM execution_log_days"))).one()
    assert (day.day, day.accepted, day.dropped) == (datetime.now(UTC).date(), 3, 9)


async def test_two_batches_at_once_never_pass_the_runs_limit(worker_setup, access_db, lease):
    await seed(access_db, lease, count=1990)
    results = await asyncio.gather(
        *(write(worker_setup, access_db, lease.proof, entries(10)) for _ in range(2)), return_exceptions=True
    )
    accepted = [result.accepted for result in results if not isinstance(result, BaseException)]
    refusals = [result for result in results if isinstance(result, BaseException)]
    assert sum(accepted) == 10
    assert all(isinstance(error, CatalogError) and error.code == "WV-LOG-QUOTA" for error in refusals)
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM execution_logs")) == 2000
