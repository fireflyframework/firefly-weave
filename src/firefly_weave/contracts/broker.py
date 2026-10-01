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

"""Broker-family wire contracts; Kafka is the only implemented driver."""

from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.values import JsonObjectData


class SourceBinding(ContractModel):
    id: UUID
    connection_revision_id: UUID
    source_kind: Literal["kafka-trigger", "outbox-subscription", "provider-source"]
    source_id: UUID
    principal_id: UUID
    source_fingerprint: str
    generation: int = 1
    revoked: bool = False


class BrokerTriggerRequest(ContractModel):
    name: ResourceName
    driver: Literal["kafka"] = "kafka"
    connection_revision_id: UUID
    cluster_id: ResourceName
    topic: str = Field(min_length=1, max_length=249, pattern=r"^[a-zA-Z0-9_-][a-zA-Z0-9._-]*$")
    kind: Literal["run", "signal"]
    activation_id: UUID | None = None
    run_id: UUID | None = None
    signal: ResourceName | None = None
    payload_schema: JsonObjectData = Field(default_factory=dict)
    dead_letter_policy: Literal["receipt", "halt"]

    @model_validator(mode="after")
    def exact_target(self) -> "BrokerTriggerRequest":
        if self.kind == "run":
            valid = self.activation_id is not None and self.run_id is None and self.signal is None
        else:
            valid = self.activation_id is None and self.run_id is not None and self.signal is not None
        if not valid:
            raise ValueError("One exact broker target is required")
        return self


class BrokerTrigger(BrokerTriggerRequest):
    id: UUID
    binding_id: UUID
    principal_id: UUID
    disabled: bool = False
    blocked: bool = False


@dataclass(frozen=True)
class BrokerRecord:
    cluster_id: str
    topic: str
    partition: int
    offset: int
    event_id: str | None
    raw_value: bytes | None = field(repr=False)
    valid_headers: bool = True


class AcceptedBrokerReceipt(ContractModel):
    status: Literal["accepted"] = "accepted"
    id: UUID
    trigger_id: UUID
    event_id: UUID
    run_id: UUID
    signal_id: UUID | None = None


class RejectedBrokerReceipt(ContractModel):
    status: Literal["rejected"] = "rejected"
    id: UUID
    trigger_id: UUID
    incident_id: UUID
    code: Literal["BROKER_INVALID", "BROKER_SECRET", "BROKER_EVENT_CONFLICT"]


BrokerReceipt = Annotated[AcceptedBrokerReceipt | RejectedBrokerReceipt, Field(discriminator="status")]


class BrokerIncident(ContractModel):
    id: UUID
    trigger_id: UUID
    receipt_id: UUID
    code: str
    evidence: JsonObjectData
