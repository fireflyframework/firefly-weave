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
"""Trusted installed verification and transaction-only protocol-state extension ports."""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.providers import (
    ProviderEvent,
    ProviderIngressResponse,
    ProviderSource,
    ProviderSourceRequest,
)

if TYPE_CHECKING:
    from firefly_weave.persistence.uow import Transaction


@runtime_checkable
class ProviderVerifier(Protocol):
    async def verify(
        self, source: ProviderSource, raw_body: bytes, headers: Mapping[str, str], received_at: datetime
    ) -> tuple[tuple[ProviderEvent, ...], ProviderIngressResponse]: ...
    async def challenge(self, source: ProviderSource, query: Mapping[str, str]) -> ProviderIngressResponse: ...


@runtime_checkable
class ProviderAdmissionHook(Protocol):
    """Optional verifier extension: only new events; no network calls or runtime starts."""

    async def persist(self, tx: "Transaction", source: ProviderSource, events: tuple[ProviderEvent, ...]) -> None: ...


@runtime_checkable
class ProviderSourceValidator(Protocol):
    """Optional pure creation policy: no I/O; inputs are isolated defensive copies."""

    def validate_source(self, request: ProviderSourceRequest, connection: ConnectionRevision) -> None: ...
