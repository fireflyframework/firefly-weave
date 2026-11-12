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
import contextlib
import copy
import json
from collections.abc import Sequence
from typing import Final, cast
from uuid import UUID

import yaml
from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.ai_policy import DEFAULT_CONTEXT_TOKENS
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
    LumiProposal,
    LumiReply,
    LumiStatus,
)
from firefly_weave.contracts.run_views import RunStepQuery, RunSummary, StepFact
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.values import JsonObject, JsonValue
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


# An AI agent's guards. Weave AI may set one in a proposal, which can tighten it, but a guard the proposal leaves out
# keeps the draft's value: the compact schema small models write from doesn't show them all, and an edit must never
# loosen a guard by omission. Loosening one is done in the editor. Whole guards are carried over when left out;
# settings are also completed key by key, as are a tool's fixed arguments.
STEP_GUARDS: Final = ("onError", "errorPath")
STEP_SETTINGS: Final = ("limits", "memory", "review")
TOOL_GUARDS: Final = ("approval", "human")
TOOL_SETTINGS: Final = ("review",)
MAX_PROPOSAL_CHARACTERS: Final = 100000


def _agents(document: JsonValue) -> dict[str, JsonObject]:
    """The agent steps of a workflow document by id, wherever they sit in its steps."""
    found: dict[str, JsonObject] = {}
    pending: list[JsonValue] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            identifier = node.get("id")
            if node.get("kind") == "agent" and isinstance(identifier, str):
                found.setdefault(identifier, node)
            pending.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            pending.extend(reversed(node))
    return found


def _keep(edited: JsonObject, kept: JsonObject, whole: Sequence[str], settings: Sequence[str]) -> bool:
    """Carry the draft's guards the edit leaves out into it; whether anything was carried."""
    changed = False
    for name in (*whole, *settings):
        if name not in edited and name in kept:
            edited[name] = copy.deepcopy(kept[name])
            changed = True
    for name in settings:
        value, previous = edited.get(name), kept.get(name)
        if isinstance(value, dict) and isinstance(previous, dict):
            for key, setting in previous.items():
                if key not in value:
                    value[key] = copy.deepcopy(setting)
                    changed = True
    return changed


def _keep_fixed(edited: JsonObject, kept: JsonObject) -> bool:
    """A tool's fixed arguments the edit leaves out stay fixed, so the model never gains an argument by omission."""
    if "fixed" not in kept:
        return False
    if "fixed" not in edited:
        edited["fixed"] = copy.deepcopy(kept["fixed"])
        return True
    value, previous = edited["fixed"], kept["fixed"]
    if not (isinstance(value, dict) and isinstance(previous, dict)):
        return False
    arguments, fixed = value.get("object"), previous.get("object")
    if not (isinstance(arguments, dict) and isinstance(fixed, dict)):
        return False
    missing = {key: copy.deepcopy(argument) for key, argument in fixed.items() if key not in arguments}
    arguments.update(missing)
    return bool(missing)


def keep_agent_guards(document: JsonValue, drafts: Sequence[JsonValue]) -> bool:
    """Give each agent a proposal edits the guards it left out, from the draft that holds that agent; in place.

    An agent is matched by its step id in the drafts of the same workflow name (any draft when none has that name),
    each tool by its name. A new agent, a new tool and a removed one are left as written. Whether anything changed.
    """
    if not isinstance(document, dict):
        return False

    def name_of(value: JsonValue) -> object:
        metadata = value.get("metadata") if isinstance(value, dict) else None
        return metadata.get("name") if isinstance(metadata, dict) else None

    same = [draft for draft in drafts if name_of(draft) == name_of(document)]
    previous: dict[str, JsonObject] = {}
    for draft in same or drafts:
        for identifier, step in _agents(draft).items():
            previous.setdefault(identifier, step)
    changed = False
    for identifier, step in _agents(document).items():
        kept = previous.get(identifier)
        if kept is None:
            continue
        changed |= _keep(step, kept, STEP_GUARDS, STEP_SETTINGS)
        old_tools = kept.get("tools")
        tools = {
            tool["name"]: tool
            for tool in (old_tools if isinstance(old_tools, list) else [])
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        new_tools = step.get("tools")
        for tool in new_tools if isinstance(new_tools, list) else []:
            old = tools.get(tool.get("name")) if isinstance(tool, dict) and isinstance(tool.get("name"), str) else None
            if isinstance(tool, dict) and old is not None:
                changed |= _keep(tool, old, TOOL_GUARDS, TOOL_SETTINGS)
                changed |= _keep_fixed(tool, old)
    return changed


def _guarded(proposal: LumiProposal, drafts: Sequence[JsonValue]) -> LumiProposal | None:
    """The proposal with every guard it left out of an existing agent carried over; None when it no longer fits."""
    if not drafts or proposal.kind != "workflow":
        return proposal
    try:
        document = parse_source(proposal.source, format=proposal.format).value
    except Exception:
        return proposal
    if not keep_agent_guards(document, drafts):
        return proposal
    source = (
        json.dumps(document, ensure_ascii=False)
        if proposal.format == "json"
        else yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    )
    # A proposal that can't carry its guards within the size limit is withheld rather than shown loosened.
    return proposal.model_copy(update={"source": source}) if len(source) <= MAX_PROPOSAL_CHARACTERS else None


def check_proposals(reply: LumiReply, drafts: Sequence[JsonValue] = ()) -> LumiAnswer:
    """The reply with each proposal's error count from the platform's own authoring check (no catalog needed).

    ``drafts`` are the workflow documents the question carried: an agent a proposal edits keeps their guards first.
    """
    checked = []
    for given in reply.proposals:
        proposal = _guarded(given, drafts)
        if proposal is None:
            continue
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


def _drafts(request: LumiAskRequest, resources: Sequence[JsonValue]) -> list[JsonValue]:
    """The workflow documents a question carried: its inline draft, then each attached draft."""
    found: list[JsonValue] = []
    if request.draft is not None:
        # A draft that doesn't parse has no agent to keep guards for.
        with contextlib.suppress(Exception):
            found.append(parse_source(request.draft.source, format=request.draft.format).value)
    for resource in resources:
        data = resource.get("data") if isinstance(resource, dict) and resource.get("kind") == "draft" else None
        if isinstance(data, dict) and "document" in data:
            found.append(data["document"])
    return found


def _answer_limit(config: LumiConfiguration, profile: Profile) -> LumiConfiguration:
    """The configuration the gateway receives: the profile's answer limit replaces a larger configured one."""
    limit = output_tokens(profile, config.profile.options.max_tokens)
    if limit == config.profile.options.max_tokens:
        return config
    options = config.profile.options.model_copy(update={"max_tokens": limit})
    return config.model_copy(update={"profile": config.profile.model_copy(update={"options": options})})


# Pages of 200 step facts read for a run attachment: the latest steps of runs up to 10,000 instances.
RUN_PAGES = 50


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
        if summary is None:
            return {"status": "not_recorded_yet"}
        if not isinstance(summary, RunSummary):
            # Classified or blocked by policy for this caller: say so, and read none of its steps.
            return {"status": "unavailable"}
        facts: list[StepFact] = []
        position: tuple[JsonValue, str] | None = None
        for _ in range(RUN_PAGES):
            page = await self.runs.steps(actor, scope, run_id, RunStepQuery(limit=200), position, context=context)
            facts.extend(item for item in page.items if isinstance(item, StepFact))
            if page.next_cursor is None or not page.items:
                return run_context(summary, facts)
            # The next page starts after the last item, as the step cursor does: its scheduled time and key.
            last = page.items[-1]
            position = (last.scheduled_at.isoformat() if last.scheduled_at else None, last.instance_key or last.node_id)
        return run_context(summary, facts, partial=True)

    def _context_tokens(self, connection: ConnectionRevision) -> int | None:
        """The context size the AI policy records for the connection's endpoint, or None when it records none."""
        policy = self.policy.current()
        endpoint = connection.config.get("endpoint")
        entry = policy.entry_for(endpoint) if policy is not None and isinstance(endpoint, str) else None
        if entry is None:
            return None
        if entry.context_tokens is None and entry.compat == "ollama":
            # Ollama runs a model with a small context unless told otherwise; never size for an unbounded one.
            return DEFAULT_CONTEXT_TOKENS
        return entry.context_tokens

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
            fix=request.fix,
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
                resources = await self._attachments(actor, scope, request, context)
                if request.explanation_only:
                    reply = reply.model_copy(update={"proposals": []})
                return await asyncio.to_thread(check_proposals, reply, _drafts(request, resources))
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except SecretUnavailable:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
