# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Pure provider inbox contracts, independent of native and vendor dependencies.

Delivery timestamps and transport retries are not semantic identity. Source scope
and owner are server assigned; providers can only return normalized event facts.
"""

from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import (
    ContractModel,
    DistributionVersion,
    Expression,
    RefExpression,
    ResourceName,
    SemVer,
)
from firefly_weave.contracts.values import JsonObject, JsonObjectData

type Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
type EventIdentity = Annotated[str, Field(min_length=1, max_length=256)]
type SafeReason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


def provider_dispatch_kinds(event_schemas: dict[str, JsonObject], declared: list[str] | None = None) -> tuple[str, ...]:
    """Return the canonical dispatch capability; absence preserves legacy all-kind behavior."""
    if declared is not None and (
        not declared or len(set(declared)) != len(declared) or not set(declared).issubset(event_schemas)
    ):
        raise ValueError("Dispatch event kinds must be a nonempty unique subset of event schemas")
    return tuple(sorted(event_schemas if declared is None else declared))


def provider_schema_digest(event_schemas: dict[str, JsonObject], declared: list[str] | None = None) -> str:
    """Pin schemas and effective dispatch capability using the canonical JSON digest."""
    return canonical_digest(
        {
            "event_schemas": cast(JsonObject, event_schemas),
            "dispatch_event_kinds": list(provider_dispatch_kinds(event_schemas, declared)),
        }
    )


class ProviderSourceRequest(ContractModel):
    name: ResourceName
    provider: ResourceName
    package: ResourceName
    package_version: DistributionVersion
    adapter_version: SemVer | None = None
    schema_digest: Digest
    connection_revision_id: UUID
    policy: JsonObjectData
    kind: Literal["run", "signal"]
    activation_id: UUID | None = None
    run_id: UUID | None = None
    signal: ResourceName | None = None
    mapping: Expression = Field(default_factory=lambda: RefExpression(ref="/payload"))

    @model_validator(mode="after")
    def exact_target(self) -> "ProviderSourceRequest":
        if self.kind == "run":
            valid = self.activation_id is not None and self.run_id is None and self.signal is None
        else:
            valid = self.activation_id is None and self.run_id is not None and self.signal is not None
        if not valid:
            raise ValueError("One exact provider target is required")
        measure_value(self.model_dump(mode="json"))
        return self


class ProviderSource(ProviderSourceRequest):
    adapter_version: SemVer
    id: UUID
    scope: Scope
    binding_id: UUID
    principal_id: UUID
    disabled: bool = False


class ProviderEvent(ContractModel):
    schema_version: Literal["weave/provider-event-v1"] = "weave/provider-event-v1"
    event_id: EventIdentity
    kind: ResourceName
    occurred_at: datetime | None = None
    account_id: EventIdentity | None = None
    conversation_id: EventIdentity | None = None
    sender_id: EventIdentity | None = None
    payload: JsonObjectData
    disposition: Literal["dispatch", "ignore"] = "dispatch"
    reason: SafeReason | None = None

    @model_validator(mode="after")
    def bounded(self) -> "ProviderEvent":
        if (self.disposition == "ignore") != (self.reason is not None):
            raise ValueError("Ignored events require a safe reason; dispatched events cannot carry one")
        if self.occurred_at is not None and self.occurred_at.utcoffset() is None:
            raise ValueError("Provider occurrence time requires an offset")
        measure_value(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ProviderReceipt(ContractModel):
    id: UUID
    source_id: UUID
    provider: ResourceName
    event_id: EventIdentity
    kind: ResourceName
    fingerprint: Digest
    received_at: datetime
    state: Literal["pending", "dispatched", "ignored", "blocked", "failed"]
    attempts: int = Field(default=0, ge=0)
    reason: SafeReason | None = None
    run_id: UUID | None = None
    signal_id: UUID | None = None


class ProviderIngressResponse(ContractModel):
    """Vendor ACK/challenge transport; returned only after durable admission commits."""

    status_code: int = Field(default=200, ge=200, le=299)
    media_type: Literal["application/json", "text/plain"] = "application/json"
    body: str = Field(default="{}", max_length=65536)
