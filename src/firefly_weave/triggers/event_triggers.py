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

"""Platform-event triggers: bind an activation to the platform events its workflow declares, and read what they did.

A binding may ask only for events, and source workflows, that the bound version declares in ``eventTrigger``.
Bindings are immutable: a trigger is created, then disabled; changing one means disabling it and creating another.
An environment holds at most 64 enabled triggers. Creating one needs ``trigger.manage`` and ``run.start``, and
``transaction.reconcile`` when its runs may resolve transactions. Disabling one is a safety control: it spends the
trigger's own reserve, so it works at a full quota.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import EventTriggerSpec
from firefly_weave.contracts.event_triggers import (
    EVENT_TRIGGER_MESSAGES,
    MAX_EVENT_TRIGGERS,
    EventTrigger,
    EventTriggerRequest,
    EventTriggerStart,
)
from firefly_weave.contracts.public import Page
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService

TRIGGER_COLUMNS = (
    "id,name,activation_id,workflow_name,events,source_workflows,may_reconcile,principal_id,revision,disabled,"
    "created_at"
)
START_COLUMNS = "id,trigger_id,event_id,event_type,source_run_id,depth,status,code,cause,run_id,requested_at,started_at"


def binding_problem(declared: EventTriggerSpec | None, request: EventTriggerRequest) -> str | None:
    """Why a binding asks for more than its workflow declares, or None when the workflow accepts it."""
    if declared is None or not set(request.events) <= set(declared.events):
        return "WV-TRIGGER-EVENTS"
    sources, allowed = request.source_workflows, declared.source_workflows
    if sources is not None and allowed is not None and not set(sources) <= set(allowed):
        return "WV-TRIGGER-EVENTS"
    return None


def effective_sources(declared: EventTriggerSpec | None, request: EventTriggerRequest) -> list[str] | None:
    """The source workflows a trigger reacts to: the binding's own, else the workflow's ``from``, else any (None)."""
    if request.source_workflows is not None:
        return list(request.source_workflows)
    if declared is not None and declared.source_workflows is not None:
        return list(declared.source_workflows)
    return None


def declaration(version: dict[str, Any]) -> EventTriggerSpec | None:
    """The ``eventTrigger`` a published version declares; only a workflow can declare one."""
    spec = version["document"].get("spec", {}) if version["kind"] == "Workflow" else {}
    trigger = spec.get("eventTrigger")
    return None if trigger is None else EventTriggerSpec.model_validate_json(json.dumps(trigger))


def _plain(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _wire(row: dict[str, Any]) -> str:
    """A row as the JSON its contract reads: absent values are left out, never sent as null."""
    return json.dumps({key: value for key, value in row.items() if value is not None}, default=_plain)


def trigger_view(row: dict[str, Any]) -> EventTrigger:
    return EventTrigger.model_validate_json(_wire(row))


def refused(code: str, status: int) -> CatalogError:
    return CatalogError(status, code, EVENT_TRIGGER_MESSAGES[code])


@service
class EventTriggerService:
    def __init__(self, runtime: RuntimeService, access: AccessService) -> None:
        self.runtime, self.access = runtime, access

    async def create(
        self, actor: Principal, scope: Scope, request: EventTriggerRequest, *, context: AuditContext
    ) -> EventTrigger:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            self.runtime.require(actor, scope, "run.start", context)
            if request.may_reconcile:
                self.runtime.require(actor, scope, "transaction.reconcile", context)
            # Refuses a retired, mismatched or test activation, and an activation its runs couldn't start from.
            activation, artifact = await self.runtime.definitions.runtime_snapshot(
                actor, scope, request.activation_id, context=context, tx=tx
            )
            await self.runtime.definitions.execution_readiness(
                actor,
                scope,
                activation.request,
                import_artifact(artifact),
                capability="run.start",
                context=context,
                tx=tx,
            )
            version = await DefinitionRepository(tx).version(activation.request.version_id)
            declared = declaration(version)
            problem = binding_problem(declared, request)
            if problem is not None:
                raise refused(problem, 422)
            repository = RuntimeRepository(tx)
            # The transaction holds the project's operation lock from its first statement, so concurrent creates
            # count and insert one at a time: the limit can't be passed, and the enabled names stay unique.
            [counted] = await repository.rows(
                f"SELECT count(*) AS enabled,count(*) FILTER (WHERE name=:name) AS named FROM event_triggers "
                f"WHERE {SCOPE} AND NOT disabled",
                name=request.name,
            )
            if counted["named"]:
                raise refused("WV-TRIGGER-CONFLICT", 409)
            if counted["enabled"] >= MAX_EVENT_TRIGGERS:
                raise refused("WV-TRIGGER-LIMIT", 409)
            [row] = await repository.rows(
                "INSERT INTO event_triggers(tenant_id,project_id,environment_id,id,name,activation_id,workflow_name,"
                "events,source_workflows,may_reconcile,principal_id,revision,created_at) VALUES(:tenant,:project,"
                ":environment,:id,:name,:activation,:workflow,:events,:sources,:may_reconcile,:principal,1,"
                f"clock_timestamp()) RETURNING {TRIGGER_COLUMNS}",
                id=uuid4(),
                name=request.name,
                activation=request.activation_id,
                workflow=version["name"],
                events=list(request.events),
                sources=effective_sources(declared, request),
                may_reconcile=request.may_reconcile,
                principal=actor.id,
            )
            trigger = trigger_view(row)
            await audit(
                tx.session,
                actor,
                "event_trigger.create",
                str(trigger.id),
                scope=scope,
                capability="trigger.manage",
                context=context,
                details={
                    "activation_id": str(trigger.activation_id),
                    "events": list(trigger.events),
                    "may_reconcile": trigger.may_reconcile,
                },
            )
        return trigger

    async def disable(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> EventTrigger:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            repository = RuntimeRepository(tx)
            rows = await repository.rows(
                f"SELECT {TRIGGER_COLUMNS} FROM event_triggers WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Event trigger not found")
            if rows[0]["disabled"]:
                return trigger_view(rows[0])
            # The update and its audit record spend the trigger's reserve; the transaction's end checks the trigger
            # is disabled and releases what is left.
            await repository.execute("SELECT weave_control_begin('event_trigger',:id)", id=identifier)
            [row] = await repository.rows(
                "UPDATE event_triggers SET disabled=true,disabled_at=clock_timestamp(),revision=revision+1 "
                f"WHERE {SCOPE} AND id=:id RETURNING {TRIGGER_COLUMNS}",
                id=identifier,
            )
            trigger = trigger_view(row)
            await audit(
                tx.session,
                actor,
                "event_trigger.disable",
                str(identifier),
                scope=scope,
                capability="trigger.manage",
                context=context,
                details={"may_reconcile": trigger.may_reconcile},
            )
        return trigger

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            await self.readable(tx, actor, scope, context)
            ids = await page_ids(tx, "event_triggers", limit, cursor)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT {TRIGGER_COLUMNS} FROM event_triggers WHERE {SCOPE} AND id=ANY(:ids) ORDER BY id",
                ids=ids[:limit],
            )
            return {
                "items": [trigger_view(row).model_dump(mode="json") for row in rows],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> EventTrigger:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            await self.readable(tx, actor, scope, context)
            return await self.loaded(tx, identifier)

    async def starts(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        context: AuditContext,
    ) -> Page[EventTriggerStart]:
        """A trigger's starts, newest first, without the events' payloads."""
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        collection = "event_triggers.starts/" + str(identifier)
        position = decode_cursor_v2(cursor, scope, collection)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            await self.readable(tx, actor, scope, context)
            await self.loaded(tx, identifier)
            after = ""
            values: dict[str, Any] = {}
            if position is not None:
                sort_value, last = position
                if not isinstance(sort_value, str):
                    raise ValueError("Invalid scope-bound cursor")
                after = " AND (requested_at,id)<(:after_at,:after_id)"
                values = {"after_at": datetime.fromisoformat(sort_value), "after_id": UUID(last)}
            rows = await RuntimeRepository(tx).rows(
                f"SELECT {START_COLUMNS} FROM event_trigger_starts WHERE {SCOPE} AND trigger_id=:trigger{after} "
                "ORDER BY requested_at DESC,id DESC LIMIT :limit",
                trigger=identifier,
                limit=limit + 1,
                **values,
            )
        items = [EventTriggerStart.model_validate_json(_wire(row)) for row in rows[:limit]]
        if len(rows) <= limit:
            return Page[EventTriggerStart](items=items)
        last_item = items[-1]
        return Page[EventTriggerStart](
            items=items,
            next_cursor=encode_cursor_v2(scope, collection, last_item.requested_at.isoformat(), str(last_item.id)),
        )

    async def readable(self, tx: Transaction, actor: Principal, scope: Scope, context: AuditContext) -> None:
        current = await self.access.load_principal(actor.id, tx=tx)
        self.runtime.require(current, scope, "trigger.manage", context)

    async def loaded(self, tx: Transaction, identifier: UUID) -> EventTrigger:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT {TRIGGER_COLUMNS} FROM event_triggers WHERE {SCOPE} AND id=:id", id=identifier
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Event trigger not found")
        return trigger_view(rows[0])
