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

"""Offer a transaction event to the platform-event triggers that accept it, in the event's own commit.

Each enabled trigger of the event's environment that accepts the event's type and source workflow gets one start
record; the event itself is stored once, as the input every start of it gets. A trigger ignores events of its own
workflow unless its sources name that workflow, events of test runs start nothing, and events from a run that sits
three reactions deep are recorded as skipped, so chains of reactions end. The recovery loop starts the requested
records later.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from firefly_weave.contracts.event_triggers import MAX_EVENT_DEPTH, MAX_EVENT_TRIGGERS
from firefly_weave.contracts.integration_events import IntegrationEvent
from firefly_weave.contracts.platform_events import PLATFORM_EVENT_VERSION, PlatformEvent
from firefly_weave.persistence.uow import Transaction


@dataclass(frozen=True)
class TriggerMatch:
    """What matching reads from an enabled trigger."""

    id: UUID
    workflow_name: str
    events: tuple[str, ...]
    # The trigger's effective sources; None reacts to any workflow but its own.
    source_workflows: list[str] | None


@dataclass(frozen=True)
class SourceFacts:
    """The run whose transaction event is offered."""

    run_id: UUID
    workflow_name: str
    depth: int
    test: bool


@dataclass(frozen=True)
class StartRow:
    trigger_id: UUID
    depth: int
    status: Literal["requested", "skipped"]
    code: Literal["WV-TRIGGER-EVENT-DEPTH"] | None = None


def accepts(trigger: TriggerMatch, event_type: str, source_workflow: str) -> bool:
    if event_type not in trigger.events:
        return False
    if trigger.source_workflows is None:
        # A reaction to its own workflow's events would start itself again; only an explicit source allows it.
        return source_workflow != trigger.workflow_name
    return source_workflow in trigger.source_workflows


def start_rows(triggers: Sequence[TriggerMatch], event: IntegrationEvent, source: SourceFacts) -> list[StartRow]:
    """One row per trigger that accepts the event, in trigger order; none for an event of a test run."""
    if source.test:
        return []
    depth = source.depth + 1
    if source.depth >= MAX_EVENT_DEPTH:
        status: Literal["requested", "skipped"] = "skipped"
        code: Literal["WV-TRIGGER-EVENT-DEPTH"] | None = "WV-TRIGGER-EVENT-DEPTH"
    else:
        status, code = "requested", None
    return [
        StartRow(trigger_id=trigger.id, depth=depth, status=status, code=code)
        for trigger in sorted(triggers, key=lambda item: item.id)
        if accepts(trigger, event.type, source.workflow_name)
    ]


def event_payload(event: IntegrationEvent) -> PlatformEvent:
    """The platform event a started run receives: built from the event's own facts, never from author input."""
    facts = event.payload.transaction
    if facts is None:
        raise ValueError("Only an event with transaction facts is a platform event")
    return PlatformEvent.model_validate(
        {
            "version": PLATFORM_EVENT_VERSION,
            "event_id": event.event_id,
            "type": event.type,
            "emitted_at": event.emitted_at,
            "transaction": facts,
        }
    )


async def fan_out(tx: Transaction, event: IntegrationEvent) -> int:
    """Record the starts of a new transaction event in its commit; returns how many start records were written."""
    facts = event.payload.transaction
    if facts is None or event.scope.environment_id is None:
        return 0
    params = {
        "tenant": event.scope.tenant_id,
        "project": event.scope.project_id,
        "environment": event.scope.environment_id,
    }
    scope = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
    depth = await tx.session.scalar(
        text(f"SELECT event_depth FROM run_facts WHERE {scope} AND run_id=:run"), {**params, "run": facts.run_id}
    )
    rows = (
        (
            await tx.session.execute(
                text(
                    f"SELECT id,workflow_name,events,source_workflows FROM event_triggers WHERE {scope} "
                    f"AND NOT disabled ORDER BY id LIMIT {MAX_EVENT_TRIGGERS + 1}"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > MAX_EVENT_TRIGGERS:
        raise RuntimeError("Event trigger admission invariant violated")
    triggers = [
        TriggerMatch(
            id=row["id"],
            workflow_name=row["workflow_name"],
            events=tuple(row["events"]),
            source_workflows=row["source_workflows"],
        )
        for row in rows
    ]
    # The facts are the platform's own: the source's workflow and test flag come with the event.
    source = SourceFacts(run_id=facts.run_id, workflow_name=facts.workflow.name, depth=depth or 0, test=facts.test)
    starts = start_rows(triggers, event, source)
    if not starts:
        return 0
    await tx.session.execute(
        text(
            "INSERT INTO event_trigger_payloads(tenant_id,project_id,environment_id,event_id,source_run_id,payload) "
            "VALUES(:tenant,:project,:environment,:event,:run,cast(:payload AS jsonb)) ON CONFLICT DO NOTHING"
        ),
        {**params, "event": event.event_id, "run": facts.run_id, "payload": event_payload(event).model_dump_json()},
    )
    for start in starts:
        await tx.session.execute(
            text(
                "INSERT INTO event_trigger_starts(tenant_id,project_id,environment_id,id,trigger_id,event_id,"
                "event_type,source_run_id,depth,status,code,requested_at) VALUES(:tenant,:project,:environment,:id,"
                ":trigger,:event,:type,:run,:depth,:status,:code,clock_timestamp()) "
                "ON CONFLICT (trigger_id,event_id) DO NOTHING"
            ),
            {
                **params,
                "id": uuid4(),
                "trigger": start.trigger_id,
                "event": event.event_id,
                "type": event.type,
                "run": facts.run_id,
                "depth": start.depth,
                "status": start.status,
                "code": start.code,
            },
        )
    return len(starts)
