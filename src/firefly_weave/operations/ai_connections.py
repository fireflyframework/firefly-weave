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

"""AI connection tests: one short model call through the AI gateway, never from the API itself.

A test needs ``connection.manage``, shares a limit of six per minute per principal with model
refreshes, is stored with the connection's test results and audited as ``ai.connection.test``.
The answer carries a code, the latency and tool support, never the model's text.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from uuid import UUID, uuid4

from pydantic import ValidationError
from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import audit
from firefly_weave.connections.diagnostics import keyless_connection
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE
from firefly_weave.contracts.ai import AIConnectionTestRequest, AIConnectionTestResult
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision, ConnectionTestResult
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.lumi_gateway import LumiGatewayClient

TESTS_PER_MINUTE = 6


class AIRateLimit:
    """At most six AI connection tests and model refreshes per principal per minute in this API process."""

    def __init__(
        self, limit: int = TESTS_PER_MINUTE, window: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._calls: dict[UUID, deque[float]] = {}

    def take(self, principal: UUID) -> None:
        now = self.clock()
        calls = self._calls.setdefault(principal, deque())
        while calls and now - calls[0] >= self.window:
            calls.popleft()
        if len(calls) >= self.limit:
            raise CatalogError(429, "WV-AI-RATE-LIMITED", "Too many AI connection tests; wait a minute, then retry")
        calls.append(now)


def _agentic(revision: ConnectionRevision) -> bool:
    return revision.connector == CONNECTOR_REFERENCE and revision.connector_digest == AGENTIC_DESCRIPTOR.manifest.digest


@service
class AIConnectionService:
    def __init__(self, connections: ConnectionService, gateway: LumiGatewayClient) -> None:
        self.connections, self.gateway = connections, gateway
        self.limiter = AIRateLimit()

    async def test(
        self,
        actor: Principal,
        scope: Scope,
        revision_id: UUID,
        request: AIConnectionTestRequest,
        *,
        context: AuditContext,
    ) -> AIConnectionTestResult:
        self.connections.require(actor, scope, "connection.manage", context)
        self.limiter.take(actor.id)
        revision = await self.connections.ready_revision(
            actor, scope, revision_id, "connection.manage", context=context
        )
        if not _agentic(revision):
            raise CatalogError(422, "WV-AI-CONNECTION", "Choose an AI connection")
        if not self.gateway.configured:
            raise CatalogError(
                503,
                "WV-AI-GATEWAY-MISSING",
                "Tests need the AI gateway. Run weave platform ai enable, or ask an operator to deploy it.",
            )
        credential = await self._credential(scope, revision)
        raw = await self.gateway.test(
            dict(revision.config),
            credential,
            provider=str(revision.config.get("provider")),
            model=request.model,
            probe_tools=request.probe_tools,
        )
        try:
            result = AIConnectionTestResult.model_validate(raw)
        except ValidationError:
            raise CatalogError(
                503, "WV-AI-GATEWAY-UNAVAILABLE", "The AI gateway returned an unexpected answer"
            ) from None
        await self._record(actor, scope, revision, result, context)
        return result

    async def _credential(self, scope: Scope, revision: ConnectionRevision) -> str | None:
        if keyless_connection(revision.adapter, revision.config, revision.secret_refs):
            # The reserved no-credential handle is never resolved, leased, logged or sent.
            return None
        handle = revision.secret_refs.get("apiKey")
        if handle is None:
            raise CatalogError(422, "WV-AI-CONNECTION", "The AI connection has no apiKey secret handle")
        try:
            return (await resolve_secret(partial(self.connections.secrets.resolve, scope, handle))).value
        except SecretUnavailable:
            raise CatalogError(503, "WV-AI-SECRET", "The AI connection's secret handle is not available") from None

    async def _record(
        self,
        actor: Principal,
        scope: Scope,
        revision: ConnectionRevision,
        result: AIConnectionTestResult,
        context: AuditContext,
    ) -> None:
        job = uuid4()
        payload = {"kind": "ai", "tested_at": datetime.now(UTC).isoformat(), **result.model_dump(mode="json")}
        async with self.connections.uow.open(scope) as tx:
            repository = ConnectionRepository(tx)
            await repository.execute(
                "INSERT INTO connection_test_jobs VALUES(:id,:tenant,:project,:environment,:revision,:principal)",
                id=job,
                revision=revision.id,
                principal=actor.id,
            )
            await repository.execute(
                "INSERT INTO connection_test_results VALUES(:job,:tenant,:project,"
                ":environment,cast(:payload AS jsonb))",
                job=job,
                payload=json.dumps(payload),
            )
            await audit(
                tx.session,
                actor,
                "ai.connection.test",
                str(revision.id),
                scope=scope,
                capability="connection.manage",
                context=context,
                details={
                    "ok": result.ok,
                    "code": result.code,
                    "model": result.model,
                    "tool_calling": result.tool_calling,
                    "latency_ms": result.latency_ms,
                },
            )


class GatewayConnectionTester:
    """The generic connection test of AI connections: a gateway test without a tool probe."""

    def __init__(self, gateway: LumiGatewayClient) -> None:
        self.gateway = gateway

    async def __call__(self, connection: BoundConnection) -> ConnectionTestResult:
        if not self.gateway.configured:
            return ConnectionTestResult(ok=False, code="failed")
        revision = connection.revision
        keyless = keyless_connection(revision.adapter, revision.config, revision.secret_refs)
        credential = None if keyless else connection.credentials("apiKey").value
        try:
            raw = await self.gateway.test(
                dict(revision.config),
                credential,
                provider=str(revision.config.get("provider")),
                model=None,
                probe_tools=False,
            )
        except CatalogError:
            return ConnectionTestResult(ok=False, code="failed")
        ok = raw.get("ok") is True
        return ConnectionTestResult(ok=ok, code="ok" if ok else "failed")
