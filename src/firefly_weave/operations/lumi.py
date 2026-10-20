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
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import validate_authoring
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.parser import parse_source
from firefly_weave.connections.diagnostics import keyless_connection
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, validate_connection
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.lumi import (
    LumiAnswer,
    LumiAskRequest,
    LumiCheckedProposal,
    LumiConfiguration,
    LumiConfigurationRequest,
    LumiReply,
    LumiStatus,
)
from firefly_weave.contracts.run_views import RunStepQuery, RunSummary, StepFact
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.lumi_context import (
    Profile,
    Shaped,
    choose_profile,
    input_budget,
    output_tokens,
    run_context,
    shape,
)
from firefly_weave.operations.lumi_deployments import COLLECTIONS, deployment_context
from firefly_weave.operations.lumi_gateway import LumiGatewayClient
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.persistence.uow import Transaction

# The document kind each proposal kind must hold.
PROPOSAL_KINDS = {
    "workflow": "Workflow",
    "decisionTable": "DecisionTable",
    "action": "Action",
    "connector": "Connector",
}


def check_proposals(reply: LumiReply) -> LumiAnswer:
    """The reply with each proposal's error count from the platform's own authoring check (no catalog needed)."""
    checked = []
    for proposal in reply.proposals:
        result = validate_authoring(proposal.source, format=proposal.format)
        errors = sum(1 for item in result.diagnostics if item.severity == "error")
        try:
            document = parse_source(proposal.source, format=proposal.format).value
        except Exception:
            document = None
        if errors == 0 and (not isinstance(document, dict) or document.get("kind") != PROPOSAL_KINDS[proposal.kind]):
            errors = 1
        checked.append(LumiCheckedProposal(**proposal.model_dump(), diagnostics=errors))
    return LumiAnswer.model_validate({"answer": reply.answer, "proposals": checked, "followUps": reply.follow_ups})


def _answer_limit(config: LumiConfiguration, profile: Profile) -> LumiConfiguration:
    """The configuration the gateway receives: the profile's answer limit replaces a larger configured one."""
    limit = output_tokens(profile, config.profile.options.max_tokens)
    if limit == config.profile.options.max_tokens:
        return config
    options = config.profile.options.model_copy(update={"max_tokens": limit})
    return config.model_copy(update={"profile": config.profile.model_copy(update={"options": options})})


@service
class LumiService:
    def __init__(
        self,
        connections: ConnectionService,
        history: HistoryService,
        debug: DebugService,
        gateway: LumiGatewayClient,
        deployments: DeploymentService,
        policy: APIAIPolicy,
        runs: RunViewService,
    ) -> None:
        self.connections, self.history, self.debug, self.gateway = connections, history, debug, gateway
        self.definitions = connections.definitions
        self.deployments = deployments
        self.policy, self.runs = policy, runs

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

    def _connection(
        self, scope: Scope, configuration: LumiConfigurationRequest, connection: ConnectionRevision
    ) -> str | None:
        """The connection's secret handle, or None for a keyless local model endpoint, which sends no credential."""
        if (
            connection.connector != CONNECTOR_REFERENCE
            or connection.connector_digest != AGENTIC_DESCRIPTOR.manifest.digest
            or connection.config.get("provider") != configuration.profile.provider
        ):
            raise CatalogError(422, "WV-LUMI-CONNECTION", "Weave AI needs its configured provider connection")
        validate_connection(connection)
        if keyless_connection(connection.adapter, connection.config, connection.secret_refs):
            # The reserved no-credential handle is never checked, resolved or sent.
            return None
        handle = connection.secret_refs["apiKey"]
        self.connections.secrets.check(scope, handle)
        return handle

    async def _attachments(
        self, actor: Principal, scope: Scope, request: LumiAskRequest, context: AuditContext
    ) -> list[JsonValue]:
        """The attached resources, read with the caller's own access; the inline draft is shaped separately."""
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
                data = await self._run(actor, scope, attachment.id, context)
            else:
                async with self.definitions.transaction(project, None, mutation=False) as tx:
                    session = await self.debug.inspect(tx, attachment.id, actor=actor, scope=project, context=context)
                    data = {
                        "status": session.view.status,
                        "current_nodes": list(session.view.current_nodes),
                        "diagnostics": [item.model_dump(mode="json") for item in session.view.diagnostics],
                    }
            result.append({"kind": attachment.kind, "id": str(attachment.id), "data": data})
        measure_value(result)
        size = len(json.dumps(result, ensure_ascii=False).encode())
        if size + (len(request.draft.source.encode()) if request.draft else 0) > 262144:
            raise CatalogError(413, "WV-LUMI-LIMIT", "Selected context is too large")
        return result

    async def _run(self, actor: Principal, scope: Scope, run_id: UUID, context: AuditContext) -> JsonValue:
        """A run's facts through the allowlisted builder; the run's own read checks `run.read` each time."""
        summary = await self.runs.summary(actor, scope, run_id, context=context)
        if not isinstance(summary, RunSummary):
            return {"status": "not_recorded_yet"}
        steps = await self.runs.steps(actor, scope, run_id, RunStepQuery(limit=200), None, context=context)
        return run_context(summary, [item for item in steps.items if isinstance(item, StepFact)])

    def _context_tokens(self, connection: ConnectionRevision) -> int | None:
        """The context size the AI policy records for the connection's endpoint, or None when it records none."""
        policy = self.policy.current()
        endpoint = connection.config.get("endpoint")
        entry = policy.entry_for(endpoint) if policy is not None and isinstance(endpoint, str) else None
        return entry.context_tokens if entry is not None else None

    def _shape(
        self, config: LumiConfiguration, request: LumiAskRequest, resources: list[JsonValue], tokens: int | None
    ) -> Shaped:
        profile = choose_profile(tokens, config.context_profile)
        schemas = cast(dict[str, JsonValue], export_schemas())
        return shape(
            profile,
            input_budget(profile, tokens, output_tokens(profile, config.profile.options.max_tokens)),
            message=request.message,
            history=request.history,
            resources=resources,
            draft=(request.draft.format, request.draft.source) if request.draft else None,
            selected_step=request.selected_step,
            schemas=schemas,
            explanation_only=request.explanation_only,
        )

    async def ask(
        self, actor: Principal, scope: Scope, request: LumiAskRequest, *, context: AuditContext
    ) -> LumiAnswer:
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
                tokens = self._context_tokens(connection)
                shaped = self._shape(config, request, await self._attachments(actor, scope, request, context), tokens)
                credential = None
                if handle is not None:
                    secret = await resolve_secret(lambda: self.connections.secrets.resolve(scope, handle))
                    credential = secret.value
                reply = await self.gateway.ask(
                    _answer_limit(config, shaped.profile),
                    request.model_copy(update={"history": shaped.history}),
                    shaped.context,
                    dict(connection.config),
                    credential,
                    context_tokens=tokens,
                )
                async with self.definitions.transaction(scope, None, mutation=False) as tx:
                    actor = await self._require(tx, actor, "lumi.use", context)
                    current = await self._read(tx)
                    if current is None or current.revision != config.revision or not current.enabled:
                        raise CatalogError(409, "WV-LUMI-REVISION", "Weave AI configuration changed; reply discarded")
                # Recheck attachment permissions and simulation ownership before releasing output.
                await self._attachments(actor, scope, request, context)
                if request.explanation_only:
                    reply = reply.model_copy(update={"proposals": []})
                return await asyncio.to_thread(check_proposals, reply)
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except SecretUnavailable:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
