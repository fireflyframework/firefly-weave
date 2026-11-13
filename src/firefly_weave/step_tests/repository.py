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
"""Storage of step tests: the saved draft, the synthetic version and test activation, their details and releases."""

import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.values import JsonObject
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.persistence.uow import Transaction
from firefly_weave.step_tests.admission import ReleaseChoice

SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
# Releases are read in one bounded page; an environment with more can't be offered as choices.
RELEASE_PAGE = 200


class StepTestRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.keys = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.keys, **values})
        return [dict(row) for row in result.mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.keys, **values})

    async def now(self) -> datetime:
        return cast(datetime, await self.tx.session.scalar(text("SELECT clock_timestamp()")))

    async def environment_name(self) -> str:
        rows = await self.rows(
            "SELECT name FROM environments WHERE tenant_id=:tenant AND project_id=:project AND id=:environment"
        )
        return str(rows[0]["name"]) if rows else str(self.tx.scope.environment_id)

    async def draft(self, identifier: UUID, revision: int) -> JsonObject | None:
        """The saved draft at this revision, unless the draft was retired."""
        rows = await self.rows(
            "SELECT document FROM draft_revisions d WHERE tenant_id=:tenant AND project_id=:project AND id=:id "
            "AND revision=:revision AND NOT EXISTS(SELECT 1 FROM draft_retirements r WHERE r.tenant_id=d.tenant_id "
            "AND r.project_id=d.project_id AND r.id=d.id)",
            id=identifier,
            revision=revision,
        )
        return cast(JsonObject, rows[0]["document"]) if rows else None

    async def connector_version(self, digest: str) -> UUID | None:
        rows = await self.rows(
            "SELECT id FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project AND kind='Connector' "
            "AND definition_digest=:digest AND NOT test ORDER BY id LIMIT 1",
            digest=digest,
        )
        return cast(UUID, rows[0]["id"]) if rows else None

    async def releases(self) -> list[WorkerRelease]:
        rows = await self.rows(f"SELECT payload FROM worker_releases WHERE {SCOPED} ORDER BY id LIMIT {RELEASE_PAGE}")
        return [WorkerRelease.model_validate_json(json.dumps(row["payload"])) for row in rows]

    async def release_choices(self, artifact: CompiledArtifact) -> dict[str, tuple[ReleaseChoice, ...]]:
        """For each task and connector the step needs, the releases of this environment that offer it exactly."""
        executable = artifact.executable
        dependencies = cast(list[JsonObject], executable["dependencies"])
        releases = await self.releases()

        def label(release: WorkerRelease) -> str:
            return "Worker release " + release.image_digest.removeprefix("sha256:")[:12]

        needed: dict[str, tuple[ReleaseChoice, ...]] = {}
        for dependency in dependencies:
            if dependency["kind"] != "Action":
                continue
            implementation = cast(
                JsonObject, cast(JsonObject, cast(JsonObject, dependency["document"])["spec"])["implementation"]
            )
            if implementation["kind"] == "worker":
                reference = f"{implementation['taskType']}@{implementation['taskVersion']}"
                pinned = next(
                    (d for d in dependencies if d["kind"] == "TaskCapability" and d["reference"] == reference), None
                )
                needed[reference] = tuple(
                    ReleaseChoice(release.id, label(release))
                    for release in releases
                    if pinned is not None
                    and (
                        offered := CatalogSnapshot.from_definitions([], tasks=release.capabilities).resolve(
                            "TaskCapability", reference
                        )
                    )
                    is not None
                    and offered.digest == pinned["digest"]
                )
            else:
                reference = cast(str, implementation["uses"])
                connector = next(d for d in dependencies if d["kind"] == "Connector" and d["reference"] == reference)
                needed[reference] = tuple(
                    ReleaseChoice(release.id, label(release))
                    for release in releases
                    if any(
                        binding.connector_digest == connector["digest"] and binding.action == implementation["action"]
                        for binding in release.connector_bindings
                    )
                )
        return needed

    async def insert_version(
        self, identifier: UUID, workflow: JsonObject, artifact: CompiledArtifact, definition_digest: str
    ) -> None:
        """The synthetic workflow version, marked test; written as publishing writes a version and its source."""
        metadata = cast(JsonObject, workflow["metadata"])
        await self.execute(
            "INSERT INTO definition_versions(id,tenant_id,project_id,kind,name,version,digest,definition_digest,"
            "document,artifact,test) VALUES(:id,:tenant,:project,'Workflow',:name,:version,:digest,"
            ":definition_digest,cast(:document AS jsonb),cast(:artifact AS jsonb),true)",
            id=identifier,
            name=metadata["name"],
            version=metadata["version"],
            digest=artifact.digest,
            definition_digest=definition_digest,
            document=json.dumps(workflow),
            artifact=artifact.to_bytes().decode(),
        )
        await self.execute(
            "INSERT INTO definition_sources(id,tenant_id,project_id,version_id,source,format,source_hash,envelope) "
            "VALUES(:id,:tenant,:project,:version,:source,'json',:hash,cast(:envelope AS jsonb))",
            id=uuid4(),
            version=identifier,
            source=json.dumps(workflow),
            hash=artifact.source_hash,
            envelope=artifact.to_bytes().decode(),
        )

    async def insert_activation(self, activation: Activation) -> None:
        """The test activation, its connections and its connector releases; never listed or looked up by name."""
        await self.execute(
            "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,"
            "payload,test) VALUES(:id,:tenant,:project,:environment,:name,:revision,:version,"
            "cast(:payload AS jsonb),true)",
            id=activation.id,
            name=activation.name,
            revision=activation.revision,
            version=activation.request.version_id,
            payload=activation.model_dump_json(),
        )
        for slot, revision in activation.request.connection_revision_ids.items():
            await self.execute(
                "INSERT INTO activation_connections VALUES(:tenant,:project,:environment,:activation,:slot,:revision)",
                activation=activation.id,
                slot=slot,
                revision=revision,
            )
        for connector, release in activation.request.connector_release_ids.items():
            await self.execute(
                "INSERT INTO activation_connector_releases VALUES(:tenant,:project,:environment,:activation,"
                ":connector,:release)",
                activation=activation.id,
                connector=connector,
                release=release,
            )

    async def insert_details(
        self,
        activation: UUID,
        principal: UUID,
        *,
        draft: UUID,
        draft_revision: int,
        step_id: str,
        side_effect: SideEffect,
        acknowledged: SideEffect | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        await self.execute(
            "INSERT INTO test_activation_details(tenant_id,project_id,environment_id,activation_id,principal_id,"
            "source,draft_id,draft_revision,step_id,side_effect,acknowledged_side_effect,created_at,expires_at) "
            "VALUES(:tenant,:project,:environment,:activation,:principal,'step',:draft,:draft_revision,:step,"
            ":effect,:acknowledged,:created,:expires)",
            activation=activation,
            principal=principal,
            draft=draft,
            draft_revision=draft_revision,
            step=step_id,
            effect=side_effect,
            acknowledged=acknowledged,
            created=created_at,
            expires=expires_at,
        )

    async def busy(self, principal: UUID, now: datetime) -> tuple[list[datetime], list[datetime]]:
        """When each of this person's running step tests here expires, and when each of the last minute's started."""
        running = await self.rows(
            "SELECT d.expires_at FROM test_activation_details d LEFT JOIN runs r ON r.tenant_id=d.tenant_id "
            "AND r.project_id=d.project_id AND r.environment_id=d.environment_id AND r.id=d.run_id "
            "WHERE d.tenant_id=:tenant AND d.project_id=:project AND d.environment_id=:environment "
            "AND d.principal_id=:principal AND d.retired_at IS NULL AND d.expires_at>:now "
            "AND coalesce(r.state->>'status','running') NOT IN ('succeeded','failed','cancelled','timed_out')",
            principal=principal,
            now=now,
        )
        recent = await self.rows(
            f"SELECT created_at FROM test_activation_details WHERE {SCOPED} AND principal_id=:principal "
            "AND created_at>:since",
            principal=principal,
            since=now - timedelta(seconds=60),
        )
        return [row["expires_at"] for row in running], [row["created_at"] for row in recent]

    async def record_run(self, activation: UUID, run: UUID) -> None:
        await self.execute(
            f"UPDATE test_activation_details SET run_id=:run WHERE {SCOPED} AND activation_id=:activation",
            activation=activation,
            run=run,
        )

    async def details(self, activation: UUID) -> dict[str, Any] | None:
        rows = await self.rows(
            f"SELECT * FROM test_activation_details WHERE {SCOPED} AND activation_id=:activation AND source='step'",
            activation=activation,
        )
        return rows[0] if rows else None

    async def run_status(self, run: UUID) -> str | None:
        rows = await self.rows(f"SELECT state->>'status' AS status FROM runs WHERE {SCOPED} AND id=:run", run=run)
        return cast(str, rows[0]["status"]) if rows else None
