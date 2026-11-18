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

"""A run's log on real PostgreSQL: engine entries from its events merged in time with what its workers wrote,
paged without gaps or repeats, behind run.read, and withheld as reading the run is."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.api.transport import decode_cursor_v2
from firefly_weave.contracts.run_views import RunLogQuery, RunStepQuery
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.run_views import RunViewService

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()


@pytest.fixture
async def views(services, access_db):
    return services(access_db[0]).resolve(RunViewService)


@pytest.fixture
async def lease(task_service, transaction_factory, queued_task, worker_ids):
    async with transaction_factory() as tx:
        return (await task_service.claim(tx, worker_ids[0], 1))[0]


async def plant(access_db, task_id, rows):
    """Stored entries about a task, as the lease-checked insert writes them; each row gives at, level and message."""
    entries = [
        {"id": str(uuid4()), "source": "worker", "code": None, "test": None, "fields": {}, **row}
        | {"at": row["at"].isoformat()}
        for row in rows
    ]
    async with access_db[1].begin() as tx:
        await tx.execute(
            text(
                "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
                "instance_key,worker_id,at,received_at,source,level,code,message,fields,bytes,workflow_name,test) "
                "SELECT t.tenant_id,t.project_id,t.environment_id,e.id,t.run_id,t.task_id,1,t.node_id,t.instance_key,"
                "coalesce((SELECT l.owner FROM task_leases l WHERE l.task_id=t.task_id LIMIT 1),cast(:anyone AS uuid)),"
                "e.at,now(),e.source,e.level,e.code,e.message,e.fields,octet_length(e.message),"
                "r.workflow_name,coalesce(e.test,r.test) FROM task_facts t JOIN run_facts r ON r.run_id=t.run_id "
                "CROSS JOIN jsonb_to_recordset(cast(:entries AS jsonb)) "
                "AS e(id uuid,at timestamptz,source text,level text,code text,message text,fields jsonb,test boolean) "
                "WHERE t.task_id=:task"
            ),
            {"task": task_id, "entries": json.dumps(entries), "anyone": str(uuid4())},
        )
    return [entry["id"] for entry in entries]


async def read_all(views, actor, scope, run_id, query):
    """Every page of a run's log, and what each page asked with."""
    items, cursor = [], None
    while True:
        position = decode_cursor_v2(cursor, scope, query.cursor_collection(run_id))
        page = await views.logs(actor, scope, run_id, query, position, context=CONTEXT)
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


async def test_run_logs_merge_engine_and_worker_entries_in_time_order_and_page_without_gaps_or_repeats(
    views, worker_setup, access_db, lease, queued_task, task_service, transaction_factory
):
    _, _, _, actor, scope, _, _ = worker_setup
    async with access_db[1]() as tx:
        started = await tx.scalar(text("SELECT created_at FROM run_events WHERE sequence=1"))
    planted = await plant(
        access_db,
        lease.proof.task_id,
        [
            {"at": started + timedelta(milliseconds=index), "level": "info", "message": f"Line {index}"}
            for index in range(300)
        ],
    )
    async with transaction_factory() as tx:
        await task_service.complete(tx, lease.proof, uuid4(), 42)
    query = RunLogQuery(limit=50)
    items, cursor, added = [], None, []
    while True:
        position = decode_cursor_v2(cursor, scope, query.cursor_collection(queued_task.id))
        page = await views.logs(actor, scope, queued_task.id, query, position, context=CONTEXT)
        items.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        # An entry written between pages, after everything read so far, appears once, later.
        late = started + timedelta(hours=1, milliseconds=len(added))
        added += await plant(access_db, lease.proof.task_id, [{"at": late, "level": "info", "message": "Late"}])
    async with access_db[1]() as tx:
        events = [str(row.id) for row in await tx.execute(text("SELECT id FROM run_events ORDER BY sequence"))]
    ids = [str(item.id) for item in items]
    assert len(ids) == len(set(ids))
    assert set(ids) == {*planted, *added, *events}
    assert len(added) >= 6
    assert [item.at for item in items] == sorted(item.at for item in items)
    assert items[0].source == "engine" and items[0].code == "RUN.STARTED"
    assert [item.message for item in items if item.source == "worker"][:3] == ["Line 0", "Line 1", "Line 2"]
    finished = [item for item in items if item.code == "STEP.SUCCEEDED"]
    assert len(finished) == 1 and finished[0].node_id is not None


async def test_the_level_filter_keeps_that_level_and_above(views, worker_setup, access_db, lease, queued_task):
    _, _, _, actor, scope, _, _ = worker_setup
    now = datetime.now(UTC)
    await plant(
        access_db,
        lease.proof.task_id,
        [
            {"at": now + timedelta(milliseconds=index), "level": level, "message": level}
            for index, level in enumerate(("debug", "info", "warning", "error"))
        ],
    )
    kept = await read_all(views, actor, scope, queued_task.id, RunLogQuery(level="warning"))
    assert [item.message for item in kept] == ["warning", "error"]
    everything = await read_all(views, actor, scope, queued_task.id, RunLogQuery())
    assert [item.level for item in everything if item.source == "worker"] == ["debug", "info", "warning", "error"]
    # The run's start is an engine entry at info: in the full log, not above warning.
    assert any(item.code == "RUN.STARTED" for item in everything)
    engine_only = await read_all(views, actor, scope, queued_task.id, RunLogQuery(source="engine"))
    assert {item.source for item in engine_only} == {"engine"}


async def test_a_withheld_run_answers_as_reading_the_run_does(views, worker_setup, access_db, lease, queued_task):
    _, _, _, actor, scope, _, _ = worker_setup
    await plant(access_db, lease.proof.task_id, [{"at": datetime.now(UTC), "level": "info", "message": "Hidden"}])
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE run_facts SET classification_state='unavailable'"))
    with pytest.raises(CatalogError) as steps:
        await views.steps(actor, scope, queued_task.id, RunStepQuery(), None, context=CONTEXT)
    with pytest.raises(CatalogError) as logs:
        await views.logs(actor, scope, queued_task.id, RunLogQuery(), None, context=CONTEXT)
    assert (logs.value.status, logs.value.code) == (steps.value.status, steps.value.code)
    with pytest.raises(CatalogError) as missing:
        await views.logs(actor, scope, uuid4(), RunLogQuery(), None, context=CONTEXT)
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")


async def test_a_person_without_run_read_reads_no_entry(
    views, worker_setup, access_db, provisioned, lease, queued_task
):
    _, _, _, _, scope, _, _ = worker_setup
    await plant(access_db, lease.proof.task_id, [{"at": datetime.now(UTC), "level": "info", "message": "Private"}])
    admin, _ = provisioned
    access = access_db[2]
    stranger = await access.load_principal(await access.create_principal(admin, "application"))
    with pytest.raises(AccessDenied):
        await views.logs(stranger, scope, queued_task.id, RunLogQuery(), None, context=CONTEXT)
