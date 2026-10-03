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

"""Run-then-task locking, current eligibility and atomic decision continuation."""

import builtins
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.human_tasks import (
    AssignmentBinding,
    AssignmentBindingRequest,
    AssignmentPin,
    CompleteHumanTask,
    HumanTask,
    HumanTaskCommand,
    ReassignHumanTask,
    TaskGroup,
    TaskGroupRequest,
)
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import transition_async, validate, workflow
from firefly_weave.runtime.models import HumanTaskIntent, RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService, view_of
from firefly_weave.runtime.waits import TERMINAL, settle


def task_view(row: dict[str, Any]) -> HumanTask:
    return HumanTask.model_validate_json(
        json.dumps({key: row[key] for key in HumanTask.model_fields if key in row}, default=str)
    )


async def human(tx: Transaction, identifier: UUID) -> Principal:
    principal = await load_principal(tx.session, identifier)
    if not principal.active or principal.kind != "human":
        raise AccessDenied()
    return principal


async def eligible(repository: RuntimeRepository, pin: AssignmentPin, identifier: UUID) -> bool:
    if identifier in pin.principal_ids:
        return True
    if not pin.group_ids:
        return False
    return bool(
        await repository.rows(
            f"SELECT 1 FROM human_task_memberships WHERE {SCOPE} AND principal_id=:actor AND "
            "group_id=ANY(:groups) LIMIT 1",
            actor=identifier,
            groups=pin.group_ids,
        )
    )


async def pin_assignments(tx: Transaction, bindings: dict[str, UUID], names: set[str]) -> dict[str, AssignmentPin]:
    if set(bindings) != names:
        raise CatalogError(422, "WV-ASSIGNMENT", "Activation assignment slots must match human nodes")
    repository = RuntimeRepository(tx)
    pins: dict[str, AssignmentPin] = {}
    for name, identifier in bindings.items():
        rows = await repository.rows(
            f"SELECT * FROM human_assignment_bindings WHERE {SCOPE} AND id=:id FOR SHARE", id=identifier
        )
        if not rows or not rows[0]["enabled"] or rows[0]["name"] != name:
            raise CatalogError(422, "WV-ASSIGNMENT", "Missing or disabled scoped assignment binding")
        pins[name] = AssignmentPin.model_validate_json(json.dumps(rows[0]["payload"]))
    return pins


async def create_task(repository: RuntimeRepository, view: Any, command: HumanTaskIntent, now: Any) -> None:
    pin = view.activation.assignment_pins.get(command.assignment)
    if pin is None:
        raise CatalogError(422, "WV-ASSIGNMENT", "Human task lacks an activation assignment pin")
    identifier = uuid4()
    await repository.execute(
        "INSERT INTO human_tasks(tenant_id,project_id,environment_id,id,run_id,node_id,revision,status,"
        "assignment,title,context,form_schema,decisions,created_at,due_at,expires_at) "
        "VALUES(:tenant,:project,:environment,:id,:run,:node,1,'ready',cast(:assignment AS jsonb),"
        ":title,cast(:context AS jsonb),cast(:form AS jsonb),cast(:decisions AS jsonb),:now,:due,:expiry)",
        id=identifier,
        run=view.id,
        node=command.node_id,
        assignment=pin.model_dump_json(),
        title=command.title,
        context=json.dumps(command.context),
        form=json.dumps(command.form_schema),
        decisions=json.dumps(command.decisions),
        now=now,
        due=command.due_at,
        expiry=command.expires_at,
    )

    await repository.execute(
        "INSERT INTO human_task_audit VALUES(:tenant,:project,:environment,:task,1,'created',NULL,:now,NULL)",
        task=identifier,
        now=now,
    )


@service
class HumanTaskService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    def require(
        self, actor: Principal, scope: Scope, capability: str, context: AuditContext, *, resource: UUID | None = None
    ) -> None:
        self.runtime.definitions.authorization.require(
            actor, scope, capability, context=context, resource=str(resource) if resource is not None else None
        )

    def _resources(
        self, actor: Principal, scope: Scope, capability: str, context: AuditContext
    ) -> tuple[bool, list[str]]:
        grants = [g for g in actor.grants if contains(g.scope, scope) and capability in ROLE_CAPABILITIES[g.role]]
        if not grants:
            return False, []
        unrestricted = any(not g.resources for g in grants)
        resources = sorted({r for g in grants for r in g.resources})
        self.runtime.definitions.authorization.require(
            actor, scope, capability, context=context, resource=None if unrestricted else resources[0]
        )
        return unrestricted, resources

    async def _readable(
        self, repository: RuntimeRepository, row: dict[str, Any], actor: Principal, scope: Scope, context: AuditContext
    ) -> None:
        try:
            self.require(actor, scope, "human_task.manage", context, resource=row["id"])
            return
        except AccessDenied:
            self.require(actor, scope, "human_task.read", context, resource=row["id"])
        pin = AssignmentPin.model_validate_json(json.dumps(row["assignment"]))
        if not await eligible(repository, pin, actor.id):
            raise AccessDenied()

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> HumanTask:
        self.require(actor, scope, "human_task.read", context, resource=identifier)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            repository = RuntimeRepository(tx)
            actor = await load_principal(tx.session, actor.id)
            rows = await repository.rows(f"SELECT * FROM human_tasks WHERE {SCOPE} AND id=:id", id=identifier)
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Human task not found")
            await self._readable(repository, rows[0], actor, scope, context)
            history = await repository.rows(
                f"SELECT revision,action,actor_id,created_at,reason FROM human_task_audit "
                f"WHERE {SCOPE} AND task_id=:id ORDER BY revision DESC LIMIT 100",
                id=identifier,
            )
            rows[0]["history"] = list(reversed(history))
            rows[0]["history_truncated"] = rows[0]["revision"] > len(history)
            return task_view(rows[0])

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: UUID | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        read_all, read_ids = self._resources(actor, scope, "human_task.read", context)
        if not read_all and not read_ids:
            raise AccessDenied()
        if not 1 <= limit <= 100 or status not in {None, "ready", "claimed", "completed", "expired", "cancelled"}:
            raise ValueError("Bounded task page and valid status required")
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await load_principal(tx.session, actor.id)
            read_all, read_ids = self._resources(actor, scope, "human_task.read", context)
            if not read_all and not read_ids:
                raise AccessDenied()
            repository = RuntimeRepository(tx)
            manage_all, manage_ids = self._resources(actor, scope, "human_task.manage", context)
            rows = await repository.rows(
                f"SELECT t.* FROM human_tasks t WHERE {SCOPE} AND (cast(:cursor AS uuid) IS NULL OR "
                "id>cast(:cursor AS uuid)) "
                "AND (cast(:status AS text) IS NULL OR status=cast(:status AS text)) "
                "AND (:manage_all OR id::text=ANY(:manage_ids) "
                "OR ((:read_all OR id::text=ANY(:read_ids)) AND ("
                "assignment->'principal_ids' @> cast(:actor_json AS jsonb) "
                "OR EXISTS(SELECT 1 FROM human_task_memberships m WHERE m.tenant_id=t.tenant_id AND "
                "m.project_id=t.project_id "
                "AND m.environment_id=t.environment_id AND m.principal_id=:actor "
                "AND t.assignment->'group_ids' @> to_jsonb(ARRAY[m.group_id::text]))))) ORDER BY id LIMIT :limit",
                cursor=cursor,
                status=status,
                manage_all=manage_all,
                manage_ids=manage_ids,
                read_all=read_all,
                read_ids=read_ids,
                actor=actor.id,
                actor_json=json.dumps([str(actor.id)]),
                limit=limit + 1,
            )
            return {
                "items": [task_view(row).model_dump(mode="json") for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def command(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        command: Literal["claim", "release", "reassign", "complete"],
        request: HumanTaskCommand,
        key: str,
        *,
        context: AuditContext,
    ) -> HumanTask:
        capability = "human_task.manage" if command == "reassign" else "human_task." + command
        self.require(actor, scope, capability, context, resource=identifier)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            located = await repository.rows(f"SELECT run_id FROM human_tasks WHERE {SCOPE} AND id=:id", id=identifier)
            if not located:
                raise CatalogError(404, "WV-NOT-FOUND", "Human task not found")
            run = await repository.run(located[0]["run_id"], lock=True)
            rows = await repository.rows(
                f"SELECT * FROM human_tasks WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier
            )
            row = rows[0]
            actor = await human(tx, actor.id)
            self.require(actor, scope, capability, context, resource=identifier)
            await self._readable(repository, row, actor, scope, context)
            current_pin = AssignmentPin.model_validate_json(json.dumps(row["assignment"]))
            if command != "reassign" and not await eligible(repository, current_pin, actor.id):
                raise AccessDenied()
            replay = Idempotency(
                tx,
                actor.id,
                f"human-task:{scope.environment_id}:{identifier}:{command}",
                key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return HumanTask.model_validate_json(json.dumps(prior))
            self.runtime.definitions.registry.require_operational()
            await repository.require_work(run["id"], run["state"])
            await settle(repository, run, terminal_only=True)
            run = await repository.run(run["id"])
            row = (await repository.rows(f"SELECT * FROM human_tasks WHERE {SCOPE} AND id=:id", id=identifier))[0]
            if run["state"]["status"] in TERMINAL or row["status"] not in {"ready", "claimed"}:
                raise CatalogError(409, "WV-HUMAN-TASK-TERMINAL", "Task or run is terminal")
            if row["revision"] != request.expected_revision:
                raise CatalogError(409, "WV-HUMAN-TASK-REVISION", "Task revision changed")
            pin = AssignmentPin.model_validate_json(json.dumps(row["assignment"]))
            if command != "reassign" and not await eligible(repository, pin, actor.id):
                raise AccessDenied()
            now = await repository.now()
            if row["expires_at"] is not None and now >= row["expires_at"]:
                raise CatalogError(409, "WV-HUMAN-TASK-EXPIRED", "Task expiry has elapsed")
            if command == "claim":
                if row["status"] != "ready" or run["state"].get("manual_paused"):
                    raise CatalogError(409, "WV-HUMAN-TASK-CLAIM", "Task cannot be claimed")
                row.update(status="claimed", claimant_id=actor.id)
            elif command == "release":
                if row["status"] != "claimed" or row["claimant_id"] != actor.id:
                    raise CatalogError(409, "WV-HUMAN-TASK-OWNER", "Claim ownership is required")
                row.update(status="ready", claimant_id=None)
            elif command == "reassign":
                assert isinstance(request, ReassignHumanTask)
                target = await human(tx, request.principal_id)
                self.require(target, scope, "human_task.complete", context, resource=identifier)
                if not await eligible(repository, pin, target.id):
                    raise AccessDenied()
                row.update(status="claimed", claimant_id=target.id)
            else:
                assert isinstance(request, CompleteHumanTask)
                if row["status"] != "claimed" or row["claimant_id"] != actor.id:
                    raise CatalogError(409, "WV-HUMAN-TASK-OWNER", "Claim ownership is required")
                output: JsonObject = {"decision": request.decision, "data": request.data}
                measure_value(output)
                artifact = import_artifact(run["artifact"])
                schema: JsonObject = {
                    "type": "object",
                    "properties": {"decision": {"enum": row["decisions"]}, "data": row["form_schema"]},
                    "required": ["decision", "data"],
                    "additionalProperties": False,
                }
                try:
                    validate(workflow(artifact), schema, output)
                except ValueError as error:
                    raise CatalogError(
                        422, "WV-HUMAN-TASK-FORM", "Decision or form violates the task schema"
                    ) from error
                actor = await human(tx, actor.id)
                self.require(actor, scope, capability, context, resource=identifier)
                if not await eligible(repository, pin, actor.id):
                    raise AccessDenied()
                await settle(repository, run, terminal_only=True)
                run = await repository.run(run["id"])
                now = await repository.now()
                if run["state"]["status"] in TERMINAL or (row["expires_at"] is not None and now >= row["expires_at"]):
                    raise CatalogError(409, "WV-HUMAN-TASK-EXPIRED", "Task or workflow deadline has elapsed")
                event = RuntimeEvent(
                    id=uuid4(),
                    type="human_completed",
                    timestamp=now,
                    sequence=run["state"]["accepted_sequence"] + 1,
                    data={
                        "node_id": row["node_id"],
                        "task_id": str(identifier),
                        "actor_id": str(actor.id),
                        "output": output,
                    },
                )
                from firefly_weave.files.authority import admit_human_files

                await admit_human_files(tx, row, output, actor, context=context)
                view = view_of(run)
                result = await transition_async(view.state, event, artifact)
                await repository.execute(
                    "INSERT INTO human_task_decisions "
                    "VALUES(:tenant,:project,:environment,:task,:actor,cast(:output AS jsonb),:now)",
                    task=identifier,
                    actor=actor.id,
                    output=json.dumps(output),
                    now=now,
                )
                await repository.execute(
                    f"UPDATE human_tasks SET status='completed' WHERE {SCOPE} AND id=:id", id=identifier
                )
                await repository.persist(
                    view.model_copy(update={"state": result.state}), event, result, canonical_digest(event.data)
                )
                row.update(status="completed", completed_at=now, decision_actor_id=actor.id, output=output)
            row["revision"] += 1
            await repository.execute(
                "UPDATE human_tasks SET status=:status,claimant_id=:claimant,revision=:revision,"
                "completed_at=:completed,decision_actor_id=:actor,output=cast(:output AS jsonb) WHERE "
                f"{SCOPE} AND id=:id",
                status=row["status"],
                claimant=row["claimant_id"],
                revision=row["revision"],
                completed=row["completed_at"],
                actor=row["decision_actor_id"],
                output=json.dumps(row["output"]),
                id=identifier,
            )
            await repository.execute(
                "INSERT INTO human_task_audit VALUES(:tenant,:project,:environment,:task,:revision,"
                ":action,:actor,:now,:reason)",
                task=identifier,
                revision=row["revision"],
                action=command,
                actor=actor.id,
                now=now,
                reason=request.reason if isinstance(request, ReassignHumanTask) else None,
            )
            result_view = task_view(row)
            await replay.save(result_view.model_dump(mode="json"))
            await audit(
                tx.session,
                actor,
                "human_task." + command,
                str(identifier),
                scope=scope,
                capability=capability,
                context=context,
                details={
                    "revision": row["revision"],
                    **({"reason": request.reason} if isinstance(request, ReassignHumanTask) else {}),
                },
            )
            return result_view

    async def bindings(
        self, actor: Principal, scope: Scope, *, context: AuditContext
    ) -> builtins.list[AssignmentBinding]:
        self.require(actor, scope, "assignment.read", context)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "assignment.read", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM human_assignment_bindings WHERE {SCOPE} ORDER BY name LIMIT 256"
            )
            return [
                AssignmentBinding.model_validate_json(
                    json.dumps({**row["payload"], "name": row["name"], "enabled": row["enabled"]})
                )
                for row in rows
            ]

    async def put_binding(
        self, actor: Principal, scope: Scope, request: AssignmentBindingRequest, key: str, *, context: AuditContext
    ) -> AssignmentBinding:
        self.require(actor, scope, "assignment.manage", context)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await human(tx, actor.id)
            self.require(actor, scope, "assignment.manage", context)
            await lock(tx, f"assignment:{scope}:{request.name}")
            replay = Idempotency(
                tx, actor.id, f"assignment:{scope.environment_id}:{request.name}", key, request.model_dump(mode="json")
            )
            prior = await replay.replay()
            if prior is not None:
                return AssignmentBinding.model_validate_json(json.dumps(prior))
            repository = RuntimeRepository(tx)
            rows = await repository.rows(
                f"SELECT * FROM human_assignment_bindings WHERE {SCOPE} AND name=:name FOR UPDATE", name=request.name
            )
            current = rows[0] if rows else None
            if request.expected_revision != (current["revision"] if current else 0):
                raise CatalogError(409, "WV-ASSIGNMENT-REVISION", "Assignment revision changed")
            if not request.principal_ids and not request.group_ids:
                raise CatalogError(422, "WV-ASSIGNMENT", "Assignment requires candidates")
            for identifier in request.principal_ids:
                target = await human(tx, identifier)
                self.require(target, scope, "human_task.complete", context)
            for identifier in request.group_ids:
                if not await repository.rows(
                    f"SELECT id FROM human_task_groups WHERE {SCOPE} AND id=:id", id=identifier
                ):
                    raise CatalogError(422, "WV-ASSIGNMENT", "Task group is outside the scope")
            result = AssignmentBinding(
                binding_id=current["id"] if current else uuid4(),
                revision=request.expected_revision + 1,
                name=request.name,
                enabled=request.enabled,
                principal_ids=request.principal_ids,
                group_ids=request.group_ids,
            )
            payload = AssignmentPin.model_validate(
                result.model_dump(include=set(AssignmentPin.model_fields))
            ).model_dump_json()
            await repository.execute(
                "INSERT INTO human_assignment_bindings "
                "VALUES(:tenant,:project,:environment,:id,:name,:revision,:enabled,cast(:payload AS jsonb)) "
                "ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,enabled=excluded.enabled,payload=excluded.payload",
                id=result.binding_id,
                name=result.name,
                revision=result.revision,
                enabled=result.enabled,
                payload=payload,
            )
            await replay.save(result.model_dump(mode="json"))
            await audit(
                tx.session,
                actor,
                "assignment.manage",
                str(result.binding_id),
                scope=scope,
                capability="assignment.manage",
                context=context,
            )
            return result

    async def put_group(
        self, actor: Principal, scope: Scope, request: TaskGroupRequest, key: str, *, context: AuditContext
    ) -> TaskGroup:
        self.require(actor, scope, "assignment.manage", context)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await human(tx, actor.id)
            self.require(actor, scope, "assignment.manage", context)
            await lock(tx, f"human-group:{scope}:{request.name}")
            replay = Idempotency(
                tx, actor.id, f"human-group:{scope.environment_id}:{request.name}", key, request.model_dump(mode="json")
            )
            prior = await replay.replay()
            if prior is not None:
                return TaskGroup.model_validate_json(json.dumps(prior))
            repository = RuntimeRepository(tx)
            rows = await repository.rows(
                f"SELECT * FROM human_task_groups WHERE {SCOPE} AND name=:name FOR UPDATE", name=request.name
            )
            current = rows[0] if rows else None
            if request.expected_revision != (current["revision"] if current else 0):
                raise CatalogError(409, "WV-GROUP-REVISION", "Group revision changed")
            for identifier in request.member_ids:
                target = await human(tx, identifier)
                self.require(target, scope, "human_task.complete", context)
            result = TaskGroup(
                id=current["id"] if current else uuid4(),
                name=request.name,
                revision=request.expected_revision + 1,
                member_ids=list(dict.fromkeys(request.member_ids)),
            )
            await repository.execute(
                "INSERT INTO human_task_groups VALUES(:tenant,:project,:environment,:id,:name,:revision) "
                "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                id=result.id,
                name=result.name,
                revision=result.revision,
            )
            await repository.execute(f"DELETE FROM human_task_memberships WHERE {SCOPE} AND group_id=:id", id=result.id)
            for identifier in result.member_ids:
                await repository.execute(
                    "INSERT INTO human_task_memberships VALUES(:tenant,:project,:environment,:group,:principal)",
                    group=result.id,
                    principal=identifier,
                )
            await replay.save(result.model_dump(mode="json"))
            await audit(
                tx.session,
                actor,
                "assignment.group",
                str(result.id),
                scope=scope,
                capability="assignment.manage",
                context=context,
                details={"revision": result.revision},
            )
            return result
