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

"""Idempotent built-in AI publication and exact admitted credential authorization."""

import json
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.diagnostics import AGENTIC_ADAPTER, is_agentic_revision, keyless_connection
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import TASK_TYPE, TASK_VERSION, task_capability
from firefly_weave.contracts.ai import AISetupGrantRequest, AISetupGrantResult, AISetupPublishResult
from firefly_weave.contracts.workers import CredentialGrantRequest, WorkerRelease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.ai_facts import (
    MAX_BYTES,
    MAX_FACTS,
    AIFacts,
    BuiltinAI,
    FactLimit,
    builtin_ai_definitions,
)
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.service import WorkerService


def publication_key(item: BuiltinAI) -> str:
    return f"weave-ai-{item.kind}-{item.definition_digest}"


def credential_pairs(releases: list[WorkerRelease]) -> tuple[tuple[UUID, str], ...]:
    known = {f"{TASK_TYPE}@{TASK_VERSION}": task_capability()}
    digests = {key: FrozenDocument.from_value(value.model_dump(by_alias=True)).digest for key, value in known.items()}
    pairs = set()
    for release in releases:
        for capability in release.capabilities:
            reference = f"{capability.task_type}@{capability.task_version}"
            if (
                reference in release.credential_capabilities
                and reference in digests
                and FrozenDocument.from_value(capability.model_dump(by_alias=True)).digest == digests[reference]
            ):
                pairs.add((release.id, reference))
    return tuple(sorted(pairs))


def _limit() -> CatalogError:
    return CatalogError(429, "WV-AI-LIMIT", "AI setup exceeds the bounded record limit")


@service
class AISetupService:
    def __init__(self, definitions: DefinitionService, connections: ConnectionService, workers: WorkerService) -> None:
        self.definitions, self.connections, self.workers = definitions, connections, workers

    async def _publication_preflight(self, tx: Transaction, builtins: tuple[BuiltinAI, ...]) -> None:
        self.definitions.registry.validate_manifest(builtins[0].document)
        # Compilation reads every admitted release in the project, including other environments.
        count, size = (
            await tx.session.execute(
                text(
                    "SELECT count(*),coalesce(sum(octet_length(payload::text)),0) FROM worker_releases "
                    "WHERE tenant_id=:tenant AND project_id=:project"
                ),
                {"tenant": tx.scope.tenant_id, "project": tx.scope.project_id},
            )
        ).one()
        if not isinstance(count, int) or not isinstance(size, int) or count < 0 or size < 0:
            raise ValueError("Publication size unavailable")
        if count > MAX_FACTS or size > MAX_BYTES:
            raise FactLimit()
        stored = await AIFacts(tx).published()
        for item in builtins:
            prior = stored.get(item.reference)
            if prior is None:
                continue
            digest, retired = prior
            if retired:
                raise CatalogError(409, "WV-AI-CATALOG-RETIRED", "A built-in AI definition is retired")
            if digest != item.definition_digest:
                raise CatalogError(409, "WV-VERSION-CONFLICT", "An immutable version already has different content")

    async def publish(self, actor: Principal, scope: Scope, *, context: AuditContext) -> AISetupPublishResult:
        if scope.project_id is None:
            raise AccessDenied()
        project = scope.model_copy(update={"environment_id": None})
        try:
            async with self.definitions.transaction(project, None) as tx:
                current = await load_principal(tx.session, actor.id)
                for principal in (actor, current):
                    self.definitions.authorization.require(principal, project, "definition.publish", context=context)
                builtins = builtin_ai_definitions()
                await self._publication_preflight(tx, builtins)
                versions = []
                for item in builtins:
                    versions.append(
                        await self.definitions.publish(
                            actor,
                            project,
                            item.kind,
                            json.dumps(item.document, sort_keys=True, separators=(",", ":")),
                            "json",
                            publication_key(item),
                            context=context,
                            tx=tx,
                        )
                    )
                result = AISetupPublishResult(connector=versions[0], actions=versions[1:])
                await audit(
                    tx.session,
                    current,
                    "ai.setup.publish",
                    "ai",
                    scope=project,
                    capability="definition.publish",
                    context=context,
                    details={"definitions": len(versions)},
                )
                return result
        except FactLimit:
            raise _limit() from None

    async def grant(
        self, actor: Principal, scope: Scope, request: AISetupGrantRequest, *, context: AuditContext
    ) -> AISetupGrantResult:
        if scope.project_id is None or scope.environment_id is None:
            raise AccessDenied()
        try:
            async with self.definitions.transaction(scope, None) as tx:
                current = await load_principal(tx.session, actor.id)
                for principal in (actor, current):
                    self.connections.require(principal, scope, "connection.manage", context)
                revision = await ConnectionRepository(tx).revision(request.connection_revision_id)
                if (
                    revision.id != request.connection_revision_id
                    or revision.adapter != AGENTIC_ADAPTER
                    or not is_agentic_revision(revision)
                ):
                    raise CatalogError(422, "WV-AI-CONNECTION", "Choose an AI connection")
                await self.connections._ready(current, scope, revision, "connection.manage", context, tx)
                if keyless_connection(revision.adapter, revision.config, revision.secret_refs):
                    await audit(
                        tx.session,
                        current,
                        "ai.setup.grant",
                        str(revision.id),
                        scope=scope,
                        capability="connection.manage",
                        context=context,
                        details={"not_needed": True},
                    )
                    return AISetupGrantResult(granted=False, not_needed=True)
                facts = AIFacts(tx)
                pairs = credential_pairs(await facts.releases())
                if not pairs:
                    raise CatalogError(
                        422, "WV-AI-WORKER-MISSING", "Admit an AI worker release before authorizing this connection"
                    )
                existing = await facts.granted_pairs((revision.id,))
                for release_id, capability in pairs:
                    if (release_id, revision.id, capability) not in existing:
                        await self.workers.grant_connection(
                            actor,
                            scope,
                            CredentialGrantRequest(
                                release_id=release_id, connection_revision_id=revision.id, capability=capability
                            ),
                            context=context,
                            tx=tx,
                        )
                await audit(
                    tx.session,
                    current,
                    "ai.setup.grant",
                    str(revision.id),
                    scope=scope,
                    capability="connection.manage",
                    context=context,
                    details={"not_needed": False, "pairs": len(pairs)},
                )
                return AISetupGrantResult(granted=True)
        except FactLimit:
            raise _limit() from None
