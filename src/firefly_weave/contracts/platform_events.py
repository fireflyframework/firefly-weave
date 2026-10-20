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


"""Platform events: what a workflow started by a platform-event trigger receives as its input.

The payload is published as the ``platform-event-v1`` schema. It carries metadata only: the transaction's state,
its steps by key and how each is undone, codes, identifiers and the business key, never business values.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.definitions import ContractModel, ResourceName, SemVer
from firefly_weave.contracts.instance_keys import InstanceKeyText
from firefly_weave.contracts.transactions import (
    MAX_TRANSACTION_PARTICIPANTS,
    PlatformEventType,
    TransactionState,
    UndoMode,
)

type EventCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_-]{0,63}$")]
PLATFORM_EVENT_VERSION = "weave/platform-event-v1"


class PlatformEventWorkflow(ContractModel):
    name: ResourceName
    version: SemVer


class TransactionEventStep(ContractModel):
    """One step of the transaction: its key, how it is undone, and for pending work the phase and its attempts."""

    step: ResourceName
    instance_key: InstanceKeyText
    how: UndoMode
    phase: Literal["confirm", "cancel", "undo"] | None = Field(default=None, exclude_if=lambda value: value is None)
    attempts: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)


class TransactionEventSteps(ContractModel):
    done: list[TransactionEventStep] = Field(default_factory=list, max_length=MAX_TRANSACTION_PARTICIPANTS)
    undone: list[TransactionEventStep] = Field(default_factory=list, max_length=MAX_TRANSACTION_PARTICIPANTS)
    pending: list[TransactionEventStep] = Field(default_factory=list, max_length=MAX_TRANSACTION_PARTICIPANTS)


class TransactionEventCause(ContractModel):
    """Why the transaction left its normal path: the platform code, the step, and the step's own failure code."""

    code: EventCode
    step: ResourceName | None = Field(default=None, exclude_if=lambda value: value is None)
    origin_code: EventCode | None = Field(default=None, exclude_if=lambda value: value is None)


class TransactionEventFacts(ContractModel):
    workflow: PlatformEventWorkflow
    run_id: UUID
    instance_key: InstanceKeyText
    business_key: Annotated[str, Field(min_length=1, max_length=200)] | None
    state: TransactionState
    cause: TransactionEventCause | None = Field(default=None, exclude_if=lambda value: value is None)
    steps: TransactionEventSteps
    incident_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    test: bool


class PlatformEvent(ContractModel):
    """The input of a run started by a platform-event trigger."""

    version: Literal["weave/platform-event-v1"]
    event_id: UUID
    type: PlatformEventType
    emitted_at: AwareDatetime
    transaction: TransactionEventFacts
