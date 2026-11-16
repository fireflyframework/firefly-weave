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


"""Platform-event triggers: which platform events start a workflow's activation, and what each event started.

A trigger binds one activation to some of the events its workflow declares in ``eventTrigger``. Bindings are
immutable: a trigger is created, then disabled; changing one means disabling it and creating another. Each event
offered to a trigger leaves one start record, which names the run it started or why it started none, never the
event's values.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.contracts.definitions import (
    CallerNames,
    ContractModel,
    OmissionOnly,
    ResourceName,
    _is_absent,
    _omit_absent_default,
)
from firefly_weave.contracts.platform_events import EventCode
from firefly_weave.contracts.transactions import PlatformEventType

type EventStartStatus = Literal["requested", "started", "rejected", "skipped"]
# Why a start started no run: the start was refused, the trigger was disabled first, the chain was too deep, or the
# chain had already made all the starts it may make.
type EventStartCode = Literal[
    "WV-TRIGGER-START", "WV-TRIGGER-DISABLED", "WV-TRIGGER-EVENT-DEPTH", "WV-TRIGGER-CHAIN-BUDGET"
]

# Enabled platform-event triggers one environment may hold, so one event requests at most this many starts.
MAX_EVENT_TRIGGERS: Final = 64
# Events from a run this deep in a chain of reactions start no workflows.
MAX_EVENT_DEPTH: Final = 3
# Starts one chain of reactions may make, across all its depths: the chain began with one event of a run that no
# event started, and every run it started, called or retried carries that event's ID.
MAX_CHAIN_STARTS: Final = 64

# Codes and their product copy. Append-only and value-free: start records and problems embed these messages.
EVENT_TRIGGER_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "WV-TRIGGER-EVENTS": (
            "The workflow doesn't accept these platform events. Add them to its eventTrigger in a new version."
        ),
        "WV-TRIGGER-LIMIT": "This environment already has 64 platform-event triggers. Disable one first.",
        "WV-TRIGGER-CONFLICT": "Another enabled platform-event trigger has this name.",
        "WV-TRIGGER-START": "The triggered workflow couldn't start.",
        "WV-TRIGGER-DISABLED": "The trigger was disabled before the workflow started.",
        "WV-TRIGGER-CHAIN-BUDGET": (
            "This chain of reactions has already started 64 workflows, so its events start no more."
        ),
    }
)


class EventTriggerRequest(ContractModel):
    """Bind an activation to platform events. Omitted ``source_workflows`` means every source workflow the
    workflow's own ``from`` allows, or any workflow when it has none."""

    name: ResourceName
    activation_id: UUID
    events: list[PlatformEventType] = Field(min_length=1, max_length=12, json_schema_extra={"uniqueItems": True})
    source_workflows: OmissionOnly[CallerNames] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    # Lets runs of this trigger resolve their transaction; setting it needs transaction.reconcile.
    may_reconcile: bool = False

    @model_validator(mode="after")
    def unique_entries(self) -> Self:
        if len(set(self.events)) != len(self.events):
            raise ValueError("events must be unique")
        if self.source_workflows is not None and len(set(self.source_workflows)) != len(self.source_workflows):
            raise ValueError("source_workflows must be unique")
        return self


class EventTrigger(EventTriggerRequest):
    id: UUID
    # The bound activation's workflow: a trigger ignores events of its own workflow unless its sources name it.
    workflow_name: ResourceName
    # Whose authority starts each run; it must still hold run.start when the run starts.
    principal_id: UUID
    # 1 when created, 2 once disabled.
    revision: int = Field(ge=1)
    disabled: bool = False
    created_at: AwareDatetime


class EventTriggerStart(ContractModel):
    """One platform event offered to one trigger: the run it started, or why it started none. Never the payload."""

    id: UUID
    trigger_id: UUID
    event_id: UUID
    event_type: PlatformEventType
    source_run_id: UUID
    # The started run's place in a chain of reactions: 1 for a run started by an event of a run no event started.
    depth: int = Field(ge=1)
    status: EventStartStatus
    code: EventStartCode | None = Field(default=None, exclude_if=lambda value: value is None)
    # The refusal's own code, such as WV-FORBIDDEN or WV-READINESS.
    cause: EventCode | None = Field(default=None, exclude_if=lambda value: value is None)
    run_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    requested_at: AwareDatetime
    started_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def outcome_matches_status(self) -> Self:
        if (self.status == "started") != (self.run_id is not None):
            raise ValueError("A start names its run exactly when it started one")
        if self.status in {"rejected", "skipped"} and self.code is None:
            raise ValueError("A start that started nothing says why")
        return self
