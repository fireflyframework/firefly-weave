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

"""The log across runs on real PostgreSQL: newest first, test runs hidden unless asked, withheld runs as rows
without text, a case-blind search that takes % and _ as text, bounded ranges and reads, and failures as entries."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_run_logs_api import CONTEXT, plant

from firefly_weave.api.transport import decode_cursor_v2
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import ExecutionLogQuery
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.run_views import RunViewService

pytestmark = pytest.mark.integration


@pytest.fixture
async def views(services, access_db):
    return services(access_db[0]).resolve(RunViewService)


@pytest.fixture
async def runs(worker_setup, queued_task, access_db):
    """Two runs of the same workflow, and the task of each."""
    _, runtime, _, actor, scope, activation, _ = worker_setup
    second = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=4), "second", context=CONTEXT
    )
    async with access_db[1]() as tx:
        tasks = dict((await tx.execute(text("SELECT run_id,task_id FROM task_facts"))).all())
    return (queued_task.id, tasks[queued_task.id]), (second.id, tasks[second.id])


async def read_all(views, worker_setup, query):
    _, _, _, actor, scope, _, _ = worker_setup
    items, cursor = [], None
    while True:
        position = decode_cursor_v2(cursor, scope, query.cursor_collection())
        page = await views.execution_logs(actor, scope, query, position, context=CONTEXT)
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


async def owner(access_db, statement, **values):
    async with access_db[1].begin() as tx:
        await tx.execute(text(statement), values)


async def test_newest_first_across_runs_and_pages_without_repeats(views, worker_setup, access_db, runs):
    (first, first_task), (second, second_task) = runs
    await plant(access_db, first_task, [{"at": ago(m), "level": "info", "message": f"A{m}"} for m in (30, 20, 10)])
    await plant(access_db, second_task, [{"at": ago(m), "level": "info", "message": f"B{m}"} for m in (25, 15, 5)])
    await owner(
        access_db, "UPDATE run_facts SET status='cancelled',ended_at=:at WHERE run_id=:run", at=ago(1), run=second
    )
    items = await read_all(views, worker_setup, ExecutionLogQuery(limit=2))
    assert [item.message for item in items] == ["Run canceled", "B5", "A10", "B15", "A20", "B25", "A30"]
    assert [item.run_id for item in items][:2] == [second, second]
    assert len({item.id for item in items}) == 7


async def test_test_runs_are_hidden_unless_asked(views, worker_setup, access_db, runs):
    (_, first_task), (second, second_task) = runs
    await plant(access_db, first_task, [{"at": ago(5), "level": "info", "message": "Real"}])
    await owner(access_db, "UPDATE run_facts SET test=true WHERE run_id=:run", run=second)
    await plant(access_db, second_task, [{"at": ago(4), "level": "info", "message": "Trial", "test": True}])
    assert [item.message for item in await read_all(views, worker_setup, ExecutionLogQuery())] == ["Real"]
    both = await read_all(views, worker_setup, ExecutionLogQuery(include_test=True))
    assert [(item.message, item.test) for item in both] == [("Trial", True), ("Real", False)]


async def test_withheld_runs_show_no_text(views, worker_setup, access_db, runs):
    (_, first_task), (second, second_task) = runs
    await plant(access_db, first_task, [{"at": ago(5), "level": "info", "message": "Line visible"}])
    hidden = await plant(access_db, second_task, [{"at": ago(4), "level": "info", "message": "Line withheld"}])
    await owner(access_db, "UPDATE run_facts SET classification_state='unavailable' WHERE run_id=:run", run=second)
    items = await read_all(views, worker_setup, ExecutionLogQuery())
    assert isinstance(items[0], UnavailableResource) and str(items[0].id) == hidden[0]
    assert "withheld" not in items[0].model_dump_json()
    assert items[1].message == "Line visible"
    # A search never says what a withheld run's entry holds, so it leaves the row out.
    searched = await read_all(views, worker_setup, ExecutionLogQuery(contains="Line"))
    assert [item.message for item in searched] == ["Line visible"]


async def test_a_search_finds_past_withheld_matches_without_a_cursor_that_points_at_them(
    views, worker_setup, access_db, runs
):
    _, _, _, actor, scope, _, _ = worker_setup
    (_, first_task), (second, second_task) = runs
    await plant(access_db, first_task, [{"at": ago(9), "level": "info", "message": "token-prefix visible"}])
    await plant(
        access_db,
        second_task,
        [{"at": ago(m), "level": "info", "message": f"token-prefix withheld {m}"} for m in (2, 3, 4)],
    )
    await owner(access_db, "UPDATE run_facts SET classification_state='unavailable' WHERE run_id=:run", run=second)
    page = await views.execution_logs(
        actor, scope, ExecutionLogQuery(contains="token-prefix", limit=1), None, context=CONTEXT
    )
    # The withheld matches are newer, yet the first page is the visible one and nothing points past it.
    assert [item.message for item in page.items] == ["token-prefix visible"]
    assert page.next_cursor is None
    only_withheld = await views.execution_logs(
        actor, scope, ExecutionLogQuery(contains="withheld", limit=1), None, context=CONTEXT
    )
    assert (only_withheld.items, only_withheld.next_cursor) == ([], None)


async def test_contains_finds_text_regardless_of_case_and_treats_percent_and_underscore_as_text(
    views, worker_setup, access_db, runs
):
    (_, task), _ = runs
    messages = ["Refund 100% applied", "refund_done for the order", "Shipped", "Refunded 1000 units"]
    await plant(
        access_db, task, [{"at": ago(10 - index), "level": "info", "message": m} for index, m in enumerate(messages)]
    )

    async def found(needle):
        return {item.message for item in await read_all(views, worker_setup, ExecutionLogQuery(contains=needle))}

    assert await found("REFUND") == {"Refund 100% applied", "refund_done for the order", "Refunded 1000 units"}
    assert await found("0%") == {"Refund 100% applied"}
    assert await found("d_d") == {"refund_done for the order"}


async def test_contains_longer_than_6_hours_is_refused(views, worker_setup):
    _, _, _, actor, scope, _, _ = worker_setup
    with pytest.raises(CatalogError) as refused:
        await views.execution_logs(
            actor,
            scope,
            ExecutionLogQuery.model_validate({"from": ago(7 * 60).isoformat(), "contains": "refund"}),
            None,
            context=CONTEXT,
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-FILTER")


async def test_a_slow_read_answers_unavailable(views, worker_setup, access_db, runs):
    _, _, _, actor, scope, _, _ = worker_setup
    async with access_db[1]() as tx:
        # A read that can't get its rows within 2 seconds gives up instead of holding a connection.
        await tx.execute(text("LOCK TABLE execution_logs IN ACCESS EXCLUSIVE MODE"))
        try:
            with pytest.raises(CatalogError) as slow:
                await asyncio.wait_for(
                    views.execution_logs(actor, scope, ExecutionLogQuery(), None, context=CONTEXT), timeout=10
                )
        finally:
            await tx.rollback()
    assert (slow.value.status, slow.value.code) == (503, "WV-UNAVAILABLE")
    assert "Narrow the time range" in slow.value.message


async def test_failed_steps_and_runs_appear_as_engine_entries(views, worker_setup, access_db, runs):
    (first, _), (second, _) = runs
    await owner(
        access_db,
        "UPDATE step_facts SET status='failed',ended_at=:at,error_code='HANDLER_FAILED' WHERE run_id=:run",
        at=ago(3),
        run=first,
    )
    await owner(
        access_db,
        "UPDATE step_facts SET status='failed',ended_at=:at,error_code='HANDLER_FAILED',handled='continue' "
        "WHERE run_id=:run",
        at=ago(2),
        run=second,
    )
    await owner(
        access_db,
        "UPDATE run_facts SET status='failed',ended_at=:at,failed_error_code='HANDLER_FAILED' WHERE run_id=:run",
        at=ago(1),
        run=first,
    )
    items = await read_all(views, worker_setup, ExecutionLogQuery(level="warning"))
    assert [(item.run_id, item.level, item.code, item.node_id is None) for item in items] == [
        (first, "error", "HANDLER_FAILED", True),
        (second, "warning", "HANDLER_FAILED", False),
        (first, "error", "HANDLER_FAILED", False),
    ]
    assert items[1].message.endswith("; the failure was handled")
    assert {item.source for item in items} == {"engine"}
    errors = await read_all(views, worker_setup, ExecutionLogQuery(level="error", node_id=items[2].node_id))
    assert [(item.run_id, item.node_id) for item in errors] == [(first, items[2].node_id)]
