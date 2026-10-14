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

"""Immutable release admission and scoped worker identities."""

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pyfly.container.stereotypes import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import executable_node_kinds
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.worker_queries import (
    ReleaseDeploymentUsage,
    ReleaseUsage,
    ReleaseUsageQuery,
    ReleaseWorkerCounts,
    RetireStaleRequest,
    RetireStaleResult,
    WorkerListQuery,
    release_usage_position,
)
from firefly_weave.contracts.workers import (
    ConnectorExecutionPin,
    CredentialGrantRequest,
    InstanceRequest,
    ReleaseRequest,
    WorkerControlRequest,
    WorkerInstance,
    WorkerRelease,
    WorkerStatus,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.ports import ConnectionBindingPort, WorkerAdmissionPort
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.fact_positions import RunPosition
from firefly_weave.operations.permissions import target_permissions
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import fleet_status
from firefly_weave.workers.repository import SCOPE, WorkerRepository


@service
class WorkerService(WorkerAdmissionPort):
    def __init__(self, definitions: DefinitionService, connections: ConnectionBindingPort) -> None:
        self.definitions = definitions
        self.connections = connections

    async def require(
        self,
        actor: Principal,
        scope: Scope,
        capability: str,
        context: AuditContext,
        tx: Transaction,
        resource: str | None = None,
    ) -> None:
        if scope.environment_id is None:
            raise AccessDenied()
        for current in (actor, await load_principal(tx.session, actor.id)):
            self.definitions.authorization.require(current, scope, capability, resource=resource, context=context)

    def precheck(
        self, actor: Principal, scope: Scope, capability: str, context: AuditContext, resource: str | None = None
    ) -> None:
        """`capability` on the token principal, before any unit of work opens."""
        if scope.environment_id is None:
            raise AccessDenied()
        self.definitions.authorization.require(actor, scope, capability, resource=resource, context=context)

    async def current(
        self,
        actor: Principal,
        scope: Scope,
        capability: str,
        context: AuditContext,
        tx: Transaction,
        resource: str | None = None,
    ) -> Principal:
        """`capability` on the principal stored now, inside the caller's unit of work; `precheck` runs first."""
        current = await load_principal(tx.session, actor.id)
        self.definitions.authorization.require(current, scope, capability, resource=resource, context=context)
        return current

    async def capabilities(self, tx: Transaction, base: CatalogSnapshot) -> CatalogSnapshot:
        rows = await WorkerRepository(tx).rows(
            "SELECT payload FROM worker_releases WHERE tenant_id=:tenant AND project_id=:project"
        )
        resources = dict(base.resources)
        for row in rows:
            release = WorkerRelease.model_validate_json(json.dumps(row["payload"]))
            snapshot = CatalogSnapshot.from_definitions([], tasks=release.capabilities)
            for key, value in snapshot.resources.items():
                if key in resources and resources[key].digest != value.digest:
                    raise CatalogError(409, "WV-CAPABILITY-CONFLICT", "Immutable task contract conflict")
                resources[key] = value
        return CatalogSnapshot(resources, base.schemas)

    async def register_release(
        self,
        actor: Principal,
        scope: Scope,
        request: ReleaseRequest,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> WorkerRelease:
        self.definitions.require(actor, scope, "release.activate", context)
        self.definitions.registry.validate_release(request)
        measure_value(request.model_dump(mode="json", by_alias=True))
        async with self.definitions.transaction(scope, tx) as tx:
            await self.require(actor, scope, "release.activate", context, tx)
            await lock(tx, f"worker-capabilities:{scope.tenant_id}:{scope.project_id}")
            existing = await self.capabilities(tx, self.definitions.capabilities)
            from firefly_weave.compiler.schemas import validate_schema

            bundle = {name: value.value for name, value in existing.schemas.items()}
            for capability in request.capabilities:
                if validate_schema(capability.input_schema, bundle) or validate_schema(
                    capability.output_schema, bundle
                ):
                    raise CatalogError(422, "WV-CAPABILITY-SCHEMA", "Task capability schema cannot be admitted")
            additions = CatalogSnapshot.from_definitions([], tasks=request.capabilities)
            for key, value in additions.resources.items():
                if key in existing.resources and existing.resources[key].digest != value.digest:
                    raise CatalogError(409, "WV-CAPABILITY-CONFLICT", "Immutable task contract conflict")
            repository = WorkerRepository(tx)
            rows = await repository.rows(
                f"SELECT payload FROM worker_releases WHERE {SCOPE} AND image_digest=:image", image=request.image_digest
            )
            if rows:
                prior = WorkerRelease.model_validate_json(json.dumps(rows[0]["payload"]))
                if prior.model_dump(exclude={"id"}) != request.model_dump():
                    raise CatalogError(409, "WV-RELEASE-CONFLICT", "Immutable image manifest conflict")
                return prior
            release = WorkerRelease(id=uuid4(), **request.model_dump(by_alias=True))
            await repository.execute(
                "INSERT INTO worker_releases VALUES(:id,:tenant,:project,:environment,:image,cast(:payload AS jsonb))",
                id=release.id,
                image=release.image_digest,
                payload=release.model_dump_json(by_alias=True),
            )
            await audit(
                tx.session,
                actor,
                "worker.release",
                str(release.id),
                scope=scope,
                capability="release.activate",
                context=context,
            )
            return release

    async def register_instance(
        self,
        actor: Principal,
        scope: Scope,
        request: InstanceRequest,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> WorkerInstance:
        async with self.definitions.transaction(scope, tx) as tx:
            await self.require(actor, scope, "worker.register", context, tx, str(request.release_id))
            repository = WorkerRepository(tx)
            release = await repository.release(request.release_id)
            available = {f"{c.task_type}@{c.task_version}" for c in release.capabilities}
            if not set(request.task_types) <= available or len(set(request.task_types)) != len(request.task_types):
                raise AccessDenied()
            for capability in request.task_types:
                await self.require(actor, scope, "worker.register", context, tx, capability)
            # The stored instance and the registration answer keep their bytes; identity is a fact.
            instance = WorkerInstance(id=uuid4(), principal_id=actor.id, **request.model_dump(exclude={"identity"}))
            await repository.execute(
                "INSERT INTO worker_instances "
                "(id,tenant_id,project_id,environment_id,release_id,principal_id,payload,revoked) "
                "VALUES(:id,:tenant,:project,:environment,:release,:principal,"
                "cast(:payload AS jsonb),false)",
                id=instance.id,
                release=release.id,
                principal=actor.id,
                payload=instance.model_dump_json(),
            )
            await repository.record_registration(instance.id, request.identity)
            await audit(
                tx.session,
                actor,
                "worker.register",
                str(instance.id),
                scope=scope,
                capability="worker.register",
                context=context,
            )
            return instance

    async def revoke(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext, tx: Transaction | None = None
    ) -> None:
        async with self.definitions.transaction(scope, tx) as tx:
            await self.require(actor, scope, "release.retire", context, tx)
            await WorkerRepository(tx).execute(
                f"UPDATE worker_instances SET revoked=true,control_revision=control_revision+1 "
                f"WHERE {SCOPE} AND id=:id AND NOT revoked",
                id=identifier,
            )
            await audit(
                tx.session,
                actor,
                "worker.revoke",
                str(identifier),
                scope=scope,
                capability="release.retire",
                context=context,
            )

    async def control(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: WorkerControlRequest,
        idempotency_key: str,
        *,
        draining: bool,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> WorkerStatus:
        async with self.definitions.transaction(scope, tx) as enlisted:
            await self.require(actor, scope, "worker.drain", context, enlisted, str(identifier))
            command = "drain" if draining else "resume"
            replay = Idempotency(
                enlisted,
                actor.id,
                f"worker.{command}:{scope.environment_id}:{identifier}",
                idempotency_key,
                request.model_dump(mode="json"),
            )
            if prior := await replay.replay():
                return WorkerStatus.model_validate_json(json.dumps(prior))
            repository = WorkerRepository(enlisted)
            rows = await repository.rows(
                f"SELECT revoked,draining,control_revision FROM worker_instances WHERE {SCOPE} AND id=:id FOR UPDATE",
                id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
            if rows[0]["revoked"]:
                raise CatalogError(409, "WV-WORKER-REVOKED", "A revoked worker cannot be drained or resumed")
            if rows[0]["control_revision"] != request.expected_revision:
                raise CatalogError(409, "WV-WORKER-REVISION", "Worker control state changed; refresh before retrying")
            await repository.execute(
                f"UPDATE worker_instances SET draining=:draining,control_revision=control_revision+1 "
                f"WHERE {SCOPE} AND id=:id",
                id=identifier,
                draining=draining,
            )
            await repository.record_drain(identifier, draining=draining, was_draining=rows[0]["draining"])
            result = await repository.status(identifier)
            await audit(
                enlisted.session,
                actor,
                f"worker.{command}",
                str(identifier),
                scope=scope,
                capability="worker.drain",
                context=context,
            )
            await replay.save(result.model_dump(mode="json"))
            return result

    async def admit(
        self,
        actor: Principal,
        scope: Scope,
        request: ActivationRequest,
        artifact: CompiledArtifact,
        *,
        capability: str,
        context: AuditContext,
        tx: Transaction,
    ) -> list[ConnectorExecutionPin]:
        if capability not in {"release.activate", "run.start"}:
            raise AccessDenied()
        self.definitions.require(actor, scope, capability, context)
        executable: dict[str, Any] = dict(artifact.executable)
        supported = executable_node_kinds()
        if any(n["kind"] not in supported for n in executable["graph"]["nodes"]):
            raise CatalogError(422, "WV-READINESS", "Unsupported executable node")
        required: dict[str, set[str]] = {}
        connector_actions: list[dict[str, Any]] = []
        for dependency in executable["dependencies"]:
            if dependency["kind"] != "Action":
                continue
            implementation = dependency["document"]["spec"]["implementation"]
            if implementation["kind"] == "connector":
                requirement = dependency["document"]["spec"].get("connection")
                if requirement is None or requirement["connector"] != implementation["uses"]:
                    raise CatalogError(422, "WV-READINESS", "Connector Action requires a declared connection")
                connector_actions.append(dependency)
                continue
            if implementation["taskType"].startswith("weave-connector-"):
                raise CatalogError(
                    422, "WV-READINESS", "Reserved connector capability requires connector implementation"
                )
            required.setdefault(implementation["taskType"], set()).add(implementation["taskVersion"])
        if set(required) != set(request.worker_release_ids):
            raise CatalogError(422, "WV-READINESS", "Exact worker release pins required")
        for task_type, release_id in request.worker_release_ids.items():
            release = await WorkerRepository(tx).release(release_id)
            offered_catalog = CatalogSnapshot.from_definitions([], tasks=release.capabilities)
            for version in required[task_type]:
                reference = f"{task_type}@{version}"
                offered = offered_catalog.resolve("TaskCapability", reference)
                pinned = next(
                    (
                        d
                        for d in executable["dependencies"]
                        if d["kind"] == "TaskCapability" and d["reference"] == reference
                    ),
                    None,
                )
                if offered is None or pinned is None or offered.digest != pinned["digest"]:
                    raise CatalogError(422, "WV-READINESS", "Worker release does not match pinned Action capability")

        pins: list[ConnectorExecutionPin] = []
        used: set[UUID] = set()
        for action in connector_actions:
            implementation = action["document"]["spec"]["implementation"]
            dependency = next(
                d
                for d in executable["dependencies"]
                if d["kind"] == "Connector" and d["reference"] == implementation["uses"]
            )
            adapter = dependency["document"]["spec"]["adapter"]
            descriptor = self.definitions.registry.descriptor(adapter)
            if descriptor.manifest.digest != dependency["digest"]:
                raise CatalogError(422, "WV-READINESS", "Installed connector contract mismatch")
            matches = []
            for version_id, release_id in request.connector_release_ids.items():
                row = await DefinitionRepository(tx).version(version_id)
                if row["definition_digest"] == dependency["digest"]:
                    matches.append((version_id, release_id))
            if len(matches) != 1:
                raise CatalogError(422, "WV-READINESS", "Exact connector release pins required")
            version_id, release_id = matches[0]
            used.add(version_id)
            release = await WorkerRepository(tx).release(release_id)
            self.definitions.registry.validate_release(release)
            binding = next(
                (
                    b
                    for b in release.connector_bindings
                    if b.connector_digest == dependency["digest"] and b.action == implementation["action"]
                ),
                None,
            )
            if binding is None:
                raise CatalogError(422, "WV-READINESS", "Connector release action unavailable")
            offered = CatalogSnapshot.from_definitions([], tasks=release.capabilities).resolve(
                "TaskCapability", binding.task_reference
            )
            if offered is None:
                raise CatalogError(422, "WV-READINESS", "Connector capability unavailable")
            task_type, task_version = binding.task_reference.split("@")
            pins.append(
                ConnectorExecutionPin(
                    **binding.model_dump(),
                    action_digest=action["digest"],
                    connector_version_id=version_id,
                    release_id=release_id,
                    capability_digest=offered.digest,
                    task_type=task_type,
                    task_version=task_version,
                )
            )
        if used != set(request.connector_release_ids):
            raise CatalogError(422, "WV-READINESS", "Extraneous connector release pins")
        return pins

    async def grant_connection(
        self,
        actor: Principal,
        scope: Scope,
        request: CredentialGrantRequest,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> None:
        async with self.definitions.transaction(scope, tx) as tx:
            await self.require(actor, scope, "connection.manage", context, tx)
            release = await WorkerRepository(tx).release(request.release_id)
            if request.capability not in release.credential_capabilities:
                raise AccessDenied()
            await self.connections.read(actor, scope, request.connection_revision_id, context=context, tx=tx)
            await WorkerRepository(tx).execute(
                "INSERT INTO worker_connection_grants "
                "VALUES(:tenant,:project,:environment,:release,:connection,:capability,"
                "false) ON CONFLICT(release_id,connection_id,capability) DO UPDATE SET "
                "revoked=false",
                release=release.id,
                connection=request.connection_revision_id,
                capability=request.capability,
            )
            await audit(
                tx.session,
                actor,
                "worker.connection.grant",
                str(release.id),
                scope=scope,
                capability="connection.manage",
                context=context,
            )

    async def retire_stale(
        self,
        actor: Principal,
        scope: Scope,
        request: RetireStaleRequest,
        idempotency_key: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> RetireStaleResult:
        """Revoke at most one batch of workers without an active lease whose last contact is older than the threshold.

        A dry run selects the same oldest-first batch without revoking it. Both save their answer for replay under
        the same Idempotency-Key; a worker that was never seen counts from its registration, and one whose
        registration time is unknown is never retired.
        """
        self.precheck(actor, scope, "release.retire", context)
        async with self.definitions.transaction(scope, tx) as enlisted:
            await self.current(actor, scope, "release.retire", context, enlisted)
            replay = Idempotency(
                enlisted,
                actor.id,
                f"workers.retire_stale:{scope.environment_id}",
                idempotency_key,
                request.model_dump(mode="json"),
            )
            if prior := await replay.replay():
                return RetireStaleResult.model_validate_json(json.dumps(prior))
            repository = WorkerRepository(enlisted)
            now = await repository.now()
            cutoff = now - timedelta(seconds=request.older_than_seconds)
            matched = await repository.retirable(cutoff, now)
            if request.dry_run:
                result = RetireStaleResult(
                    matched=matched,
                    revoked=0,
                    worker_ids=await repository.retirement_batch(cutoff, now, lock=False),
                )
            else:
                locked = await repository.retirement_batch(cutoff, now, lock=True)
                revoked = await repository.retire(locked, cutoff, now)
                result = RetireStaleResult(matched=matched, revoked=len(revoked), worker_ids=revoked)
            await audit(
                enlisted.session,
                actor,
                "worker.retire_stale",
                str(scope.environment_id),
                scope=scope,
                capability="release.retire",
                context=context,
                details={
                    "dry_run": request.dry_run,
                    "older_than_seconds": request.older_than_seconds,
                    **result.model_dump(mode="json"),
                },
            )
            await replay.save(result.model_dump(mode="json"))
            return result

    async def release_usage(
        self,
        actor: Principal,
        scope: Scope,
        release_id: UUID,
        query: ReleaseUsageQuery,
        *,
        context: AuditContext,
    ) -> ReleaseUsage:
        """Worker counts, pinning activations and a page of the readable deployments that run an admitted release.

        The cursor is bound to the release and to the targets this reader may see, which only the read unit knows.
        """
        self.precheck(actor, scope, "catalog.read", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            current = await self.current(actor, scope, "catalog.read", context, tx)
            repository = WorkerRepository(tx)
            digest = await repository.release_digest(release_id)
            targets = target_permissions(
                self.definitions.authorization, actor, current, scope, "deployment.read", context
            )
            collection = query.cursor_collection(release_id, targets)
            position = release_usage_position(decode_cursor_v2(query.cursor, scope, collection))
            now = await repository.now()
            workers = ReleaseWorkerCounts(**await repository.release_workers(release_id, now))
            activations = await repository.release_activations(release_id)
            rows = (
                []
                if targets == ()
                else await repository.release_deployments(
                    release_id, digest, authorized_targets=targets, position=position, limit=query.limit
                )
            )
            visible = rows[: query.limit]
            cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                cursor = encode_cursor_v2(scope, collection, last["component"], str(last["deployment_id"]))
            return ReleaseUsage(
                workers=workers,
                deployments=[
                    ReleaseDeploymentUsage.model_validate_json(json.dumps(row, default=str)) for row in visible
                ],
                activations=activations,
                next_cursor=cursor,
            )

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        *,
        releases: bool = False,
        limit: int = 50,
        cursor: UUID | None = None,
        query: WorkerListQuery | None = None,
        position: RunPosition | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        """Releases in ID order, or one page of workers from a single fleet query.

        Workers take a typed query and the decoded position of its cursor; the keyword `limit` selects the first page
        of current workers.
        """
        if not releases:
            if query is None:
                if cursor is not None:
                    raise ValueError("Continue a worker list with its query and position")
                query = WorkerListQuery(limit=limit)
            elif cursor is not None or limit != 50:
                raise ValueError("Use either a query or legacy list arguments")
            return await self._fleet(actor, scope, query, position, context=context)
        if query is not None or position is not None:
            raise ValueError("Release lists take no worker query")
        from firefly_weave.persistence.paging import page_ids

        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            await self.require(actor, scope, "catalog.read", context, tx)
            ids = await page_ids(tx, "worker_releases", limit, cursor)
            items = [
                (await self.read(actor, scope, identifier, releases=True, context=context, tx=tx)).model_dump(
                    mode="json", by_alias=True
                )
                for identifier in ids[:limit]
            ]
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}

    async def _fleet(
        self,
        actor: Principal,
        scope: Scope,
        query: WorkerListQuery,
        position: RunPosition | None,
        *,
        context: AuditContext,
    ) -> dict[str, Any]:
        authorization = self.definitions.authorization
        target = None if query.target_id is None else str(query.target_id)
        self.precheck(actor, scope, "status.read", context)
        if target is not None:
            authorization.require(actor, scope, "deployment.read", resource=target, context=context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            current = await self.current(actor, scope, "status.read", context, tx)
            if target is not None:
                authorization.require(current, scope, "deployment.read", resource=target, context=context)
            targets = target_permissions(authorization, actor, current, scope, "deployment.read", context)
            repository = WorkerRepository(tx)
            observed_at = await repository.now()
            rows = await repository.fleet(query, position, observed_at=observed_at, authorized_targets=targets)
            visible = rows[: query.limit]
            next_cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                at = last["last_seen_at"] if query.order == "last_seen_desc" else last["registered_at"]
                next_cursor = encode_cursor_v2(
                    scope, query.cursor_collection(), at.isoformat() if at else None, str(last["id"])
                )
            return {
                "items": [fleet_status(row, observed_at).model_dump(mode="json") for row in visible],
                "next_cursor": next_cursor,
            }

    async def read(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        releases: bool = False,
        context: AuditContext,
        tx: Transaction | None = None,
        observed_at: datetime | None = None,
    ) -> WorkerStatus | WorkerRelease:
        if not releases:
            return await self._worker(actor, scope, identifier, context=context, tx=tx, observed_at=observed_at)
        async with self.definitions.transaction(scope, tx, mutation=False) as enlisted:
            await self.require(actor, scope, "catalog.read", context, enlisted)
            rows = await WorkerRepository(enlisted).rows(
                f"SELECT payload FROM worker_releases WHERE {SCOPE} AND id=:id", id=identifier
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
            return WorkerRelease.model_validate_json(json.dumps(rows[0]["payload"]))

    async def _worker(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        context: AuditContext,
        tx: Transaction | None,
        observed_at: datetime | None,
    ) -> WorkerStatus:
        """One worker through the fleet statement, so a read shows what a list shows, placement included."""
        self.precheck(actor, scope, "status.read", context, str(identifier))
        async with self.definitions.transaction(scope, tx, mutation=False) as enlisted:
            current = await self.current(actor, scope, "status.read", context, enlisted, str(identifier))
            targets = target_permissions(
                self.definitions.authorization, actor, current, scope, "deployment.read", context
            )
            repository = WorkerRepository(enlisted)
            now = observed_at or await repository.now()
            rows = await repository.fleet(
                WorkerListQuery(state="all", limit=1),
                None,
                observed_at=now,
                authorized_targets=targets,
                worker_id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
            return fleet_status(rows[0], now)
