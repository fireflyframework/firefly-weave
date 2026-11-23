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

"""Incidents of transactions on real PostgreSQL: transactions in doubt listed first through the priority filter."""

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.transactions import IN_DOUBT_CODES
from firefly_weave.operations.incidents import IncidentService

pytestmark = pytest.mark.integration

IN_DOUBT = sorted(IN_DOUBT_CODES)
OTHER = ["WV-RUNTIME-STATE", "WV-TASK-FAILED", "WV-TASK-AMBIGUOUS", "WV-RUNTIME-OUTPUT"]
CODES = IN_DOUBT + OTHER


def incidents(case) -> IncidentService:
    return IncidentService(case.runtime, case.access, case.tasks)


async def seed(case, run_id, count, *, prefix):
    """Incidents of every in-doubt code and four others, with tied and unknown opening times and every status."""
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
                "origin_code,code,status,revision) SELECT md5(:prefix||r.id::text||g)::uuid,r.tenant_id,"
                "r.project_id,r.environment_id,r.id,:prefix||':'||g,'work',g,'WV-TASK-FAILED',"
                "(cast(:codes AS text[]))[1+g%cardinality(cast(:codes AS text[]))],"
                "(ARRAY['active','closed','resolved'])[1+g%3],1 FROM runs r,generate_series(1,:count) g "
                "WHERE r.id=:run"
            ),
            {"prefix": prefix, "run": run_id, "count": count, "codes": CODES},
        )
        await session.execute(
            text(
                "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,"
                "opened_at,updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT i.tenant_id,"
                "i.project_id,i.environment_id,i.id,'work','',CASE WHEN i.generation%7=0 THEN NULL ELSE "
                "timestamptz '2026-10-01T00:00:00Z'+(i.generation%5)*interval '1 minute' END,"
                "timestamptz '2026-10-02T00:00:00Z',f.workflow_name,f.workflow_version,false,false "
                "FROM incidents i JOIN run_facts f ON f.run_id=i.run_id WHERE i.run_id=:run "
                "AND i.incident_key LIKE :prefix||':%'"
            ),
            {"prefix": prefix, "run": run_id},
        )


def position_of(scope, query, cursor):
    from firefly_weave.api.transport import decode_cursor, decode_cursor_v2
    from firefly_weave.operations.fact_positions import run_position

    collection = query.cursor_collection()
    if query.order == "id":
        return decode_cursor(cursor, scope, collection)
    return run_position(decode_cursor_v2(cursor, scope, collection))


async def walk(case, values) -> list[dict]:
    """Every item of a listing, page by page through its scope-bound cursors."""
    service, position, found = incidents(case), None, []
    query = IncidentQuery(**values)
    while True:
        page = await service.list_all(case.actor, case.scope, query=query, position=position, context=AuditContext())
        assert len(page["items"]) <= query.limit
        found += page["items"]
        if page["next_cursor"] is None:
            return found
        position = position_of(case.scope, query, page["next_cursor"])


async def expected_order(case, order, *, in_doubt: bool | None = None, statuses=None) -> list[str]:
    direction = {"opened_desc": "DESC", "opened_asc": "ASC"}.get(order)
    sort = f"f.opened_at {direction} NULLS LAST,i.id {direction}" if direction else "i.id ASC"
    rows = await case.rows(
        "SELECT i.id FROM incidents i LEFT JOIN incident_facts f ON f.incident_id=i.id "
        "WHERE (cast(:in_doubt AS boolean) IS NULL OR (i.code=ANY(cast(:codes AS text[])))=cast(:in_doubt AS boolean)) "
        "AND (cast(:statuses AS text[]) IS NULL OR i.status=ANY(cast(:statuses AS text[]))) ORDER BY " + sort,
        in_doubt=in_doubt,
        codes=IN_DOUBT,
        statuses=statuses,
    )
    return [str(row["id"]) for row in rows]


@pytest.fixture
async def seeded(operations_case):
    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    await seed(case, first.id, 60, prefix="a")
    await seed(case, second.id, 17, prefix="b")
    return case


async def test_priority_high_lists_only_incidents_in_doubt(seeded):
    items = await walk(seeded, {"priority": "high", "limit": 7})
    assert [item["id"] for item in items] == await expected_order(seeded, "opened_desc", in_doubt=True)
    assert {item["code"] for item in items} == set(IN_DOUBT)
    assert {item.get("priority") for item in items} == {"high"}


async def test_priority_normal_lists_every_other_incident(seeded):
    items = await walk(seeded, {"priority": "normal", "limit": 7})
    assert [item["id"] for item in items] == await expected_order(seeded, "opened_desc", in_doubt=False)
    assert {item["code"] for item in items} == set(OTHER)
    assert all("priority" not in item for item in items)


@pytest.mark.parametrize("order", ["opened_desc", "opened_asc", "id"])
async def test_both_bands_together_list_every_incident_once(seeded, order):
    for values in ({"order": order, "limit": 50}, {"order": order, "limit": 4, "status": ["active"]}):
        everything = await expected_order(seeded, order, statuses=values.get("status"))
        high = [item["id"] for item in await walk(seeded, {**values, "priority": "high"})]
        normal = [item["id"] for item in await walk(seeded, {**values, "priority": "normal"})]
        assert not set(high) & set(normal)
        assert sorted(high + normal) == sorted(everything)
        # Each band keeps the order of the unfiltered list.
        assert high == [identifier for identifier in everything if identifier in set(high)]
        assert normal == [identifier for identifier in everything if identifier in set(normal)]
