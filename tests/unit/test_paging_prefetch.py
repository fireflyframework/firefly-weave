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

"""Selected-row bytes are bounded before payload loading without changing page identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.paging import TABLES, page_ids
from firefly_weave.persistence.uow import Transaction

CEILING = 8 * 1024 * 1024


def transaction(sizes):
    rows = [SimpleNamespace(id=UUID(int=i + 1), logical_bytes=size) for i, size in enumerate(sizes)]
    # Both result views are provided so the original ID-only implementation reaches the regression assertions.
    result = SimpleNamespace(all=lambda: rows, scalars=lambda: [r.id for r in rows])
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    return Transaction(session=session, scope=scope), rows


@pytest.mark.parametrize("sizes", [[CEILING + 1], [CEILING // 2, CEILING // 2 + 1]])
async def test_oversized_selected_page_fails_before_any_payload_query(sizes):
    tx, _ = transaction(sizes)
    with pytest.raises(CatalogError) as rejected:
        await page_ids(tx, "runs", len(sizes), None)
    assert (rejected.value.status, rejected.value.code) == (429, "WV-PAGE-LIMIT")
    assert rejected.value.result is None
    assert str(tx.scope.tenant_id) not in rejected.value.message
    assert tx.session.execute.await_count == 1


@pytest.mark.parametrize(
    "sizes,limit", [([], 50), ([CEILING], 1), ([CEILING // 2, CEILING // 2], 2), ([1, CEILING * 4], 1)]
)
async def test_empty_exact_ceiling_and_oversized_lookahead_preserve_ids(sizes, limit):
    tx, rows = transaction(sizes)
    assert await page_ids(tx, "runs", limit, None) == [r.id for r in rows]
    assert tx.session.execute.await_count == 1


@pytest.mark.parametrize("table", sorted(TABLES))
async def test_allowlisted_queries_preserve_scope_order_and_one_row_lookahead(table):
    tx, rows = transaction([1, 2, 3])
    after = uuid4()
    run_id = uuid4() if table == "incidents" else None
    assert await page_ids(tx, table, 2, after, run_id=run_id) == [r.id for r in rows]
    statement, parameters = tx.session.execute.await_args.args
    sql = str(statement)
    assert f"FROM {table} " in sql
    assert "tenant_id=:tenant AND project_id=:project AND environment_id=:environment" in sql
    assert "AND id>:after" in sql
    assert ("AND run_id=:run" in sql) == (run_id is not None)
    assert "ORDER BY id LIMIT :limit" in sql
    assert parameters == {
        "tenant": tx.scope.tenant_id,
        "project": tx.scope.project_id,
        "environment": tx.scope.environment_id,
        "after": after,
        "run": run_id,
        "limit": 3,
        "page_limit": 2,
    }
    expected = (
        "public.weave_runs_bytes(selected_row::public.runs)"
        if table == "runs"
        else "octet_length(to_jsonb(selected_row)::text)"
    )
    assert expected in sql
    assert "row_number() OVER (ORDER BY selected_row.id) <= :page_limit" in sql
    assert "ELSE 0 END AS logical_bytes" in sql
    assert sql.startswith("SELECT selected_row.id, CASE")
    assert sql.endswith("ORDER BY selected_row.id")
    assert all(lock not in sql.upper() for lock in ("FOR UPDATE", "FOR SHARE", "FOR KEY SHARE"))


async def test_no_cursor_or_run_filter_is_added_when_not_requested():
    tx, _ = transaction([])
    await page_ids(tx, "incidents", 100, None)
    sql = str(tx.session.execute.await_args.args[0])
    assert "id>:after" not in sql and "run_id=:run" not in sql
    assert tx.session.execute.await_args.args[1]["limit"] == 101


@pytest.mark.parametrize(
    "table,limit,run",
    [
        ("runs; SELECT secret", 1, None),
        ("principals", 1, None),
        ("runs", 0, None),
        ("runs", 101, None),
        ("runs", 1, UUID(int=1)),
    ],
)
async def test_invalid_page_is_rejected_before_database_access(table, limit, run):
    tx, _ = transaction([])
    with pytest.raises(CatalogError) as rejected:
        await page_ids(tx, table, limit, None, run_id=run)
    assert (rejected.value.status, rejected.value.code) == (422, "WV-PAGE")
    tx.session.execute.assert_not_awaited()
