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

"""Independent logical-usage inventory shared by operations integration tests."""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ZERO = UUID(int=0)


async def recompute_usage(connection: AsyncConnection, tenant: UUID, project: UUID) -> dict[tuple[UUID, str], int]:
    """Inventory charged tables and retained control provenance independently of counters."""
    tables = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE t.tgname='operation_usage' "
                    "AND n.nspname='public' "
                    "ORDER BY c.relname"
                )
            )
        )
        .scalars()
        .all()
    )
    totals = defaultdict(int)
    params = {"tenant": tenant, "project": project}
    for table in tables:
        projection = f"public.weave_{table}_account(r)" if table in ("runs", "run_events") else "to_jsonb(r)"
        rows = (
            await connection.execute(
                text(
                    f"SELECT item,public.weave_row_metrics('{table}',item) AS metrics FROM "
                    f"(SELECT {projection} AS item FROM {table} r) q WHERE "
                    "coalesce(item->>'tenant_id',"
                    "item->'event'->'scope'->>'tenant_id')=cast(cast(:tenant AS uuid) AS text) AND "
                    "coalesce(item->>'project_id',"
                    "item->'event'->'scope'->>'project_id')=cast(cast(:project AS uuid) AS text)"
                ),
                params,
            )
        ).mappings()
        for row in rows:
            for metric, amount in row["metrics"].items():
                dimension = (
                    ZERO
                    if metric in ("ordinary_bytes", "runs_retained", "debug_rows")
                    else UUID(row["item"].get("environment_id", str(ZERO)))
                )
                if metric == "outbox_pending" and await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM operation_control_deliveries WHERE tenant_id=:tenant "
                        "AND project_id=:project AND id=:id)"
                    ),
                    {**params, "id": row["item"]["id"]},
                ):
                    metric, dimension = "control_outbox_pending", ZERO
                totals[dimension, metric] += int(amount)
    allocation = (
        (
            await connection.execute(
                text(
                    "SELECT coalesce(sum(control_bytes),0) AS spent,count(*) AS rows "
                    "FROM operation_allocations WHERE tenant_id=:tenant AND project_id=:project"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    totals[ZERO, "ordinary_bytes"] -= int(allocation["spent"])
    totals[ZERO, "control_bytes"] += int(allocation["spent"]) + 512 * allocation["rows"]
    reservation = (
        (
            await connection.execute(
                text(
                    "SELECT coalesce(sum(amount),0) AS amount,"
                    "coalesce(sum(outbox_slots),0) AS slots,count(*) AS rows FROM operation_reservations "
                    "WHERE tenant_id=:tenant AND project_id=:project"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    totals[ZERO, "ordinary_bytes"] += 512 * reservation["rows"]
    totals[ZERO, "control_reserved"] += int(reservation["amount"])
    totals[ZERO, "control_outbox_reserved"] += int(reservation["slots"])
    return {key: value for key, value in totals.items() if value}


async def assert_usage(connection: AsyncConnection, ids: dict[str, UUID]) -> None:
    expected = await recompute_usage(connection, ids["tenant"], ids["project"])
    actual = (
        await connection.execute(
            text(
                "SELECT environment_id,metric,value FROM operation_usage "
                "WHERE tenant_id=:tenant AND project_id=:project AND value<>0"
            ),
            ids,
        )
    ).all()
    assert {(environment, metric): value for environment, metric, value in actual} == expected
