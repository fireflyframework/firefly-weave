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

"""Fenced leases, accepted receipts and atomic runtime advancement."""

import asyncio
import json
import secrets
from functools import partial
from typing import Any, cast
from uuid import UUID

from pyfly.container.stereotypes import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import ScopedSecrets
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectorInvocation
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import (
    CompletionAcknowledgment,
    CompletionReceipt,
    ConnectorExecutionPin,
    CredentialLease,
    CredentialRequest,
    LeaseProof,
    TaskConnectionContext,
    TaskError,
    TaskExecutionContext,
    TaskLease,
    UnavailableCompletionReceipt,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import Omission
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.workers.models import VerifiedTask, capped_extension, new_lease_token, token_hash, unavailable
from firefly_weave.workers.repository import SCOPE, WorkerRepository
from firefly_weave.workers.service import WorkerService


class _TaskOperation:
    def __init__(
        self,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
        workers: WorkerService,
        runtime: RuntimeService,
        connections: ConnectionService,
        secrets: ScopedSecrets,
        *,
        ttl_seconds: int = 15,
    ) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("Lease TTL must be 1..60 seconds")
        self.actor, self.scope, self.context = actor, scope, context
        self.workers, self.runtime, self.ttl = workers, runtime, ttl_seconds
        self.connections, self.secrets = connections, secrets

    async def authority(
        self, tx: Transaction, owner: UUID, operation: str, capability: str | None = None, *, lock: bool = False
    ) -> Any:
        repository = WorkerRepository(tx)
        instance = await repository.instance(owner, lock=lock)
        if instance.principal_id != self.actor.id:
            raise AccessDenied()
        await self.workers.require(self.actor, self.scope, operation, self.context, tx, str(instance.release_id))
        if capability:
            if capability not in instance.task_types:
                raise AccessDenied()
            await self.workers.require(self.actor, self.scope, operation, self.context, tx, capability)
        return instance

    async def claim(self, tx: Transaction, worker_id: UUID, limit: int) -> list[TaskLease]:
        self.connections.registry.require_operational()
        if not 1 <= limit <= 100:
            raise ValueError("Claim limit must be 1..100")
        async with self.workers.definitions.transaction(self.scope, tx) as enlisted:
            instance = await self.authority(enlisted, worker_id, "task.claim")
            repository = WorkerRepository(enlisted)
            claimed: list[TaskLease] = []
            if await repository.draining(worker_id):
                await repository.observe(worker_id)
                return claimed
            for identifier in await self.runtime.task_candidates(enlisted, instance.release_id, instance.task_types):
                if len(claimed) >= limit:
                    break
                locked = await self.runtime.lock_task(enlisted, identifier, skip_locked=True)
                if locked is None:
                    continue
                run, task = locked
                if (
                    task["status"] != "ready"
                    or run["state"]["status"] != "waiting"
                    or run["state"].get("manual_paused")
                ):
                    continue
                payload = task["payload"]
                capability = f"{payload['task_type']}@{payload['task_version']}"
                if capability not in instance.task_types:
                    continue
                instance = await self.authority(enlisted, worker_id, "task.claim", capability, lock=True)
                if await repository.draining(worker_id):
                    break
                if await self.runtime.observe_unavailable(enlisted, run):
                    continue
                await RuntimeRepository(enlisted).require_work(run["id"], run["state"])
                now = await repository.now()
                if task["next_attempt_at"] is not None and task["next_attempt_at"] > now:
                    continue
                from datetime import datetime

                deadline = await self.runtime.task_deadline(
                    enlisted, run["id"], datetime.fromisoformat(payload["deadline"])
                )
                if deadline <= now:
                    continue
                active = await repository.rows(
                    f"SELECT count(*) AS count FROM task_leases WHERE {SCOPE} AND "
                    f"owner=:owner AND status='active' AND expires_at>clock_timestamp()",
                    owner=worker_id,
                )
                if active[0]["count"] >= instance.capacity:
                    break
                generation = (
                    await repository.rows(
                        f"SELECT coalesce(max(generation),0)+1 AS value FROM task_leases "
                        f"WHERE {SCOPE} AND task_id=:task",
                        task=identifier,
                    )
                )[0]["value"]
                # Only the retry policy decides whether a failed/expired attempt may be requeued.
                action = next(
                    d["document"]["spec"]
                    for d in run["artifact"]["executable"]["dependencies"]
                    if d["kind"] == "Action" and d["digest"] == payload["action_digest"]
                )
                measure_value(payload["input"])
                token = new_lease_token()
                expires = capped_extension(now, self.ttl, deadline)
                await repository.execute(
                    "INSERT INTO task_leases "
                    "VALUES(:tenant,:project,:environment,:task,:generation,:owner,:token,"
                    ":now,:expires,:deadline,:capability,cast(:policy AS jsonb),'active')",
                    task=identifier,
                    generation=generation,
                    owner=worker_id,
                    token=token_hash(token),
                    now=now,
                    expires=expires,
                    deadline=deadline,
                    capability=capability,
                    policy=json.dumps({"side_effect": action["sideEffect"], "retry": action["retry"]}),
                )
                await self.runtime.set_task_status(enlisted, identifier, "leased")
                lease = TaskLease(
                    proof=LeaseProof(task_id=identifier, generation=generation, owner=worker_id, token=token),
                    input=payload["input"],
                    operation_key=task["operation_key"],
                    deadline=deadline,
                    expires_at=expires,
                    capability=capability,
                    worker_release_id=instance.release_id,
                )
                claimed.append(lease)
                if enlisted.telemetry is not None:
                    telemetry = enlisted.telemetry
                    enqueued = task.get("enqueued_at")
                    age = (
                        max(0.0, (now - enqueued).total_seconds()) if generation == 1 and enqueued is not None else None
                    )
                    if not enlisted.on_commit(partial(telemetry.queue_age, "tasks.claim", age)):
                        telemetry.record("dropped", status="rejected")
                await audit(
                    enlisted.session,
                    self.actor,
                    "task.claim",
                    str(identifier),
                    scope=self.scope,
                    capability="task.claim",
                    context=self.context,
                )
            # Keep run/task -> instance lock order, including when an idle poll
            # records presence. A drain only owns the instance row.
            await repository.observe(worker_id)
            return claimed

    async def check(self, tx: Transaction, proof: LeaseProof, operation: str, *, live: bool = True) -> VerifiedTask:
        locked = await self.runtime.lock_task(tx, proof.task_id)
        if locked is None:
            raise unavailable()
        run, task = locked
        repository = WorkerRepository(tx)
        attempt = await repository.lease(proof.task_id, proof.generation)
        instance = await self.authority(tx, proof.owner, operation, attempt["capability"], lock=True)
        if (
            attempt["owner"] != proof.owner
            or not secrets.compare_digest(attempt["token_hash"], token_hash(proof.token))
            or instance.release_id != task["worker_release_id"]
        ):
            raise unavailable()
        latest = (
            await repository.rows(
                f"SELECT max(generation) AS value FROM task_leases WHERE {SCOPE} AND task_id=:task", task=proof.task_id
            )
        )[0]["value"]
        checked_at = await repository.now() if live else None
        if live and (
            proof.generation != latest
            or attempt["status"] != "active"
            or task["status"] != "leased"
            or run["state"]["status"] not in ({"waiting", "suspended"} if operation == "task.complete" else {"waiting"})
            or (operation != "task.complete" and run["state"].get("manual_paused"))
            or attempt["expires_at"] <= checked_at
            or attempt["deadline"] <= checked_at
        ):
            raise unavailable()
        from firefly_weave.runtime.admission import require_available

        if live:
            await RuntimeRepository(tx).require_work(run["id"], run["state"])
            require_available(run)
        return VerifiedTask(tx, run, task, attempt, checked_at)

    async def heartbeat(self, tx: Transaction, lease: LeaseProof) -> TaskLease:
        async with self.workers.definitions.transaction(self.scope, tx) as tx:
            self.connections.registry.require_operational()
            verified = await self.check(tx, lease, "task.heartbeat")
            repository = WorkerRepository(tx)
            expires = capped_extension(await repository.now(), self.ttl, verified.attempt["deadline"])
            await repository.execute(
                f"UPDATE task_leases SET expires_at=:expires WHERE {SCOPE} AND "
                f"task_id=:task AND generation=:generation",
                task=lease.task_id,
                generation=lease.generation,
                expires=expires,
            )
            await repository.observe(lease.owner)
            return TaskLease(
                proof=lease,
                input=verified.task["payload"]["input"],
                operation_key=verified.task["operation_key"],
                deadline=verified.attempt["deadline"],
                expires_at=expires,
                capability=verified.attempt["capability"],
                worker_release_id=verified.task["worker_release_id"],
            )

    async def complete(
        self, tx: Transaction, lease: LeaseProof, completion_id: UUID, output: JsonValue
    ) -> CompletionAcknowledgment:
        return await self._finish(tx, lease, completion_id, output, failed=False)

    async def fail(self, tx: Transaction, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment:
        return await self._finish(tx, lease, error.completion_id, error.model_dump(mode="json"), failed=True)

    async def _finish(
        self, tx: Transaction, lease: LeaseProof, completion_id: UUID, output: JsonValue, *, failed: bool
    ) -> CompletionAcknowledgment:
        async with self.workers.definitions.transaction(self.scope, tx) as tx:
            verified = await self.check(tx, lease, "task.complete", live=False)
            repository = WorkerRepository(tx)
            prior = await repository.receipt(lease.task_id, completion_id)
            from firefly_weave.runtime.admission import require_available
            from firefly_weave.runtime.admission import unavailable as policy_unavailable

            if prior and prior.generation == lease.generation:
                if prior.status == "rejected":
                    return prior
                if policy_unavailable(verified.run):
                    return UnavailableCompletionReceipt(
                        task_id=prior.task_id,
                        generation=prior.generation,
                        completion_id=prior.completion_id,
                        accepted_at=prior.accepted_at,
                        status=prior.status,
                        unavailable=True,
                        payload_match="unavailable",
                        omissions=[Omission(path="/accepted_output_hash", reason="uncertain_derived")],
                    )
            require_available(verified.run)
            measure_value(output)
            rejection = None if failed else self.runtime.classify_task_output(verified, output)
            fingerprint = (
                None
                if rejection
                else canonical_digest({"status": "failed" if failed else "completed", "output": output})
            )
            if prior:
                if prior.generation != lease.generation or prior.accepted_output_hash != fingerprint:
                    raise CatalogError(409, "WV-COMPLETION-CONFLICT", "Completion ID payload conflict")
                return prior
            ignored = (
                verified.attempt["status"] == "control_revoked"
                and verified.task["status"] == "cancelled"
                and verified.run["state"]["status"] in {"failed", "cancelled"}
            )
            if ignored:
                generations = await repository.rows(
                    f"SELECT max(generation) AS value FROM task_leases WHERE {SCOPE} AND task_id=:task",
                    task=lease.task_id,
                )
                if generations[0]["value"] != lease.generation:
                    raise unavailable()
            else:
                self.connections.registry.require_operational()
                verified = await self.check(tx, lease, "task.complete")
                await self.runtime.finish_verified_task(
                    tx, verified, completion_id, None if rejection else output, failed=failed, rejection_code=rejection
                )
            receipt = CompletionReceipt(
                task_id=lease.task_id,
                generation=lease.generation,
                completion_id=completion_id,
                accepted_output_hash=fingerprint,
                reason_code=cast(Any, rejection),
                accepted_at=await repository.now(),
                status="rejected" if rejection else ("ignored" if ignored else ("failed" if failed else "completed")),
            )
            await repository.execute(
                "INSERT INTO completion_receipts "
                "VALUES(:tenant,:project,:environment,:task,:generation,:completion,"
                "cast(:payload AS jsonb))",
                task=lease.task_id,
                generation=lease.generation,
                completion=completion_id,
                payload=receipt.model_dump_json(),
            )
            await repository.execute(
                f"UPDATE task_leases SET status=:status WHERE {SCOPE} AND task_id=:task AND generation=:generation",
                task=lease.task_id,
                generation=lease.generation,
                status="control_revoked_receipted" if ignored else receipt.status,
            )
            await audit(
                tx.session,
                self.actor,
                "task." + receipt.status,
                str(lease.task_id),
                scope=self.scope,
                capability="task.complete",
                context=self.context,
                details={"completion_id": str(completion_id), "generation": lease.generation},
            )
            return receipt

    async def execution_context(self, tx: Transaction, proof: LeaseProof) -> TaskExecutionContext:
        verified = await self.check(tx, proof, "task.claim")
        connection = None
        # Only the saved activation chooses the revision. A worker cannot use
        # this endpoint to enumerate other connections or retrieve secret handles.
        if verified.task["payload"].get("connection_slot"):
            revision = await self.connections.lease_revision(verified)
            connection = TaskConnectionContext(
                revision_id=revision.id,
                connector=revision.connector,
                config=revision.config,
                allowed_destinations=revision.allowed_destinations,
                secret_slots=sorted(revision.secret_refs),
            )
        return TaskExecutionContext(connection=connection, expires_at=verified.attempt["expires_at"])

    async def invocation(self, tx: Transaction, proof: LeaseProof) -> tuple[str, ConnectorInvocation]:
        verified = await self.check(tx, proof, "task.claim")
        target = verified.task["payload"].get("connector_target")
        if target is None:
            raise unavailable()
        pin = ConnectorExecutionPin.model_validate_json(json.dumps(target))
        if (
            pin.action_digest != verified.task["payload"]["action_digest"]
            or pin.task_type != verified.task["payload"]["task_type"]
            or pin.task_version != verified.task["payload"]["task_version"]
            or pin.release_id != verified.task["worker_release_id"]
            or pin.task_reference != verified.attempt["capability"]
            or pin.model_dump(mode="json") not in verified.run["activation"].get("connector_execution_pins", [])
        ):
            raise unavailable()
        release = await WorkerRepository(tx).release(pin.release_id)
        self.connections.registry.validate_release(release)
        descriptor = self.connections.registry.descriptor(pin.adapter)
        if (
            descriptor.implementation_version != pin.implementation_version
            or descriptor.manifest.digest != pin.connector_digest
        ):
            raise unavailable()
        dependencies = verified.run["artifact"]["executable"]["dependencies"]
        action = next(
            d["document"]["spec"] for d in dependencies if d["kind"] == "Action" and d["digest"] == pin.action_digest
        )
        implementation = action["implementation"]
        if implementation["kind"] != "connector" or implementation["action"] != pin.action:
            raise unavailable()
        revision = await self.connections.lease_revision(verified)
        if revision.connector_digest != pin.connector_digest:
            raise unavailable()
        manifest = cast(dict[str, Any], descriptor.manifest.value["spec"])
        return pin.adapter, ConnectorInvocation(
            connection=revision,
            config=implementation.get("config", {}),
            action=pin.action,
            input_schema=action["inputSchema"],
            output_schema=action["outputSchema"],
            max_request_bytes=manifest["limits"]["maxRequestBytes"],
            max_response_bytes=manifest["limits"]["maxResponseBytes"],
            target=pin,
            schema_bundle={d["reference"]: d["document"] for d in dependencies if d["kind"] == "Schema"},
        )

    async def credential_authority(
        self, request: CredentialRequest, *, resolved: bool = False, provider_version: str | None = None
    ) -> tuple[str, Any]:
        async with self.workers.definitions.transaction(self.scope, None) as tx:
            self.connections.registry.require_operational()
            verified = await self.check(tx, request.lease, "credential.lease")
            repository = WorkerRepository(tx)
            release = await repository.release(verified.task["worker_release_id"])
            capability = verified.attempt["capability"]
            if capability not in release.credential_capabilities:
                raise AccessDenied()
            connection_slot = verified.task["payload"]["connection_slot"]
            bound = verified.run["activation"]["request"]["connection_revision_ids"].get(connection_slot)
            grants = await repository.rows(
                f"SELECT 1 FROM worker_connection_grants WHERE {SCOPE} AND "
                f"release_id=:release AND connection_id=:connection AND "
                f"capability=:capability AND NOT revoked",
                release=release.id,
                connection=request.connection_revision_id,
                capability=capability,
            )
            if bound != str(request.connection_revision_id) or not grants:
                raise AccessDenied()
            handle = await self.connections.lease_handle(
                self.actor,
                self.scope,
                request.connection_revision_id,
                request.slot,
                resource=capability,
                context=self.context,
                tx=tx,
            )
            if resolved:
                await audit(
                    tx.session,
                    self.actor,
                    "credential.resolve",
                    str(request.lease.task_id),
                    scope=self.scope,
                    capability="credential.lease",
                    context=self.context.model_copy(update={"run_id": verified.run["id"]}),
                    details={
                        "generation": request.lease.generation,
                        "connection_revision_id": str(request.connection_revision_id),
                        "slot": request.slot,
                        "provider_version": provider_version,
                    },
                )
            return handle, verified.attempt["expires_at"]

    async def credentials(self, request: CredentialRequest) -> CredentialLease:
        handle, expires = await self.credential_authority(request)
        # Synchronous provider ports run on a thread, never in a database transaction.
        async with asyncio.timeout(5):
            secret = await resolve_secret(partial(self.secrets.resolve, self.scope, handle))
        current_handle, current_expiry = await self.credential_authority(
            request, resolved=True, provider_version=secret.provider_version
        )
        if current_handle != handle:
            raise AccessDenied()
        return CredentialLease(
            value=secret.value, provider_version=secret.provider_version, expires_at=min(expires, current_expiry)
        )


@service
class TaskService:
    """Stateless application service; request authority is always explicit."""

    def __init__(
        self, workers: WorkerService, runtime: RuntimeService, connections: ConnectionService, secrets: ScopedSecrets
    ) -> None:
        self.workers, self.runtime = workers, runtime
        self.connections, self.secrets = connections, secrets

    def _operation(self, actor: Principal, scope: Scope, context: AuditContext) -> _TaskOperation:
        return _TaskOperation(actor, scope, context, self.workers, self.runtime, self.connections, self.secrets)

    async def claim(
        self, tx: Transaction, worker_id: UUID, limit: int, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> list[TaskLease]:
        return await self._operation(actor, scope, context).claim(tx, worker_id, limit)

    async def heartbeat(
        self, tx: Transaction, lease: LeaseProof, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> TaskLease:
        return await self._operation(actor, scope, context).heartbeat(tx, lease)

    async def complete(
        self,
        tx: Transaction,
        lease: LeaseProof,
        completion_id: UUID,
        output: JsonValue,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> CompletionAcknowledgment:
        return await self._operation(actor, scope, context).complete(tx, lease, completion_id, output)

    async def fail(
        self,
        tx: Transaction,
        lease: LeaseProof,
        error: TaskError,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> CompletionAcknowledgment:
        return await self._operation(actor, scope, context).fail(tx, lease, error)

    async def credentials(
        self, request: CredentialRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> CredentialLease:
        return await self._operation(actor, scope, context).credentials(request)

    async def context(
        self, tx: Transaction, lease: LeaseProof, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> TaskExecutionContext:
        return await self._operation(actor, scope, context).execution_context(tx, lease)

    async def verify_file_task(
        self, tx: Transaction, lease: LeaseProof, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> VerifiedTask:
        """In-process authority for file access; never return this proof through the API."""
        return await self._operation(actor, scope, context).check(tx, lease, "task.claim")

    async def credential_authority(
        self, request: CredentialRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> None:
        await self._operation(actor, scope, context).credential_authority(request)

    async def invocation(
        self, tx: Transaction, proof: LeaseProof, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> tuple[str, ConnectorInvocation]:
        async with self.workers.definitions.transaction(scope, tx) as enlisted:
            return await self._operation(actor, scope, context).invocation(enlisted, proof)

    async def recovery_candidates(self, tx: Transaction, limit: int) -> list[UUID]:
        """Trusted polling port; does not claim or reexecute attempts."""
        if not 1 <= limit <= 100:
            raise ValueError("Bounded recovery page required")
        from firefly_weave.runtime.admission import runnable, runnable_parameters

        rows = await WorkerRepository(tx).rows(
            f"SELECT task_id FROM task_leases WHERE {SCOPE} AND "
            "((status='active' AND expires_at<=clock_timestamp()) OR status='failed') "
            "AND task_id NOT IN (SELECT t.id FROM task_intents t JOIN runtime_capacity_blocks b ON "
            "b.run_id=t.run_id WHERE b.active) "
            f"AND (SELECT {runnable('r')} FROM runs r WHERE r.id="
            "(SELECT t.run_id FROM task_intents t WHERE t.id=task_leases.task_id)) "
            "ORDER BY expires_at,task_id LIMIT :limit",
            limit=limit,
            **runnable_parameters(),
        )
        return [row["task_id"] for row in rows]

    async def recovery_attempt(self, tx: Transaction, task_id: UUID) -> dict[str, Any] | None:
        """Called after runtime run/task locks; worker module owns attempt/outcome SQL."""
        repository = WorkerRepository(tx)
        rows = await repository.rows(
            f"SELECT * FROM task_leases WHERE {SCOPE} AND task_id=:task ORDER BY generation DESC LIMIT 1 FOR UPDATE",
            task=task_id,
        )
        if not rows:
            return None
        attempt = rows[0]
        if not (
            attempt["status"] == "failed"
            or (attempt["status"] == "active" and attempt["expires_at"] <= await repository.now())
        ):
            return None
        return attempt

    async def retire_attempt(self, tx: Transaction, task_id: UUID, generation: int) -> None:
        await WorkerRepository(tx).execute(
            f"UPDATE task_leases SET status='recovered' WHERE {SCOPE} AND task_id=:task AND generation=:generation "
            "AND status IN ('active','failed')",
            task=task_id,
            generation=generation,
        )
