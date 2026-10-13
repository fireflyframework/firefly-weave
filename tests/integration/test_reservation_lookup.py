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

"""Trigger-side reservation lookups stay primary-key lookups however many reservations exist."""

import json
from uuid import UUID, uuid4

import pytest
import test_waits as wait_tests
from operations_support import assert_usage
from sqlalchemy import text

pytestmark = pytest.mark.integration
author = wait_tests.author
waiting_run = wait_tests.waiting_run

# Tables whose primary key the accounting trigger reaches with a value derived from TG_TABLE_NAME.
CONTROL = "operation_control_context"
LOOKUPS = {
    "operation_reservations": ("operation_reservations_pkey", ("kind", "resource_id")),
    "operation_allocations": ("operation_allocations_pkey", ("relation", "row_key")),
}


def scans(plan, found):
    if plan.get("Relation Name") in (*LOOKUPS, CONTROL) and "Scan" in plan["Node Type"]:
        found.append(plan)
    for child in plan.get("Plans", ()):
        scans(child, found)
    return found


async def explained_trigger(owner, statement, params):
    """Run one statement and return every plan auto_explain reports for the trigger's nested queries."""
    notices = []
    raw = (await (await owner.connection()).get_raw_connection()).driver_connection
    listener = lambda _, message: notices.append(message.message)  # noqa: E731
    raw.add_log_listener(listener)
    try:
        for setting in (
            "LOAD 'auto_explain'",
            "SET LOCAL auto_explain.log_min_duration=0",
            "SET LOCAL auto_explain.log_nested_statements=on",
            "SET LOCAL auto_explain.log_format=json",
            "SET LOCAL auto_explain.log_level=notice",
            "SET LOCAL client_min_messages=notice",
        ):
            await owner.execute(text(setting))
        await owner.execute(text(statement), params)
        await owner.execute(text("SET LOCAL auto_explain.log_min_duration=-1"))
    finally:
        raw.remove_log_listener(listener)
    return [json.loads(notice[notice.index("{") :]) for notice in notices if "plan:" in notice]


async def test_run_insert_uses_reservation_primary_key_among_thousands(waiting_run, access_db, author):
    ids = {"tenant": author[2].tenant_id, "project": author[2].project_id}
    async with access_db[1].begin() as owner:
        # Another project's reservations: the lookup must not cost time proportional to them.
        other_tenant, other_project = uuid4(), uuid4()
        await owner.execute(text("INSERT INTO tenants VALUES(:id,'filler')"), {"id": other_tenant})
        await owner.execute(
            text("INSERT INTO projects VALUES(:id,:tenant,'filler')"), {"id": other_project, "tenant": other_tenant}
        )
        await owner.execute(
            text(
                "INSERT INTO operation_reservations SELECT :tenant,:project,"
                "(ARRAY['run','provider','binding','webhook'])[1+n%4],gen_random_uuid(),0,0 "
                "FROM generate_series(1,6000) n"
            ),
            {"tenant": other_tenant, "project": other_project},
        )
        await owner.execute(
            text(
                "INSERT INTO operation_allocations SELECT :tenant,:project,"
                "(ARRAY['runs','run_events','task_intents'])[1+n%3],md5(n::text),1 "
                "FROM generate_series(1,6000) n"
            ),
            {"tenant": other_tenant, "project": other_project},
        )
        await owner.execute(text("ANALYZE operation_reservations"))
        await owner.execute(text("ANALYZE operation_allocations"))
        await assert_usage(owner, ids)
        original = (
            await owner.execute(
                text("SELECT (amount,outbox_slots) FROM operation_reservations WHERE kind='run' AND resource_id=:id"),
                {"id": UUID(waiting_run)},
            )
        ).scalar_one()
    copy = uuid4()
    async with access_db[1].begin() as owner:
        plans = await explained_trigger(
            owner,
            "INSERT INTO runs SELECT (jsonb_populate_record(r,jsonb_build_object('id',cast(:copy AS uuid)))).* "
            "FROM runs r WHERE id=cast(:run AS uuid)",
            {"copy": copy, "run": waiting_run},
        )
        found = [scan for plan in plans for scan in scans(plan["Plan"], [])]
        assert {*LOOKUPS, CONTROL} <= {scan["Relation Name"] for scan in found}, plans
        # The control context is empty outside a reserved control, so only its key collation is meaningful.
        assert not [scan for scan in found if 'COLLATE "C"' in scan.get("Index Cond", "") + scan.get("Filter", "")]
        for scan in (scan for scan in found if scan["Relation Name"] in LOOKUPS):
            index, columns = LOOKUPS[scan["Relation Name"]]
            condition = scan.get("Index Cond", "")
            assert scan["Node Type"] in {"Index Scan", "Index Only Scan"}, scan
            assert scan["Index Name"] == index, scan
            assert all(f"{column} =" in condition for column in columns), scan
        reserved = dict(
            (
                await owner.execute(
                    text(
                        "SELECT resource_id,(amount,outbox_slots) FROM operation_reservations "
                        "WHERE kind='run' AND resource_id IN (:copy,:run)"
                    ),
                    {"copy": copy, "run": UUID(waiting_run)},
                )
            ).all()
        )
        assert reserved[UUID(waiting_run)] == original
        amount, slots = reserved[copy]
        assert amount > 16777216 and slots == 64
        await assert_usage(owner, ids)
