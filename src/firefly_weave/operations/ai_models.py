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

"""Authorized AI metadata and awaited discovery through the configured AI gateway.

In-process callers that save within a supplied transaction may call ``refresh_after`` only
once their transaction commits. HTTP saves and connection tests own their awaited refresh.
"""

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from pyfly.container import service
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.ai_policy import AIPolicy
from firefly_weave.connections.diagnostics import is_agentic_revision
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.ai import (
    AIEndpoint,
    AIEndpointsResult,
    AIModel,
    AIModelsQuery,
    AIModelsResult,
    GatewayModelList,
)
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ai_cache import AIModelCache, CacheKey
from firefly_weave.operations.ai_connections import AIConnectionService
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.operations.lumi_gateway import LumiGatewayClient


@service
class AIModelService:
    def __init__(
        self,
        connections: ConnectionService,
        tests: AIConnectionService,
        policy: APIAIPolicy,
        gateway: LumiGatewayClient,
    ) -> None:
        self.connections, self.tests, self.policy, self.gateway = connections, tests, policy, gateway
        self.cache = AIModelCache()

    def _require(
        self, actor: Principal, current: Principal, scope: Scope, *, manage: bool, context: AuditContext
    ) -> None:
        if scope.project_id is None or scope.environment_id is None:
            raise AccessDenied()
        for principal in (actor, current):
            self.connections.require(principal, scope, "catalog.read", context)
            if manage:
                self.connections.require(principal, scope, "connection.manage", context)

    async def _authorize(self, actor: Principal, scope: Scope, *, manage: bool, context: AuditContext) -> Principal:
        async with self.connections.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self._require(actor, current, scope, manage=manage, context=context)
            return current

    async def _revision(
        self, actor: Principal, scope: Scope, revision_id: UUID, *, manage: bool, context: AuditContext
    ) -> ConnectionRevision:
        async with self.connections.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self._require(actor, current, scope, manage=manage, context=context)
            revision = await ConnectionRepository(tx).revision(revision_id)
            if not is_agentic_revision(revision):
                raise CatalogError(422, "WV-AI-CONNECTION", "Choose an AI connection")
            return revision

    @staticmethod
    def _key(scope: Scope, revision_id: UUID, policy: AIPolicy) -> CacheKey:
        assert scope.project_id is not None and scope.environment_id is not None and policy.sha256 is not None
        return scope.tenant_id, scope.project_id, scope.environment_id, revision_id, policy.sha256

    async def endpoints(self, actor: Principal, scope: Scope, *, context: AuditContext) -> AIEndpointsResult:
        await self._authorize(actor, scope, manage=False, context=context)
        policy = self.policy.current()
        if policy is None:
            return AIEndpointsResult(policy="absent")
        return AIEndpointsResult(
            policy="loaded",
            endpoints=[
                AIEndpoint(
                    id=entry.id,
                    label=entry.label,
                    url=entry.url,
                    providers=list(entry.providers),
                    compat=entry.compat,
                    credential=entry.credential,
                    models="served" if entry.served else "listed",
                    context_tokens=entry.context_tokens,
                    development_only=self.policy.origins.match("model", entry.url) is not None,
                )
                for entry in policy.endpoints
            ],
        )

    async def models(
        self, actor: Principal, scope: Scope, query: AIModelsQuery, *, context: AuditContext
    ) -> AIModelsResult:
        revision = await self._revision(actor, scope, query.connection, manage=query.refresh, context=context)
        if query.refresh:
            return await self._refresh(
                actor, scope, revision, admitted=False, deadline=time.monotonic() + 20.0, context=context
            )
        policy = self.policy.current()
        if policy is None:
            return AIModelsResult(approval="unknown", discovery="unknown")
        entry = policy.entry_for(str(revision.config.get("endpoint", "")))
        provider = str(revision.config.get("provider", ""))
        if entry is None or provider not in entry.providers:
            return AIModelsResult(approval="unknown", discovery="LLM_POLICY")
        cached = self.cache.get(self._key(scope, revision.id, policy))
        return (
            cached
            if cached is not None
            else AIModelsResult(approval="served" if entry.served else "listed", discovery="unknown")
        )

    def _unchanged(self, policy: AIPolicy) -> None:
        current = self.policy.current()
        if current is None or current.sha256 != policy.sha256:
            raise CatalogError(409, "WV-AI-POLICY-CHANGED", "The AI policy changed; refresh the model list")

    async def _refresh(
        self,
        actor: Principal,
        scope: Scope,
        revision: ConnectionRevision,
        *,
        admitted: bool,
        deadline: float,
        context: AuditContext,
    ) -> AIModelsResult:
        policy = self.policy.current()
        if not admitted:
            self.tests.limiter.take(actor.id)
        self.cache.discard_revision(scope, revision.id)
        if policy is None:
            return AIModelsResult(approval="unknown", discovery="unknown")
        entry = policy.entry_for(str(revision.config.get("endpoint", "")))
        provider = str(revision.config.get("provider", ""))
        if entry is None or provider not in entry.providers:
            return AIModelsResult(approval="unknown", discovery="LLM_POLICY")
        raw = GatewayModelList(discovery="LLM_TIMEOUT")
        left = deadline - time.monotonic()
        credential = None
        try:
            if left > 0:
                async with asyncio.timeout(left):
                    credential = await self.tests.credential(scope, revision)
                    await self.connections.revalidate(actor, scope, revision, "connection.manage", context)
                    await self._revision(actor, scope, revision.id, manage=True, context=context)
                    self._unchanged(policy)
                    raw = await self.gateway.models(
                        dict(revision.config),
                        credential,
                        provider=provider,
                        timeout_seconds=min(15.0, max(0.001, deadline - time.monotonic())),
                    )
        except TimeoutError:
            raw = GatewayModelList(discovery="LLM_TIMEOUT")
        except CatalogError as error:
            if error.code in {"WV-AI-GATEWAY-MISSING", "WV-AI-GATEWAY-UNAVAILABLE"}:
                raw = GatewayModelList(discovery="LLM_UNREACHABLE")
            elif error.code == "WV-AI-GATEWAY-TIMEOUT":
                raw = GatewayModelList(discovery="LLM_TIMEOUT")
            elif error.code == "WV-AI-SECRET":
                raw = GatewayModelList(discovery="LLM_AUTH")
            else:
                raise
        finally:
            credential = None
        await self._revision(actor, scope, revision.id, manage=True, context=context)
        self._unchanged(policy)
        models = []
        if raw.discovery == "ok":
            models = [row for row in raw.models if row.approved and entry.approves(provider, row.name)]
            if entry.compat == "ollama" and not entry.served:
                # Explicit gateway refusals must not be reintroduced as unavailable policy entries.
                returned = {row.name for row in raw.models}
                models.extend(
                    AIModel(name=name, approved=True, available=False, tools="unknown")
                    for name in entry.models
                    if name not in returned and entry.approves(provider, name)
                )
            models.sort(key=lambda row: row.name)
        result = AIModelsResult(
            approval="served" if entry.served else "listed",
            discovery=raw.discovery,
            discovered_at=datetime.now(UTC),
            models=models,
        )
        # Validate nested metadata again before auditing or retaining a gateway response.
        result = AIModelsResult.model_validate_json(result.model_dump_json())
        if len(result.model_dump_json().encode("utf-8")) > 262144:
            raise CatalogError(429, "WV-AI-LIMIT", "The model metadata is too large")
        await self._record_refresh(actor, scope, revision.id, result, context)
        self._unchanged(policy)
        self.cache.put(self._key(scope, revision.id, policy), result)
        return result

    async def _record_refresh(
        self, actor: Principal, scope: Scope, revision_id: UUID, result: AIModelsResult, context: AuditContext
    ) -> None:
        async with self.connections.definitions.transaction(scope, None) as tx:
            current = await load_principal(tx.session, actor.id)
            self._require(actor, current, scope, manage=True, context=context)
            await audit(
                tx.session,
                actor,
                "ai.models.refresh",
                str(revision_id),
                scope=scope,
                capability="connection.manage",
                context=context,
                details={"discovery": result.discovery, "models": len(result.models)},
            )
            current = await load_principal(tx.session, actor.id)
            self._require(actor, current, scope, manage=True, context=context)

    async def refresh_after(
        self,
        actor: Principal,
        scope: Scope,
        revision_id: UUID,
        *,
        admitted: bool,
        deadline: float,
        context: AuditContext,
    ) -> None:
        try:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            async with asyncio.timeout(left):
                revision = await self._revision(actor, scope, revision_id, manage=True, context=context)
                await self._refresh(actor, scope, revision, admitted=admitted, deadline=deadline, context=context)
        except (CatalogError, AccessDenied, AuthenticationFailed, TimeoutError, OperationalError, PoolTimeout):
            self.cache.discard_revision(scope, revision_id)
        except DBAPIError as error:
            if not error.connection_invalidated:
                raise
            self.cache.discard_revision(scope, revision_id)
