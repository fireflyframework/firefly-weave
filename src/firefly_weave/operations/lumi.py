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

"""Private ephemeral assistant: current caller reads and explicit connection delegation."""

import asyncio
import json
from typing import cast

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, validate_connection
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.lumi import (
    LumiAskRequest,
    LumiConfiguration,
    LumiConfigurationRequest,
    LumiReply,
    LumiStatus,
)
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.lumi_deployments import COLLECTIONS, deployment_context
from firefly_weave.operations.lumi_gateway import LumiGatewayClient
from firefly_weave.persistence.uow import Transaction


@service
class LumiService:
    def __init__(
        self,
        connections: ConnectionService,
        history: HistoryService,
        debug: DebugService,
        gateway: LumiGatewayClient,
        deployments: DeploymentService,
    ) -> None:
        self.connections, self.history, self.debug, self.gateway = connections, history, debug, gateway
        self.definitions = connections.definitions
        self.deployments = deployments

    async def _require(self, tx: Transaction, actor: Principal, capability: str, context: AuditContext) -> Principal:
        if tx.scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Weave AI requires an environment")
        current = await load_principal(tx.session, actor.id)
        self.definitions.require(current, tx.scope, capability, context)
        return current

    @staticmethod
    async def _read(tx: Transaction) -> LumiConfiguration | None:
        payload = await tx.session.scalar(
            text("SELECT payload FROM lumi_configurations WHERE tenant_id=:t AND project_id=:p AND environment_id=:e"),
            {"t": tx.scope.tenant_id, "p": tx.scope.project_id, "e": tx.scope.environment_id},
        )
        return LumiConfiguration.model_validate_json(json.dumps(payload)) if payload is not None else None

    async def configuration(self, actor: Principal, scope: Scope, *, context: AuditContext) -> LumiConfiguration:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require(tx, actor, "lumi.manage", context)
            result = await self._read(tx)
            if result is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Weave AI is not configured")
            return result

    async def status(self, actor: Principal, scope: Scope, *, context: AuditContext) -> LumiStatus:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require(tx, actor, "lumi.use", context)
            config = await self._read(tx)
            if config is None or not config.enabled or not self.gateway.configured:
                return LumiStatus(configured=False)
            return LumiStatus(
                configured=True, provider=config.profile.provider, model=config.profile.model, revision=config.revision
            )

    async def configure(
        self,
        actor: Principal,
        scope: Scope,
        request: LumiConfigurationRequest,
        expected: int | None,
        *,
        context: AuditContext,
    ) -> LumiConfiguration:
        async with self.definitions.transaction(scope, None) as tx:
            actor = await self._require(tx, actor, "lumi.manage", context)
            prior = await self._read(tx)
            if (prior.revision if prior else None) != expected:
                raise CatalogError(409, "WV-LUMI-REVISION", "Weave AI configuration changed; reload it")
            connection = await self.connections.read(
                actor, scope, request.connection_revision_id, context=context, tx=tx
            )
            self._connection(scope, request, connection)
            result = LumiConfiguration(**request.model_dump(), revision=1 if prior is None else prior.revision + 1)
            await tx.session.execute(
                text(
                    "INSERT INTO lumi_configurations(tenant_id,project_id,environment_id,revision,payload) "
                    "VALUES(:t,:p,:e,:r,cast(:v AS jsonb)) ON CONFLICT(tenant_id,project_id,environment_id) "
                    "DO UPDATE SET revision=excluded.revision,payload=excluded.payload"
                ),
                {
                    "t": scope.tenant_id,
                    "p": scope.project_id,
                    "e": scope.environment_id,
                    "r": result.revision,
                    "v": result.model_dump_json(by_alias=True),
                },
            )
            await audit(
                tx.session,
                actor,
                "lumi.configure",
                str(scope.environment_id),
                scope=scope,
                capability="lumi.manage",
                context=context,
                details={"revision": result.revision},
            )
            return result

    def _connection(self, scope: Scope, configuration: LumiConfigurationRequest, connection: ConnectionRevision) -> str:
        if (
            connection.connector != CONNECTOR_REFERENCE
            or connection.connector_digest != AGENTIC_DESCRIPTOR.manifest.digest
            or connection.config.get("provider") != configuration.profile.provider
        ):
            raise CatalogError(422, "WV-LUMI-CONNECTION", "Weave AI needs its configured provider connection")
        validate_connection(connection)
        handle = connection.secret_refs["apiKey"]
        self.connections.secrets.check(scope, handle)
        return handle

    async def _attachments(
        self, actor: Principal, scope: Scope, request: LumiAskRequest, context: AuditContext
    ) -> JsonValue:
        result: list[JsonValue] = []
        project = scope.model_copy(update={"environment_id": None})
        for attachment in request.attachments:
            data: JsonValue
            if attachment.kind in COLLECTIONS:
                record = await self.deployments.read(
                    COLLECTIONS[attachment.kind], attachment.id, actor=actor, scope=scope, context=context
                )
                data = deployment_context(attachment.kind, record)
            elif attachment.kind == "draft":
                value = await self.definitions.read(actor, project, "drafts", attachment.id, context=context)
                data = cast(JsonValue, {"document": value["document"], "revision": value["revision"]})
            elif attachment.kind == "run":
                async with self.definitions.transaction(scope, None, mutation=False) as tx:
                    page = await self.history.page(
                        tx, attachment.id, None, 20, actor=actor, scope=scope, context=context
                    )
                    data = page.model_dump(mode="json")
            else:
                async with self.definitions.transaction(project, None, mutation=False) as tx:
                    session = await self.debug.inspect(tx, attachment.id, actor=actor, scope=project, context=context)
                    data = {
                        "status": session.view.status,
                        "current_nodes": list(session.view.current_nodes),
                        "diagnostics": [item.model_dump(mode="json") for item in session.view.diagnostics],
                    }
            result.append({"kind": attachment.kind, "id": str(attachment.id), "data": data})
        if request.draft is not None:
            result.append({"kind": "inline-source", "format": request.draft.format, "source": request.draft.source})
        measure_value(result)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 262144:
            raise CatalogError(413, "WV-LUMI-LIMIT", "Selected context is too large")
        if request.explanation_only:
            return {
                "resources": result,
                "purpose": "explain-operations",
                "limitations": (
                    "Saved snapshots only, not live cloud state or worker task capacity. "
                    "Explain only; return no proposals."
                ),
            }
        schemas = export_schemas()
        return {
            "resources": result,
            "authoringSchemas": {key: schemas[key] for key in ("workflow", "action", "connector", "decisiontable")},
        }

    async def ask(self, actor: Principal, scope: Scope, request: LumiAskRequest, *, context: AuditContext) -> LumiReply:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await self._require(tx, actor, "lumi.use", context)
            config = await self._read(tx)
            if config is None or not config.enabled or not self.gateway.configured:
                raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is not configured")
            connection = await ConnectionRepository(tx).revision(config.connection_revision_id)
            handle = self._connection(scope, config, connection)
        # No database transaction is held across secret resolution or model I/O.
        try:
            async with asyncio.timeout(config.profile.timeout_seconds):
                attachments = await self._attachments(actor, scope, request, context)
                secret = await resolve_secret(lambda: self.connections.secrets.resolve(scope, handle))
                reply = await self.gateway.ask(config, request, attachments, dict(connection.config), secret.value)
                async with self.definitions.transaction(scope, None, mutation=False) as tx:
                    actor = await self._require(tx, actor, "lumi.use", context)
                    current = await self._read(tx)
                    if current is None or current.revision != config.revision or not current.enabled:
                        raise CatalogError(409, "WV-LUMI-REVISION", "Weave AI configuration changed; reply discarded")
                # Recheck attachment permissions and simulation ownership before releasing output.
                await self._attachments(actor, scope, request, context)
                return reply.model_copy(update={"proposals": []}) if request.explanation_only else reply
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except SecretUnavailable:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
