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

"""Worker-owned SQL only; runtime rows are accessed through RuntimeService."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.worker_queries import RETIREMENT_BATCH, ReleaseUsagePosition, WorkerListQuery
from firefly_weave.contracts.workers import (
    CompletionReceipt,
    WorkerIdentity,
    WorkerInstance,
    WorkerRelease,
    WorkerStatus,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.fact_positions import RunPosition
from firefly_weave.persistence.paging import PAGE_BYTES
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import observed_worker, stored_identity, unavailable

SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
NOW = "cast(:now AS timestamptz)"
_W_SCOPE = "w.tenant_id=:tenant AND w.project_id=:project AND w.environment_id=:environment"
_FACTS = (
    "LEFT JOIN worker_facts f ON f.worker_id=w.id AND f.tenant_id=w.tenant_id AND f.project_id=w.project_id "
    "AND f.environment_id=w.environment_id"
)
# A lease that still holds its worker: active, and neither expired nor past its deadline.
_ACTIVE_LEASE = (
    "SELECT 1 FROM task_leases l WHERE l.tenant_id=w.tenant_id AND l.project_id=w.project_id "
    f"AND l.environment_id=w.environment_id AND l.owner=w.id AND l.status='active' AND l.expires_at>{NOW} "
    f"AND l.deadline>{NOW}"
)
# A worker's last contact; one never seen counts from its recorded registration, and one without either has none.
_CONTACT = "coalesce(w.last_seen_at,f.registered_at)"
_RETIRABLE = (
    f"FROM worker_instances w {_FACTS} WHERE {_W_SCOPE} AND NOT w.revoked "
    f"AND {_CONTACT}<cast(:cutoff AS timestamptz) AND NOT EXISTS({_ACTIVE_LEASE})"
)
# Placement: members of each permitted target's latest complete observation at the target's current revision,
# while that observation is unexpired, matched to the worker components of the target's deployments. A host that
# more than one placement lists has none. Observations and deployments are bounded per environment.
_PLACEMENT_CTES = (
    "latest_complete AS (SELECT DISTINCT ON (o.target_id) o.target_id,o.payload,t.payload->>'name' AS target_name "
    "FROM deployment_observations o JOIN deployment_targets t ON t.id=o.target_id AND t.tenant_id=o.tenant_id "
    "AND t.project_id=o.project_id AND t.environment_id=o.environment_id "
    "WHERE o.tenant_id=:tenant AND o.project_id=:project AND o.environment_id=:environment "
    "AND (:all_targets OR o.target_id=ANY(cast(:target_ids AS uuid[]))) "
    "AND (o.payload->>'complete')::boolean AND (o.payload->>'target_revision')::integer=t.revision "
    "ORDER BY o.target_id,(o.payload->>'observed_at')::timestamptz DESC,o.id DESC), "
    "placements AS (SELECT DISTINCT member->>'host' AS host,o.target_id,o.target_name,d.id AS deployment_id,"
    "component->>'name' AS component FROM latest_complete o "
    "CROSS JOIN LATERAL jsonb_array_elements(o.payload->'resources') resource "
    "CROSS JOIN LATERAL jsonb_array_elements(coalesce(resource->'members','[]'::jsonb)) member "
    "JOIN deployment_deployments d ON d.target_id=o.target_id AND d.tenant_id=:tenant AND d.project_id=:project "
    "AND d.environment_id=:environment CROSS JOIN LATERAL jsonb_array_elements(d.payload->'components') component "
    f"WHERE (o.payload->>'expires_at')::timestamptz>{NOW} AND resource->>'name'=component->>'name' "
    "AND resource->>'kind'='worker' AND component->>'kind'='worker'), "
    "unique_placement AS (SELECT host,(jsonb_agg(jsonb_build_object('target_id',target_id,'target_name',"
    "target_name,'deployment_id',deployment_id,'component',component))->0) AS placement FROM placements "
    "GROUP BY host HAVING count(*)=1)"
)
# Filters bind as typed nulls, so every filter combination is the same statement.
_FLEET_FILTERS = (
    f"(:all_workers OR (NOT revoked AND coalesce(last_seen_at,registered_at)>{NOW}-interval '1 hour')) "
    "AND (cast(:presence AS text) IS NULL OR presence=cast(:presence AS text)) "
    "AND (cast(:draining AS boolean) IS NULL OR draining=cast(:draining AS boolean)) "
    "AND (cast(:release AS uuid) IS NULL OR release_id=cast(:release AS uuid)) "
    "AND (cast(:task AS text) IS NULL OR payload->'task_types' @> jsonb_build_array(cast(:task AS text))) "
    "AND (cast(:host_prefix AS text) IS NULL OR starts_with(identity->>'host',cast(:host_prefix AS text))) "
    "AND (cast(:target AS uuid) IS NULL OR (placement->>'target_id')::uuid=cast(:target AS uuid))"
)


class WorkerRepository:
    def __init__(self, tx: Transaction):
        self.tx = tx
        self.params = dict(tenant=tx.scope.tenant_id, project=tx.scope.project_id, environment=tx.scope.environment_id)

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.params, **values})
        return [dict(r) for r in result.mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.params, **values})

    async def now(self) -> datetime:
        value: datetime = await self.tx.session.scalar(text("SELECT clock_timestamp()"))
        return value

    async def release(self, identifier: UUID) -> WorkerRelease:
        rows = await self.rows(f"SELECT payload FROM worker_releases WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise unavailable()
        return WorkerRelease.model_validate_json(json.dumps(rows[0]["payload"]))

    async def instance(self, identifier: UUID, *, lock: bool = False) -> WorkerInstance:
        rows = await self.rows(
            f"SELECT payload,revoked FROM worker_instances WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows or rows[0]["revoked"]:
            raise unavailable()
        return WorkerInstance.model_validate_json(json.dumps(rows[0]["payload"]))

    async def draining(self, identifier: UUID) -> bool:
        rows = await self.rows(f"SELECT draining FROM worker_instances WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise unavailable()
        return bool(rows[0]["draining"])

    async def observe(self, identifier: UUID) -> None:
        # Idle polling is frequent; coalesce observations without inventing liveness.
        await self.execute(
            f"UPDATE worker_instances SET last_seen_at=clock_timestamp() WHERE {SCOPE} AND id=:id "
            "AND NOT revoked AND (last_seen_at IS NULL OR last_seen_at<clock_timestamp()-interval '5 seconds')",
            id=identifier,
        )

    async def record_registration(self, identifier: UUID, identity: WorkerIdentity | None) -> None:
        """Record a new instance's facts in its registration transaction, never in its payload."""
        await self.execute(
            "INSERT INTO worker_facts(tenant_id,project_id,environment_id,worker_id,registered_at,identity) "
            "VALUES(:tenant,:project,:environment,:id,clock_timestamp(),cast(:identity AS jsonb))",
            id=identifier,
            identity=json.dumps({} if identity is None else identity.model_dump(mode="json")),
        )

    async def record_drain(self, identifier: UUID, *, draining: bool, was_draining: bool) -> None:
        """Record when a worker starts draining; a repeated drain keeps that time and resuming clears it."""
        if draining and not was_draining:
            await self.execute(
                f"UPDATE worker_facts SET drained_at=clock_timestamp() WHERE {SCOPE} AND worker_id=:id", id=identifier
            )
        elif not draining:
            await self.execute(
                f"UPDATE worker_facts SET drained_at=NULL WHERE {SCOPE} AND worker_id=:id AND drained_at IS NOT NULL",
                id=identifier,
            )

    async def fleet(
        self,
        query: WorkerListQuery,
        position: RunPosition | None,
        *,
        observed_at: datetime,
        authorized_targets: tuple[UUID, ...] | None,
        worker_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """One page of workers and its lookahead row, with grouped active leases and permitted placement.

        One statement serves every filter, order and position. `authorized_targets` is None for every target and
        empty for none. A page above the logical byte limit is refused before its payloads are returned.
        """
        column = "last_seen_at" if query.order == "last_seen_desc" else "registered_at"
        keyset = ""
        bounds: dict[str, Any] = {}
        if position is not None:
            bounds["position_id"] = position.id
            if position.at is None:
                keyset = f" AND {column} IS NULL AND id<cast(:position_id AS uuid)"
            else:
                # Unknown times come after every known one.
                keyset = (
                    f" AND (({column},id)<(cast(:position_at AS timestamptz),cast(:position_id AS uuid)) "
                    f"OR {column} IS NULL)"
                )
                bounds["position_at"] = position.at
        order = f"{column} DESC NULLS LAST,id DESC"
        one = worker_id is not None
        sql = (
            "WITH active AS (SELECT owner,count(*) AS active_leases FROM task_leases "
            "WHERE tenant_id=:tenant AND project_id=:project AND environment_id=:environment AND status='active' "
            f"AND expires_at>{NOW} AND deadline>{NOW}"
            + (" AND owner=:worker" if one else "")
            + " GROUP BY owner), "
            + _PLACEMENT_CTES
            + ", fleet AS (SELECT w.id,w.release_id,w.payload,w.revoked,w.draining,w.control_revision,"
            "w.last_seen_at,f.registered_at,f.identity,f.drained_at,coalesce(a.active_leases,0) AS active_leases,"
            f"CASE WHEN w.last_seen_at IS NULL THEN 'unknown' WHEN w.last_seen_at>{NOW}-interval '60 seconds' "
            "THEN 'recent' ELSE 'stale' END AS presence,p.placement "
            f"FROM worker_instances w {_FACTS} LEFT JOIN active a ON a.owner=w.id "
            "LEFT JOIN unique_placement p ON p.host=f.identity->>'host' AND f.identity->>'runtime'<>'embedded' "
            f"WHERE {_W_SCOPE}" + (" AND w.id=:worker" if one else "") + "), "
            f"page AS (SELECT fleet.*,row_number() OVER (ORDER BY {order}) AS page_row FROM fleet "
            f"WHERE {_FLEET_FILTERS}{keyset} ORDER BY {order} LIMIT :page_size), "
            "measured AS (SELECT page.*,sum(CASE WHEN page_row<=:limit THEN octet_length(payload::text)"
            "+octet_length(coalesce(identity,'{}'::jsonb)::text) ELSE 0 END) OVER () AS page_bytes FROM page) "
            "SELECT id,release_id,revoked,draining,control_revision,last_seen_at,registered_at,drained_at,"
            "active_leases,presence,placement,CASE WHEN page_bytes<=:budget THEN payload END AS payload,"
            "CASE WHEN page_bytes<=:budget THEN identity END AS identity,page_bytes>:budget AS oversized "
            "FROM measured ORDER BY page_row"
        )
        rows = await self.rows(
            sql,
            now=observed_at,
            worker=worker_id,
            all_targets=authorized_targets is None,
            target_ids=list(authorized_targets or ()),
            all_workers=query.state == "all",
            presence=query.presence,
            draining=query.draining,
            release=query.release_id,
            task=query.task_type,
            host_prefix=query.host_prefix,
            target=query.target_id,
            page_size=query.limit + 1,
            limit=query.limit,
            budget=PAGE_BYTES,
            **bounds,
        )
        if any(row["oversized"] for row in rows):
            raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
        return rows

    async def retirable(self, cutoff: datetime, now: datetime) -> int:
        """Every worker a retirement at this cutoff would revoke, across all batches."""
        rows = await self.rows(f"SELECT count(*) AS count {_RETIRABLE}", cutoff=cutoff, now=now)
        return int(rows[0]["count"])

    async def retirement_batch(self, cutoff: datetime, now: datetime, *, lock: bool) -> list[UUID]:
        """The oldest retirable workers, oldest last contact first; locking skips rows another command holds."""
        rows = await self.rows(
            f"SELECT w.id {_RETIRABLE} ORDER BY {_CONTACT} ASC,w.id ASC LIMIT :batch"
            + (" FOR UPDATE OF w SKIP LOCKED" if lock else ""),
            cutoff=cutoff,
            now=now,
            batch=RETIREMENT_BATCH,
        )
        return [row["id"] for row in rows]

    async def retire(self, identifiers: list[UUID], cutoff: datetime, now: datetime) -> list[UUID]:
        """Revoke the locked workers that still qualify; the conditions are checked again on the locked rows."""
        if not identifiers:
            return []
        rows = await self.rows(
            "UPDATE worker_instances w SET revoked=true,control_revision=w.control_revision+1 "
            f"WHERE {_W_SCOPE} AND w.id=ANY(cast(:ids AS uuid[])) AND NOT w.revoked "
            "AND coalesce(w.last_seen_at,(SELECT f.registered_at FROM worker_facts f WHERE f.worker_id=w.id "
            "AND f.tenant_id=w.tenant_id AND f.project_id=w.project_id AND f.environment_id=w.environment_id))"
            f"<cast(:cutoff AS timestamptz) AND NOT EXISTS({_ACTIVE_LEASE}) RETURNING w.id",
            ids=identifiers,
            cutoff=cutoff,
            now=now,
        )
        revoked = {row["id"] for row in rows}
        return [identifier for identifier in identifiers if identifier in revoked]

    async def release_digest(self, identifier: UUID) -> str:
        rows = await self.rows(f"SELECT image_digest FROM worker_releases WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
        return str(rows[0]["image_digest"])

    async def release_workers(self, identifier: UUID, now: datetime) -> dict[str, int]:
        rows = await self.rows(
            "SELECT count(*) FILTER (WHERE revoked) AS revoked,"
            "count(*) FILTER (WHERE NOT revoked AND draining) AS draining,"
            f"count(*) FILTER (WHERE NOT revoked AND NOT draining AND last_seen_at>{NOW}-interval '60 seconds') "
            "AS online,count(*) FILTER (WHERE NOT revoked AND NOT draining AND (last_seen_at IS NULL "
            f"OR last_seen_at<={NOW}-interval '60 seconds')) AS offline "
            f"FROM worker_instances WHERE {SCOPE} AND release_id=:release",
            release=identifier,
            now=now,
        )
        return {key: int(value) for key, value in rows[0].items()}

    async def release_activations(self, identifier: UUID) -> int:
        """Activation revisions pinning the release that can still start runs.

        A revision counts unless it, the latest revision of the same activation, or its workflow version is
        retired, as the start check decides.
        """
        pinned = (
            "EXISTS(SELECT 1 FROM jsonb_each_text(coalesce(a.payload->'request'->'{key}','{{}}'::jsonb)) pin "
            "WHERE pin.value=:release)"
        )
        rows = await self.rows(
            "SELECT count(*) AS count FROM activation_revisions a WHERE a.tenant_id=:tenant AND a.project_id=:project "
            f"AND a.environment_id=:environment AND ({pinned.format(key='worker_release_ids')} "
            f"OR {pinned.format(key='connector_release_ids')}) "
            "AND NOT coalesce((a.payload->>'retired')::boolean,false) "
            "AND NOT coalesce((SELECT (l.payload->>'retired')::boolean FROM activation_revisions l "
            "WHERE l.tenant_id=a.tenant_id AND l.project_id=a.project_id AND l.environment_id=a.environment_id "
            "AND l.name=a.name ORDER BY l.revision DESC LIMIT 1),false) "
            "AND NOT EXISTS(SELECT 1 FROM definition_retirements r WHERE r.tenant_id=a.tenant_id "
            "AND r.project_id=a.project_id AND r.version_id=a.version_id)",
            release=str(identifier),
        )
        return int(rows[0]["count"])

    async def release_deployments(
        self,
        identifier: UUID,
        digest: str,
        *,
        authorized_targets: tuple[UUID, ...] | None,
        position: ReleaseUsagePosition | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Deployment components that run exactly this admitted release, in deployment and component order."""
        keyset = ""
        bounds: dict[str, Any] = {}
        if position is not None:
            keyset = (
                " AND (d.id,(component->>'name') COLLATE \"C\")>"
                '(cast(:deployment AS uuid),cast(:component AS text) COLLATE "C")'
            )
            bounds = dict(deployment=position.deployment_id, component=position.component)
        return await self.rows(
            "SELECT d.target_id,t.payload->>'name' AS target_name,d.id AS deployment_id,"
            "d.payload->>'name' AS deployment_name,component->>'name' AS component,"
            "(component->>'replicas')::integer AS replicas FROM deployment_deployments d "
            "JOIN deployment_targets t ON t.id=d.target_id AND t.tenant_id=d.tenant_id AND t.project_id=d.project_id "
            "AND t.environment_id=d.environment_id "
            "CROSS JOIN LATERAL jsonb_array_elements(d.payload->'components') component "
            "WHERE d.tenant_id=:tenant AND d.project_id=:project AND d.environment_id=:environment "
            "AND (:all_targets OR d.target_id=ANY(cast(:target_ids AS uuid[]))) "
            "AND component->>'worker_release_id'=:release AND split_part(component->>'image','@',2)=:digest"
            + keyset
            + " ORDER BY d.id,(component->>'name') COLLATE \"C\" LIMIT :limit",
            release=str(identifier),
            digest=digest,
            all_targets=authorized_targets is None,
            target_ids=list(authorized_targets or ()),
            limit=limit + 1,
            **bounds,
        )

    async def status(self, identifier: UUID, observed_at: datetime | None = None) -> WorkerStatus:
        now = observed_at or await self.now()
        rows = await self.rows(
            "SELECT w.payload,w.revoked,w.draining,w.control_revision,w.last_seen_at,"
            "f.identity,f.registered_at,f.drained_at FROM worker_instances w LEFT JOIN worker_facts f "
            "ON f.worker_id=w.id AND f.tenant_id=w.tenant_id AND f.project_id=w.project_id "
            "AND f.environment_id=w.environment_id WHERE w.tenant_id=:tenant AND w.project_id=:project "
            "AND w.environment_id=:environment AND w.id=:id",
            id=identifier,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
        row = rows[0]
        active = await self.rows(
            f"SELECT count(*) AS count FROM task_leases WHERE {SCOPE} AND owner=:id "
            "AND status='active' AND expires_at>:now AND deadline>:now",
            id=identifier,
            now=now,
        )
        return observed_worker(
            WorkerInstance.model_validate_json(json.dumps({**row["payload"], "revoked": row["revoked"]})),
            last_seen_at=row["last_seen_at"],
            draining=row["draining"],
            revision=row["control_revision"],
            active_leases=active[0]["count"],
            observed_at=now,
            identity=stored_identity(row["identity"]),
            registered_at=row["registered_at"],
            drained_at=row["drained_at"],
        )

    async def lease(self, task: UUID, generation: int) -> dict[str, Any]:
        rows = await self.rows(
            f"SELECT * FROM task_leases WHERE {SCOPE} AND task_id=:task AND generation=:generation FOR UPDATE",
            task=task,
            generation=generation,
        )
        if not rows:
            raise unavailable()
        return rows[0]

    async def receipt(self, task: UUID, completion: UUID) -> CompletionReceipt | None:
        rows = await self.rows(
            f"SELECT payload FROM completion_receipts WHERE {SCOPE} AND task_id=:task AND completion_id=:completion",
            task=task,
            completion=completion,
        )
        return CompletionReceipt.model_validate_json(json.dumps(rows[0]["payload"])) if rows else None
