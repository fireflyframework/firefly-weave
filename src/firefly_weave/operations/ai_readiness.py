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

"""Permission-aware readiness from stored records, without gateway or secret resolution."""

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.connections.diagnostics import keyless_connection
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.ai import AIReadinessFix, AIReadinessItem, AIReadinessResult, ReadinessId
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ai_facts import (
    AIFacts,
    FactLimit,
    builtin_ai_definitions,
    required_ai_capabilities,
    test_time,
)
from firefly_weave.operations.ai_setup import credential_pairs
from firefly_weave.operations.lumi_gateway import LumiGatewayClient
from firefly_weave.workers.repository import WorkerRepository


@dataclass(frozen=True)
class _Unavailable:
    detail: str


async def _observe[T](work: Awaitable[T]) -> T | _Unavailable:
    try:
        return await work
    except FactLimit:
        return _Unavailable("limit_exceeded")
    except (ValueError, CatalogError):
        return _Unavailable("record_unavailable")


def _unknown(identifier: ReadinessId, detail: str) -> AIReadinessItem:
    return AIReadinessItem(id=identifier, status="unknown", detail=detail)


def _requires(identifier: ReadinessId, role: str) -> AIReadinessItem:
    return AIReadinessItem(
        id=identifier,
        status="unknown",
        detail=f"requires_{role}",
        fix=AIReadinessFix(kind="ask", label=f"Requires {role}"),
    )


def model_response_item(results: list[dict[str, Any] | None], now: datetime) -> AIReadinessItem:
    identifier: ReadinessId = "model_responds"
    if not results:
        return AIReadinessItem(id=identifier, status="action", detail="test_required")
    failures = set()
    for row in results:
        if row is None:
            failures.add("test_required")
            continue
        try:
            if row.get("kind") != "ai" or type(row.get("ok")) is not bool:
                raise ValueError("Test outcome unavailable")
            age = now - test_time(row)
            if age < timedelta():
                raise ValueError("Test time unavailable")
        except (ValueError, TypeError):
            return _unknown(identifier, "record_unavailable")
        if not row["ok"]:
            failures.add("test_failed")
        elif age >= timedelta(hours=24):
            failures.add("test_stale")
    detail = next(
        (value for value in ("test_failed", "test_required", "test_stale") if value in failures), "test_passed"
    )
    return AIReadinessItem(id=identifier, status="action" if failures else "done", detail=detail)


def _navigation(workers: bool = False) -> AIReadinessFix:
    return AIReadinessFix(
        kind="navigate",
        label="Open workers" if workers else "Open AI settings",
        route="operate/workers" if workers else "settings/ai",
    )


@service
class AIReadinessService:
    def __init__(self, connections: ConnectionService, gateway: LumiGatewayClient) -> None:
        self.connections, self.gateway = connections, gateway

    def _allowed(
        self,
        actor: Principal,
        current: Principal,
        scope: Scope,
        capability: str,
        context: AuditContext,
        *,
        resource: UUID | None = None,
    ) -> bool:
        try:
            for principal in (actor, current):
                self.connections.definitions.authorization.require(
                    principal,
                    scope,
                    capability,
                    resource=str(resource) if resource is not None else None,
                    context=context,
                )
        except AccessDenied:
            return False
        return True

    @staticmethod
    def _has_grants(actor: Principal, current: Principal, scope: Scope, capabilities: set[str]) -> bool:
        return all(
            principal.active
            and principal.kind != "worker"
            and any(
                contains(grant.scope, scope) and capabilities.intersection(ROLE_CAPABILITIES[grant.role])
                for grant in principal.grants
            )
            for principal in (actor, current)
        )

    def _can_bind(
        self, actor: Principal, current: Principal, scope: Scope, identifier: UUID, context: AuditContext
    ) -> bool:
        try:
            self.connections.require_revision(actor, current, scope, identifier, "connection.bind", context)
        except AccessDenied:
            return False
        return True

    async def read(self, actor: Principal, scope: Scope, *, context: AuditContext) -> AIReadinessResult:
        if scope.project_id is None or scope.environment_id is None:
            raise AccessDenied()
        async with self.connections.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            for principal in (actor, current):
                self.connections.require(principal, scope, "catalog.read", context)
            facts = AIFacts(tx)
            now = await facts.now()
            project = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)
            manage = self._allowed(actor, current, scope, "connection.manage", context)
            releases = await _observe(facts.releases())
            items = [self._installed(releases)]
            if self._allowed(actor, current, project, "catalog.read", context):
                published = await _observe(facts.published())
                publication = self._published(published)
                if (
                    not isinstance(published, _Unavailable)
                    and publication.status == "action"
                    and all(
                        not retired and digest == item.definition_digest
                        for item in builtin_ai_definitions()
                        if item.reference in published
                        for digest, retired in (published[item.reference],)
                    )
                    and self._allowed(actor, current, project, "definition.publish", context)
                ):
                    publication = publication.model_copy(
                        update={"fix": AIReadinessFix(kind="operation", label="Publish", operation="ai_setup.publish")}
                    )
                items.append(publication)
            else:
                items.append(_requires("actions_published", "developer"))
            if not self._has_grants(actor, current, scope, {"status.read"}):
                items.append(_requires("worker_online", "viewer"))
            elif isinstance(releases, _Unavailable):
                items.append(_unknown("worker_online", releases.detail))
            else:
                identifiers = await _observe(facts.worker_ids()) if releases else []
                if isinstance(identifiers, _Unavailable):
                    items.append(_unknown("worker_online", identifiers.detail))
                else:
                    statuses = []
                    denied = False
                    for identifier in identifiers:
                        if not self._allowed(actor, current, scope, "status.read", context, resource=identifier):
                            denied = True
                            continue
                        statuses.append(await _observe(WorkerRepository(tx).status(identifier, observed_at=now)))
                    online = any(
                        not isinstance(row, _Unavailable) and not row.revoked and row.presence == "recent"
                        for row in statuses
                    )
                    if not online and (
                        denied or (not identifiers and not self._allowed(actor, current, scope, "status.read", context))
                    ):
                        items.append(_requires("worker_online", "viewer"))
                    elif not online and any(
                        isinstance(row, _Unavailable) or row.presence == "unknown" for row in statuses
                    ):
                        items.append(_unknown("worker_online", "record_unavailable"))
                    else:
                        items.append(
                            AIReadinessItem(
                                id="worker_online",
                                status="done" if online else "action",
                                detail="worker_online" if online else "worker_offline",
                                fix=None if online else _navigation(True),
                            )
                        )
            connections: list[ConnectionRevision] | _Unavailable
            if not manage and not self._has_grants(actor, current, scope, {"connection.bind"}):
                connections = _Unavailable("requires_deployer")
            else:
                connections = await _observe(facts.latest_connections())
                if (
                    not isinstance(connections, _Unavailable)
                    and not manage
                    and (
                        any(not self._can_bind(actor, current, scope, row.id, context) for row in connections)
                        or (not connections and not self._allowed(actor, current, scope, "connection.bind", context))
                    )
                ):
                    connections = _Unavailable("requires_deployer")
            if isinstance(connections, _Unavailable):
                items.append(
                    _requires("connection", "deployer")
                    if connections.detail == "requires_deployer"
                    else _unknown("connection", connections.detail)
                )
            else:
                items.append(
                    AIReadinessItem(
                        id="connection",
                        status="done" if connections else "action",
                        detail="connection_available" if connections else "connection_missing",
                        fix=_navigation() if manage and not connections else None,
                    )
                )
            managed_items: tuple[ReadinessId, ...] = ("key_available", "connection_authorized", "model_responds")
            if not manage:
                items.extend(_requires(name, "tenant_admin") for name in managed_items)
            elif isinstance(connections, _Unavailable):
                items.extend(_unknown(name, connections.detail) for name in managed_items)
            else:
                credentialed = [
                    row for row in connections if not keyless_connection(row.adapter, row.config, row.secret_refs)
                ]
                items.append(self._keys(scope, connections, credentialed))
                if connections and not credentialed:
                    items.append(
                        AIReadinessItem(id="connection_authorized", status="not_needed", detail="no_credential")
                    )
                elif isinstance(releases, _Unavailable):
                    items.append(_unknown("connection_authorized", releases.detail))
                elif not connections:
                    items.append(
                        AIReadinessItem(id="connection_authorized", status="action", detail="connection_missing")
                    )
                elif not releases:
                    items.append(AIReadinessItem(id="connection_authorized", status="action", detail="worker_missing"))
                else:
                    pairs = await _observe(facts.granted_pairs(tuple(row.id for row in credentialed)))
                    authorization = self._authorized(releases, credentialed, pairs)
                    if authorization.status == "action" and all(credential_pairs([release]) for release in releases):
                        authorization = authorization.model_copy(
                            update={
                                "fix": AIReadinessFix(kind="operation", label="Authorize", operation="ai_setup.grant")
                            }
                        )
                    items.append(authorization)
                tests = await _observe(facts.latest_tests(tuple(row.id for row in connections))) if connections else {}
                model = (
                    _unknown("model_responds", tests.detail)
                    if isinstance(tests, _Unavailable)
                    else model_response_item([tests.get(row.id) for row in connections], now)
                )
                if model.status == "action" and connections:
                    model = model.model_copy(
                        update={
                            "fix": AIReadinessFix(
                                kind="operation", label="Test connection", operation="ai_connections.test"
                            )
                        }
                    )
                items.append(model)
            lumi_manage = self._allowed(actor, current, scope, "lumi.manage", context)
            if not lumi_manage and not self._allowed(actor, current, scope, "lumi.use", context):
                items.append(_requires("weave_ai", "lumi_user"))
            else:
                lumi = await _observe(facts.lumi())
                if isinstance(lumi, _Unavailable):
                    items.append(_unknown("weave_ai", lumi.detail))
                else:
                    detail = (
                        "weave_ai_missing"
                        if lumi is None
                        else "weave_ai_disabled"
                        if not lumi.enabled
                        else "gateway_missing"
                        if not self.gateway.configured
                        else "weave_ai_ready"
                    )
                    items.append(
                        AIReadinessItem(
                            id="weave_ai",
                            status="done" if detail == "weave_ai_ready" else "action",
                            detail=detail,
                            fix=_navigation() if lumi_manage and detail != "weave_ai_ready" else None,
                        )
                    )
            return AIReadinessResult(items=items)

    @staticmethod
    def _installed(releases: list[WorkerRelease] | _Unavailable) -> AIReadinessItem:
        if isinstance(releases, _Unavailable):
            return _unknown("worker_installed", releases.detail)
        found = {f"{cap.task_type}@{cap.task_version}" for row in releases for cap in row.capabilities}
        installed = required_ai_capabilities() <= found
        return AIReadinessItem(
            id="worker_installed",
            status="done" if installed else "action",
            detail="worker_installed" if installed else "worker_missing",
            fix=None
            if installed
            else AIReadinessFix(
                kind="command", label="Ask an operator to enable AI", command="weave platform ai enable --ollama auto"
            ),
        )

    @staticmethod
    def _published(published: dict[str, tuple[str, bool]] | _Unavailable) -> AIReadinessItem:
        if isinstance(published, _Unavailable):
            return _unknown("actions_published", published.detail)
        missing = any(row.reference not in published or published[row.reference][1] for row in builtin_ai_definitions())
        mismatch = any(
            row.reference in published and published[row.reference][0] != row.definition_digest
            for row in builtin_ai_definitions()
        )
        detail = "actions_missing" if missing else "catalog_mismatch" if mismatch else "actions_published"
        return AIReadinessItem(
            id="actions_published", status="done" if detail == "actions_published" else "action", detail=detail
        )

    def _keys(
        self, scope: Scope, connections: list[ConnectionRevision], credentialed: list[ConnectionRevision]
    ) -> AIReadinessItem:
        if not connections:
            return AIReadinessItem(id="key_available", status="action", detail="connection_missing")
        if not credentialed:
            return AIReadinessItem(id="key_available", status="not_needed", detail="no_credential")
        try:
            for row in credentialed:
                if not row.secret_refs:
                    raise ValueError("Credential metadata unavailable")
                for handle in row.secret_refs.values():
                    self.connections.secrets.check(scope, handle)
        except (CatalogError, ValueError):
            return AIReadinessItem(
                id="key_available",
                status="action",
                detail="key_missing",
                fix=AIReadinessFix(kind="ask", label="Ask an operator to set the connection secret"),
            )
        return AIReadinessItem(id="key_available", status="done", detail="key_grant_available")

    @staticmethod
    def _authorized(
        releases: list[WorkerRelease],
        connections: list[ConnectionRevision],
        pairs: set[tuple[UUID, UUID, str]] | _Unavailable,
    ) -> AIReadinessItem:
        if isinstance(pairs, _Unavailable):
            return _unknown("connection_authorized", pairs.detail)
        needed = {
            (release.id, row.id, capability)
            for release in releases
            for row in connections
            for capability in required_ai_capabilities()
            if capability in release.credential_capabilities
        }
        capable = all(set(release.credential_capabilities) & required_ai_capabilities() for release in releases)
        done = capable and bool(needed) and needed <= pairs
        return AIReadinessItem(
            id="connection_authorized",
            status="done" if done else "action",
            detail="connection_authorized" if done else "authorization_missing",
        )
