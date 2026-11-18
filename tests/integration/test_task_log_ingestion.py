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

"""Writing log entries on real PostgreSQL: only the holder of a current lease writes, about that lease, and the
server redacts and dates what it stores."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.workers import TaskLogEntry, TaskLogRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import UnitOfWork

pytestmark = pytest.mark.integration
SECRET = "hunter2-" + "Correct-Horse"


def entries(count=1, *, message="Checked the order", at=None, **fields):
    return [
        TaskLogEntry(
            at=at or datetime.now(UTC),
            source="worker",
            level="info",
            code="ORDER.CHECKED",
            message=message,
            fields=fields or {"index": index},
        )
        for index in range(count)
    ]


async def write(worker_setup, access_db, proof, items, *, dropped=0, daily_limit=200_000):
    tasks, _, _, actor, scope, _, _ = worker_setup
    async with UnitOfWork(access_db[0]).open(scope, mutation=True, admission=False) as tx:
        return await tasks.log(
            tx,
            TaskLogRequest(lease=proof, entries=items, dropped=dropped),
            actor=actor,
            scope=scope,
            context=AuditContext(),
            daily_limit=daily_limit,
        )


async def stored(access_db):
    async with access_db[1]() as tx:
        return (await tx.execute(text("SELECT * FROM execution_logs ORDER BY at,id"))).mappings().all()


@pytest.fixture
async def lease(task_service, transaction_factory, queued_task, worker_ids):
    async with transaction_factory() as tx:
        return (await task_service.claim(tx, worker_ids[0], 1))[0]


async def refused(worker_setup, access_db, proof):
    # With room left and with the day's quota spent alike, so a dead or foreign lease learns nothing of the quotas.
    for daily_limit in (200_000, 0):
        with pytest.raises(CatalogError) as error:
            await write(worker_setup, access_db, proof, entries(), daily_limit=daily_limit)
        assert (error.value.status, error.value.code) == (409, "WV-LOG-LEASE")
    assert await stored(access_db) == []


async def test_the_run_and_step_come_from_the_lease(worker_setup, access_db, lease, queued_task):
    accepted = await write(worker_setup, access_db, lease.proof, entries(2))
    assert (accepted.accepted, accepted.dropped) == (2, 0)
    async with access_db[1]() as tx:
        task = (
            await tx.execute(
                text(
                    "SELECT t.node_id,t.instance_key,r.workflow_name,r.test FROM task_facts t "
                    "JOIN run_facts r ON r.run_id=t.run_id WHERE t.task_id=:task"
                ),
                {"task": lease.proof.task_id},
            )
        ).one()
    rows = await stored(access_db)
    assert [row["fields"] for row in rows] == [{"index": 0}, {"index": 1}]
    for row in rows:
        assert row["run_id"] == queued_task.id
        assert (row["task_id"], row["generation"], row["worker_id"]) == (
            lease.proof.task_id,
            lease.proof.generation,
            lease.proof.owner,
        )
        assert (row["node_id"], row["instance_key"], row["workflow_name"], row["test"]) == tuple(task)
        assert (row["source"], row["level"], row["code"]) == ("worker", "info", "ORDER.CHECKED")
        assert row["bytes"] > 0


async def test_a_revoked_lease_writes_nothing(worker_setup, access_db, lease):
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET status='recovered'"))
    await refused(worker_setup, access_db, lease.proof)


async def test_an_expired_lease_writes_nothing(worker_setup, access_db, lease):
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    await refused(worker_setup, access_db, lease.proof)


async def test_a_stale_generation_writes_nothing(worker_setup, access_db, lease):
    async with access_db[1].begin() as tx:
        await tx.execute(
            text(
                "INSERT INTO task_leases SELECT tenant_id,project_id,environment_id,task_id,generation+1,owner,"
                "token_hash,claimed_at,expires_at,deadline,capability,policy,status,transaction_aware "
                "FROM task_leases WHERE task_id=:task"
            ),
            {"task": lease.proof.task_id},
        )
    await refused(worker_setup, access_db, lease.proof)


async def test_another_workers_lease_writes_nothing(worker_setup, access_db, lease, worker_ids):
    await refused(worker_setup, access_db, lease.proof.model_copy(update={"owner": worker_ids[1]}))
    await refused(worker_setup, access_db, lease.proof.model_copy(update={"token": "forged"}))


@pytest.mark.parametrize("role", ["developer", "deployer", "operator", "viewer", "task_participant"])
async def test_a_person_without_task_log_is_refused_before_any_lease_is_read(
    worker_setup, access_db, provisioned, lease, role
):
    from uuid import uuid4

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Grant

    tasks, _, _, _, scope, _, _ = worker_setup
    _, _, access, _ = access_db
    admin, _ = provisioned
    person_id = await access.create_principal(admin, "human")
    granted = scope.model_copy(update={"environment_id": None}) if role == "developer" else scope
    await access.grant(admin, person_id, Grant(role=role, scope=granted))
    person = await access.load_principal(person_id)
    # A lease naming no worker, and the worker's real one: neither reaches the lease check.
    for proof in (lease.proof.model_copy(update={"task_id": uuid4(), "owner": uuid4()}), lease.proof):
        with pytest.raises(AccessDenied):
            async with UnitOfWork(access_db[0]).open(scope, mutation=True, admission=False) as tx:
                await tasks.log(
                    tx,
                    TaskLogRequest(lease=proof, entries=entries()),
                    actor=person,
                    scope=scope,
                    context=AuditContext(),
                    daily_limit=200_000,
                )
    assert await stored(access_db) == []


async def test_a_purged_run_answers_lease_gone(worker_setup, access_db, lease):
    async with access_db[1].begin() as tx:
        await tx.execute(text("DELETE FROM task_leases WHERE task_id=:task"), {"task": lease.proof.task_id})
    await refused(worker_setup, access_db, lease.proof)


async def test_the_server_redacts_what_a_worker_left_in(worker_setup, access_db, lease):
    left_in = TaskLogEntry(
        at=datetime.now(UTC),
        source="connector",
        level="warning",
        message="Retrying with password=" + SECRET,
        fields={"url": "https://api.example.test/items?sig=" + SECRET},
        redactions=1,
    )
    await write(worker_setup, access_db, lease.proof, [left_in])
    (row,) = await stored(access_db)
    assert row["message"] == "Retrying with password=[redacted]"
    assert row["fields"] == {"url": "https://api.example.test/items?sig=[redacted]"}
    # One value the worker hid, two the server hid.
    assert row["redactions"] == 3
    assert SECRET not in str(dict(row))


async def test_the_widest_valid_fields_are_stored_within_the_byte_limit(worker_setup, access_db, lease):
    wide = [
        TaskLogEntry(at=datetime.now(UTC), source="worker", level="info", message="Wide", fields=fields)
        for fields in (
            {f"field_{index:02d}": "界" * 256 for index in range(16)},
            {f"field_{index:02d}": '"' * 256 for index in range(16)},
            {f"field_{index:02d}": -5e-324 if index % 2 else 1.5e308 for index in range(16)},
        )
    ]
    assert (await write(worker_setup, access_db, lease.proof, wide)).accepted == 3
    async with access_db[1]() as tx:
        rows = (
            await tx.execute(text("SELECT octet_length(fields::text) AS size,fields,truncated FROM execution_logs"))
        ).all()
    assert all(size <= 8192 for size, _, _ in rows)
    # Text fields past the limit are left out and the entry says so; numbers fit whole.
    assert sorted((len(fields), truncated) for _, fields, truncated in rows) == [(10, True), (15, True), (16, False)]


async def test_a_row_the_database_refuses_answers_invalid_not_failed(worker_setup, access_db, lease, monkeypatch):
    from firefly_weave.operations import execution_logs

    shaped = execution_logs.stored_entry

    def too_long(entry):
        return {**shaped(entry), "message": "m" * 600}

    monkeypatch.setattr(execution_logs, "stored_entry", too_long)
    with pytest.raises(CatalogError) as refused:
        await write(worker_setup, access_db, lease.proof, entries())
    assert (refused.value.status, refused.value.code) == (422, "WV-VALIDATION")
    assert await stored(access_db) == []


async def test_an_entry_dated_outside_the_lease_is_moved_inside_it(worker_setup, access_db, lease):
    early = entries(1, at=datetime(2000, 1, 1, tzinfo=UTC), position="early")
    late = entries(1, at=datetime.now(UTC) + timedelta(days=1), position="late")
    await write(worker_setup, access_db, lease.proof, early + late)
    async with access_db[1]() as tx:
        claimed = await tx.scalar(text("SELECT claimed_at FROM task_leases"))
    rows = {row["fields"]["position"]: row for row in await stored(access_db)}
    assert rows["early"]["at"] == claimed
    assert rows["late"]["at"] == rows["late"]["received_at"]
    assert rows["late"]["at"] < datetime.now(UTC) + timedelta(minutes=1)
