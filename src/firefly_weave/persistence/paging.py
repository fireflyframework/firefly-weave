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

"""Bounded transaction-local ID/size queries; authorization remains with each service."""

from uuid import UUID

from sqlalchemy import text

from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction

PAGE_BYTES = 8 * 1024 * 1024

TABLES = frozenset(
    {
        "connection_revisions",
        "teams_references",
        "provider_sources",
        "provider_receipts",
        "connection_source_bindings",
        "broker_routes",
        "broker_incidents",
        "runs",
        "event_subscriptions",
        "event_deliveries",
        "worker_releases",
        "worker_instances",
        "trigger_routes",
        "incidents",
    }
)


async def page_ids(
    tx: Transaction,
    table: str,
    limit: int,
    after: UUID | None,
    *,
    run_id: UUID | None = None,
    run_filters: dict[str, str | bool | None] | None = None,
) -> list[UUID]:
    if table not in TABLES or not 1 <= limit <= 100 or (run_id is not None and table != "incidents"):
        raise CatalogError(422, "WV-PAGE", "A bounded page is required")
    if run_filters is not None and table != "runs":
        raise CatalogError(422, "WV-PAGE", "Run filters require the runs collection")
    predicates = ""
    filter_params: dict[str, str | bool | None] = {}
    if run_filters is not None:
        for key in ("business_key", "correlation_key", "status"):
            value = run_filters.get(key)
            if value is not None:
                column = "state" if key == "status" else "request"
                predicates += f" AND {column}->>'{key}'=:filter_{key}"
                filter_params[f"filter_{key}"] = value
        if not run_filters.get("include_archived", False):
            predicates += " AND NOT EXISTS (SELECT 1 FROM run_archives a WHERE a.run_id=runs.id AND a.archived=true)"
    measurement = (
        "public.weave_runs_bytes(selected_row::public.runs)"
        if table == "runs"
        else "octet_length(to_jsonb(selected_row)::text)"
    )
    rows = await tx.session.execute(
        text(
            "SELECT selected_row.id, CASE WHEN row_number() OVER (ORDER BY selected_row.id) <= :page_limit "
            f"THEN {measurement} ELSE 0 END AS logical_bytes FROM ("
            f"SELECT * FROM {table} WHERE tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
            + (" AND id>:after" if after is not None else "")
            + (" AND run_id=:run" if run_id is not None else "")
            + predicates
            + " ORDER BY id LIMIT :limit) AS selected_row ORDER BY selected_row.id"
        ),
        {
            **filter_params,
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
            "after": after,
            "run": run_id,
            "limit": limit + 1,
            "page_limit": limit,
        },
    )
    selected = rows.all()
    # The owning read transaction keeps a repeatable snapshot until the callers fetch these IDs.
    # A mutation instead holds the project admission fence. Neither path needs pagination row locks.
    if sum(row.logical_bytes for row in selected[:limit]) > PAGE_BYTES:
        raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
    return [row.id for row in selected]
