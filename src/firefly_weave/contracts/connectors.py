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

"""Reference-only connection contracts and adapter execution ports."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.values import JsonObject, JsonObjectData, JsonValue

if TYPE_CHECKING:
    from firefly_weave.contracts.workers import ConnectorExecutionPin


class ResolvedSecret(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    value: str = Field(repr=False, exclude=True)
    provider_version: str | None = None


class ConnectionRequest(ContractModel):
    name: ResourceName
    connector_version_id: UUID
    config: JsonObjectData = Field(default_factory=dict)
    secret_refs: dict[ResourceName, ResourceName] = Field(alias="secretRef", default_factory=dict)
    allowed_destinations: tuple[str, ...] = ()


type ConnectionIssueCode = Literal["CONNECTOR", "CONFIG", "AUTH", "SECRET", "DESTINATION"]


@dataclass(frozen=True)
class ConnectionIssue:
    """A safe finding about a connection request; ``path`` is a JSON pointer into the request body.

    Messages are plain language and never contain secret values or the names of other handles.
    """

    path: str
    message: str
    code: ConnectionIssueCode = "CONFIG"


class ConnectionInvalid(ValueError):
    """Descriptor connection validation failure that explains each rejected field."""

    def __init__(self, issues: Sequence[ConnectionIssue]) -> None:
        super().__init__("Connection requirements are unavailable or incompatible")
        self.issues = tuple(issues)


class ConnectionRevision(ConnectionRequest):
    id: UUID
    revision: int
    connector: str
    connector_digest: str
    adapter: str


class ConnectionTestResult(ContractModel):
    ok: bool
    code: Literal["ok", "failed"] = "ok"
    job_id: UUID | None = None


@dataclass(frozen=True)
class BoundConnection:
    slot: str
    revision: ConnectionRevision
    credentials: Callable[[str], ResolvedSecret] = field(repr=False)


@dataclass(frozen=True)
class ConnectorInvocation:
    connection: ConnectionRevision
    config: JsonObject
    action: str
    input_schema: JsonObject
    output_schema: JsonObject
    max_request_bytes: int
    max_response_bytes: int
    target: "ConnectorExecutionPin | None" = None
    schema_bundle: dict[str, JsonObject] = field(default_factory=dict)


class ConnectorFailure(Exception):
    def __init__(self, code: str, outcome: Literal["not_started", "failed", "unknown"] = "unknown") -> None:
        self.code, self.outcome = code, outcome
        super().__init__(code)


@dataclass(frozen=True)
class ActionContext:
    operation_key: str
    attempt_deadline: datetime
    credentials: Callable[[str], Awaitable[ResolvedSecret]] = field(repr=False)
    invocation: ConnectorInvocation
    authorize: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    reference: Callable[[UUID, int, str | None], Awaitable[JsonObject]] | None = field(default=None, repr=False)
    email_submit: Callable[[JsonObject], Awaitable[JsonObject]] | None = field(default=None, repr=False)


class ConnectorAdapter(Protocol):
    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue: ...
    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult: ...
