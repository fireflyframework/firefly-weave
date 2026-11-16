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

"""Activation admits AI steps only for connections and models the operator's AI policy approves."""

import importlib.util
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.ai_policy import AIPolicy, PolicyEndpoint, from_pairs, render
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import (
    ActionNode,
    AgentNode,
    AgentToolIR,
    Dependency,
    Participation,
    TransformNode,
    UndoPhaseNode,
    WorkflowIR,
)
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
from firefly_weave.contracts.agentic import (
    AGENTIC_DESCRIPTOR,
    AgenticConnectionAdapter,
    action_definition,
    task_capability,
)
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.ai_cache import AIModelCache
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.operations.lumi_gateway import LumiGatewayClient
from firefly_weave.persistence.uow import Transaction

CONNECTOR = "weave-agentic-provider@1.0.0"
ENDPOINT = "https://models.example/v1"
OLLAMA = "http://ollama:11434"
PROVIDER_MESSAGE = "The AI connection does not match the step's provider"
MODEL_MESSAGE = "The model is not approved for this AI connection"
RESULT = {"type": "string", "minLength": 1, "maxLength": 2000}


def validator():
    assert importlib.util.find_spec("firefly_weave.definitions.ai"), "The activation AI validator is unavailable"
    return importlib.import_module("firefly_weave.definitions.ai").validate_ai_bindings


def endpoint(*models, url=ENDPOINT, providers=("openai-chat",), credential="required"):
    return PolicyEndpoint(
        id="models",
        label="Models",
        url=url,
        providers=providers,
        credential=credential,
        models="served" if models == ("served",) else models,
    )


def approving(*models, **changes):
    return AIPolicy(version=2, endpoints=(endpoint(*models, **changes),))


def ai_node(model="chosen", provider="openai-chat", connection="ai"):
    return ActionNode.model_construct(
        id="summarize", connection=connection, llm_profile=LLMProfile.model_construct(provider=provider, model=model)
    )


def bound(provider="openai-chat", url=ENDPOINT):
    return SimpleNamespace(revision=SimpleNamespace(config={"provider": provider, "endpoint": url}))


# Pure admission: no database, cache, gateway, network or secret is involved.


def test_loaded_policy_rejects_provider_before_model():
    node = ActionNode.model_construct(
        connection="ai", llm_profile=LLMProfile.model_construct(provider="openai-chat", model="chosen")
    )
    policy = AIPolicy(
        version=2,
        endpoints=(
            PolicyEndpoint(
                id="models",
                label="Models",
                url="https://models.example/v1",
                providers=("openai-chat",),
                credential="required",
                models=("chosen",),
            ),
        ),
    )
    bindings = {"ai": SimpleNamespace(revision=SimpleNamespace(config={"provider": "anthropic", "endpoint": ENDPOINT}))}
    with pytest.raises(CatalogError) as refused:
        validator()([node], bindings, policy)
    assert (refused.value.status, refused.value.code, str(refused.value)) == (
        422,
        "WV-ACTIVATION-AI-PROVIDER",
        PROVIDER_MESSAGE,
    )
    validator()([node], bindings, None)


def test_a_provider_mismatch_is_reported_even_when_the_model_is_also_unapproved():
    # Choosing the right connection comes first; approving the model would not fix this step.
    with pytest.raises(CatalogError) as refused:
        validator()([ai_node(model="unlisted")], {"ai": bound(provider="openai-responses")}, approving("chosen"))
    assert (refused.value.status, refused.value.code, str(refused.value)) == (
        422,
        "WV-ACTIVATION-AI-PROVIDER",
        PROVIDER_MESSAGE,
    )


def test_matching_provider_and_listed_model_are_admitted():
    validator()([ai_node()], {"ai": bound()}, approving("chosen", "other"))


@pytest.mark.parametrize(
    "policy,node,url",
    [
        (approving("chosen", url="https://elsewhere.example/v1"), ai_node(), ENDPOINT),
        (approving("chosen", providers=("anthropic", "openai-responses")), ai_node(), ENDPOINT),
        (approving("chosen"), ai_node(model="unlisted"), ENDPOINT),
        (approving("served", url=OLLAMA + "/v1", credential="none"), ai_node(model="bad model"), OLLAMA + "/v1"),
        (approving("chosen"), ai_node(model=""), ENDPOINT),
    ],
    ids=["unlisted-endpoint", "provider-not-approved", "unlisted-model", "invalid-served-name", "empty-model"],
)
def test_unapproved_endpoint_provider_or_model_is_refused_as_a_model_problem(policy, node, url):
    with pytest.raises(CatalogError) as refused:
        validator()([node], {"ai": bound(url=url)}, policy)
    assert (refused.value.status, refused.value.code, str(refused.value)) == (
        422,
        "WV-ACTIVATION-AI-MODEL",
        MODEL_MESSAGE,
    )


def test_a_connection_without_an_endpoint_is_refused_as_a_model_problem():
    bindings = {"ai": SimpleNamespace(revision=SimpleNamespace(config={"provider": "openai-chat"}))}
    with pytest.raises(CatalogError) as refused:
        validator()([ai_node()], bindings, approving("chosen"))
    assert refused.value.code == "WV-ACTIVATION-AI-MODEL"


def test_served_endpoint_admits_any_valid_name_without_discovery(monkeypatch):
    def consulted(*args, **kwargs):
        raise AssertionError("Activation consulted model discovery")

    monkeypatch.setattr(AIModelCache, "get", consulted)
    monkeypatch.setattr(AIModelCache, "put", consulted)
    monkeypatch.setattr(LumiGatewayClient, "models", consulted)
    policy = approving("served", url=OLLAMA + "/v1", credential="none")
    validator()([ai_node(model="not-installed:1b")], {"ai": bound(url=OLLAMA + "/v1")}, policy)


def test_version_one_policies_keep_exact_provider_and_model_pairs():
    policy = from_pairs([("openai-chat", "chosen"), ("openai-responses", "other")], [ENDPOINT])
    assert policy.version == 1
    validator()([ai_node(model="chosen")], {"ai": bound()}, policy)
    with pytest.raises(CatalogError) as refused:
        validator()([ai_node(model="other")], {"ai": bound()}, policy)
    assert refused.value.code == "WV-ACTIVATION-AI-MODEL"


def test_steps_that_are_not_ai_tasks_are_never_checked():
    plain = ActionNode.model_construct(id="fetch", connection="http", llm_profile=None)
    unbound = ActionNode.model_construct(id="notify", connection=None, llm_profile=None)
    transform = TransformNode.model_construct(id="copy")
    validator()([plain, unbound, transform], {}, approving())


@pytest.mark.parametrize("connection", [None, "optional-ai"])
def test_an_unbound_ai_step_keeps_the_connection_error_before_any_policy_check(connection):
    with pytest.raises(CatalogError) as refused:
        validator()([ai_node(connection=connection)], {"ai": bound(provider="anthropic")}, approving())
    assert (refused.value.status, refused.value.code) == (422, "WV-CONNECTION")


def test_every_ai_step_is_checked():
    nodes = [ai_node(), ai_node(model="other", connection="backup")]
    bindings = {"ai": bound(), "backup": bound()}
    validator()(nodes, bindings, approving("chosen", "other"))
    with pytest.raises(CatalogError) as refused:
        validator()(nodes, bindings, approving("chosen"))
    assert refused.value.code == "WV-ACTIVATION-AI-MODEL"


def agent_node(model="chosen", provider="openai-chat", connection="ai"):
    return AgentNode.model_construct(
        id="support", connection=connection, llm_profile=LLMProfile.model_construct(provider=provider, model=model)
    )


def test_an_agent_on_another_providers_connection_is_refused_at_activation():
    with pytest.raises(CatalogError) as refused:
        validator()([agent_node()], {"ai": bound(provider="openai-responses")}, approving("chosen"))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-PROVIDER")


def test_an_agents_model_must_be_approved_for_its_connection():
    validator()([agent_node()], {"ai": bound()}, approving("chosen"))
    with pytest.raises(CatalogError) as refused:
        validator()([agent_node(model="unlisted")], {"ai": bound()}, approving("chosen"))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-MODEL")


def reserved():
    return importlib.import_module("firefly_weave.definitions.ai").validate_ai_reserved


def agent_ir() -> WorkflowIR:
    return WorkflowIR.model_validate(compiled(agent_workflow()).executable)


def with_nodes(ir: WorkflowIR, nodes) -> WorkflowIR:
    return ir.model_copy(update={"graph": ir.graph.model_copy(update={"nodes": nodes})})


def test_an_ir_that_runs_the_turn_task_outside_an_agent_is_refused_at_activation():
    ir = agent_ir()
    reserved()(ir)
    turn = next(node for node in ir.graph.nodes if isinstance(node, AgentNode)).dependency
    # An action step pinned to the platform's turn action would run the model without the agent's guards.
    stray = ActionNode.model_construct(id="turn", dependency=turn, connection="ai", llm_profile=None)

    with pytest.raises(CatalogError) as refused:
        reserved()(with_nodes(ir, [*ir.graph.nodes, stray]))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")


def test_an_agent_whose_turn_action_isnt_the_platforms_is_refused_at_activation():
    ir = agent_ir()
    nodes = [
        node.model_copy(update={"dependency": "sha256:" + "0" * 64}) if isinstance(node, AgentNode) else node
        for node in ir.graph.nodes
    ]

    with pytest.raises(CatalogError) as refused:
        reserved()(with_nodes(ir, nodes))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")


@pytest.mark.parametrize("task", ["turn", "generate"])
def test_an_agent_tool_pinned_to_an_ai_task_is_refused_at_activation(task):
    ir = agent_ir()
    agent = next(node for node in ir.graph.nodes if isinstance(node, AgentNode))
    generate = next(
        dependency
        for dependency in WorkflowIR.model_validate(compiled(ai_workflow()).executable).dependencies
        if dependency.kind == "Action"
    )
    ir = ir.model_copy(update={"dependencies": [*ir.dependencies, generate]})
    # A model must never start another model through a tool: that call would skip the agent's token cap.
    tool = AgentToolIR.model_construct(
        name="ask_model", target="action", dependency=agent.dependency if task == "turn" else generate.digest
    )
    lookup = AgentToolIR.model_construct(name="lookup", target="human", dependency=None)

    def with_tools(*tools):
        armed = agent.model_copy(update={"tools": list(tools)})
        return with_nodes(ir, [armed if node is agent else node for node in ir.graph.nodes])

    reserved()(with_tools(lookup))
    with pytest.raises(CatalogError) as refused:
        reserved()(with_tools(lookup, tool))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")


def plain_action() -> Dependency:
    """An Action that runs no model: it undoes a change through a connector."""
    document = deepcopy(action_definition())
    document["metadata"]["name"] = "remove-quantity"
    document["spec"]["implementation"] = {"kind": "connector", "uses": "inventory@1.0.0", "action": "remove"}
    return Dependency.model_construct(
        kind="Action", reference="remove-quantity@1.0.0", digest=canonical_digest(document), document=document
    )


def undone(step: str, undo: str) -> ActionNode:
    """A step of a transaction pinned to ``step``, compensated by an action pinned to ``undo``."""
    phase = UndoPhaseNode.model_construct(kind="action", dependency=undo, input={"literal": {}}, connection="ai")
    txn = Participation.model_construct(owner="move", mode="compensate", phases={"compensate": phase})
    return ActionNode.model_construct(id="add", dependency=step, connection=None, llm_profile=None, txn=txn)


@pytest.mark.parametrize("where", ["tool", "step", "undo"])
def test_a_pinned_document_activation_cannot_read_is_refused_rather_than_assumed_safe(where):
    plain = plain_action()
    ir = agent_ir()
    ir = ir.model_copy(update={"dependencies": [*ir.dependencies, plain]})
    reserved()(with_nodes(ir, [*ir.graph.nodes, undone(plain.digest, plain.digest)]))
    agent = next(node for node in ir.graph.nodes if isinstance(node, AgentNode))
    # A digest the IR carries no document for: what it runs can't be checked, so it may be the model turn.
    unknown = "sha256:" + "1" * 64
    if where == "tool":
        tool = AgentToolIR.model_construct(name="lookup", target="action", dependency=unknown)
        nodes = [agent.model_copy(update={"tools": [tool]}) if node is agent else node for node in ir.graph.nodes]
    elif where == "step":
        nodes = [
            *ir.graph.nodes,
            ActionNode.model_construct(id="act", dependency=unknown, connection="ai", llm_profile=None),
        ]
    else:
        nodes = [*ir.graph.nodes, undone(plain.digest, unknown)]

    with pytest.raises(CatalogError) as refused:
        reserved()(with_nodes(ir, nodes))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")
    assert str(refused.value) == "The workflow pins a document it doesn't carry, so what that runs can't be checked"


@pytest.mark.parametrize("task", ["turn", "generate"])
def test_an_undo_action_that_runs_a_model_task_is_refused_at_activation(task):
    plain = plain_action()
    generate = next(
        dependency
        for dependency in WorkflowIR.model_validate(compiled(ai_workflow()).executable).dependencies
        if dependency.kind == "Action"
    )
    ir = agent_ir()
    ir = ir.model_copy(update={"dependencies": [*ir.dependencies, plain, generate]})
    turn = next(node for node in ir.graph.nodes if isinstance(node, AgentNode)).dependency
    reserved()(with_nodes(ir, [*ir.graph.nodes, undone(plain.digest, plain.digest)]))
    # An undo runs after the step with none of an agent's guards: no token cap, data notice or reviewed transcript.
    model = undone(plain.digest, turn if task == "turn" else generate.digest)

    with pytest.raises(CatalogError) as refused:
        reserved()(with_nodes(ir, [*ir.graph.nodes, model]))
    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")
    assert str(refused.value) == "Only an AI agent step runs the platform's model turn"


# Activation: real definition, connection and authorization services over an in-memory environment.


def profile(model):
    return {"provider": "openai-chat", "model": model, "options": {"max_tokens": 64}, "outputSchema": RESULT}


def llm(identifier, name, slot):
    return {
        "id": identifier,
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": name,
        "connection": slot,
        "prompt": {"literal": "Summarize the supplied text."},
        "context": {"ref": "/input/text"},
    }


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def ai_workflow():
    """Two AI tasks on two connections; the second sits inside a switch branch."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "summarize", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT_SCHEMA,
            "outputSchema": {"type": "string"},
            "connections": {"ai": {"connector": CONNECTOR}, "backup": {"connector": CONNECTOR}},
            "llmProfiles": {"main": profile("chosen"), "fallback": profile("other")},
            "steps": [
                llm("summarize", "main", "ai"),
                {
                    "id": "route",
                    "kind": "switch",
                    "cases": [
                        {
                            "when": {"literal": False},
                            "steps": [llm("retry", "fallback", "backup")],
                            "output": {"literal": "retried"},
                        }
                    ],
                    "default": {"steps": [], "output": {"literal": "done"}},
                },
            ],
            "output": {"ref": "/steps/summarize/output/result"},
        },
    }


def agent_workflow(model="chosen"):
    """An AI agent with no tools on one AI connection: only the model's own checks apply."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "support-desk", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT_SCHEMA,
            "outputSchema": {},
            "connections": {"ai": {"connector": CONNECTOR}},
            "llmProfiles": {"support": profile(model)},
            "steps": [
                {
                    "id": "support",
                    "kind": "agent",
                    "uses": "weave-agentic-turn@1.0.0",
                    "profile": "support",
                    "connection": "ai",
                    "instructions": {"literal": "You answer order questions."},
                    "prompt": {"ref": "/input/text"},
                    "tools": [],
                }
            ],
            "output": {"literal": None},
        },
    }


def plain_workflow():
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "copy-text", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT_SCHEMA,
            "outputSchema": {"type": "string"},
            "steps": [{"id": "copy", "kind": "transform", "value": {"ref": "/input/text"}}],
            "output": {"ref": "/steps/copy/output"},
        },
    }


def compiled(document):
    catalog = CatalogSnapshot.from_definitions(
        [
            load_definition(AGENTIC_DESCRIPTOR.manifest.value),
            load_definition(action_definition()),
            load_definition(turn_action_definition()),
        ],
        tasks=[task_capability(), turn_capability()],
        adapters=["weave-agentic-provider"],
    )
    result = compile_source(document, format="object", catalog=catalog)
    assert result.ok, result.to_bytes()
    assert result.artifact is not None
    return result.artifact


class Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Environment:
    """The records one activation reads and writes, with rollback on failure."""

    def __init__(self, scope):
        self.scope = scope
        self.versions = {}
        self.revisions = {}
        self.saved = {}
        self.activations = []
        self.bound = []
        self.audits = []
        self.events = []
        self.trace = []
        self.statements = []
        self.transaction = None
        self.info = {}

    def get_transaction(self):
        return self.transaction

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        assert self.transaction is not None and self.transaction.is_active
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            self.trace.append("lock")
            return Rows()
        if sql.startswith("SELECT request_hash,response FROM mutation_idempotency"):
            self.trace.append("replay")
            found = self.saved.get((params["principal"], params["operation"], params["key"]))
            return Rows([] if found is None else [{"request_hash": found[0], "response": found[1]}])
        if sql.startswith("INSERT INTO mutation_idempotency"):
            self.trace.append("save")
            self.saved[(params["principal"], params["operation"], params["key"])] = (
                params["hash"],
                json.loads(params["response"]),
            )
            return Rows()
        if sql.startswith("SELECT v.*, EXISTS"):
            assert params["tenant"] == self.scope.tenant_id and params["project"] == self.scope.project_id
            found = self.versions.get(params["id"])
            return Rows([] if found is None else [found])
        if sql.startswith("SELECT revision FROM activation_revisions"):
            self.trace.append("revision")
            revisions = [a["revision"] for a in self.activations if a["name"] == params["name"]]
            return Rows([{"revision": max(revisions)}] if revisions else [])
        if sql.startswith("INSERT INTO activation_revisions"):
            self.trace.append("insert")
            self.activations.append(dict(params))
            return Rows()
        if sql.startswith("INSERT INTO activation_connections"):
            self.bound.append(dict(params))
            return Rows()
        raise AssertionError("Unexpected statement: " + sql)

    async def scalar(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.statements.append(sql)
        if "current_setting('weave.tenant_id',true)" in sql:
            return str(self.scope.tenant_id)
        if sql.startswith("SELECT octet_length(to_jsonb(v)::text)"):
            return 1
        if sql.startswith("SELECT payload FROM connection_revisions"):
            assert params["tenant"] == self.scope.tenant_id and params["project"] == self.scope.project_id
            found = self.revisions.get(params["id"])
            if found is None or found[0] != params["environment"]:
                return None
            return found[1].model_dump(mode="json", by_alias=True)
        if "clock_timestamp()" in sql:
            return datetime.now(UTC)
        raise AssertionError("Unexpected statement: " + sql)


class RecordingPolicy:
    def __init__(self, environment, value):
        self.environment, self.value = environment, value

    def current(self):
        self.environment.trace.append("policy")
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def revision(env, name, *, provider="openai-chat", url=ENDPOINT, handle="private-handle", environment_id=None):
    value = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name=name,
        connector_version_id=env.connector_id,
        connector=CONNECTOR,
        connector_digest=AGENTIC_DESCRIPTOR.manifest.digest,
        adapter="weave-agentic-provider",
        config={"provider": provider, "endpoint": url, "secretSlot": "apiKey"},
        secretRef={"apiKey": handle},
        allowed_destinations=(url.removesuffix("/v1"),),
    )
    env.revisions[value.id] = (environment_id or env.scope.environment_id, value)
    return value


def workflow_version(env, document):
    artifact = compiled(document)
    identifier = uuid4()
    env.versions[identifier] = {
        "id": identifier,
        "kind": "Workflow",
        "name": document["metadata"]["name"],
        "version": document["metadata"]["version"],
        "digest": artifact.digest,
        "document": document,
        "artifact": json.loads(artifact.to_bytes()),
        "retired": False,
    }
    return identifier, artifact.digest


@pytest.fixture
def env(monkeypatch):
    import firefly_weave.connections.service as connection_module
    import firefly_weave.definitions.service as definition_module

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    env = Environment(scope)
    env.connector_id = uuid4()
    env.versions[env.connector_id] = {
        "id": env.connector_id,
        "kind": "Connector",
        "retired": False,
        "definition_digest": AGENTIC_DESCRIPTOR.manifest.digest,
        "document": AGENTIC_DESCRIPTOR.manifest.value,
        "artifact": {"executable": {"dependencies": []}},
    }
    env.actor = Principal(id=uuid4(), kind="human", grants=(Grant(role="deployer", scope=scope),))
    env.current = env.actor

    @asynccontextmanager
    async def opened(given, mutation=True):
        assert given == scope and mutation and env.transaction is None
        saved = deepcopy((env.saved, env.activations, env.bound, env.audits, env.events))
        env.transaction = SimpleNamespace(is_active=True)
        env.info = {"weave_admission": (env.transaction, scope.tenant_id, scope.project_id)}
        try:
            yield Transaction(env, given)
        except BaseException:
            env.saved, env.activations, env.bound, env.audits, env.events = saved
            raise
        finally:
            env.transaction.is_active = False
            env.transaction = None

    async def current_principal(session, identifier):
        assert session is env and identifier == env.actor.id
        env.trace.append("current")
        return env.current

    async def binding_principal(session, identifier):
        assert session is env and identifier == env.actor.id
        return env.current

    async def audit(session, principal, action, resource, **kwargs):
        assert session is env and env.transaction is not None
        env.trace.append("audit")
        env.audits.append(action)

    def validate(nodes, bindings, policy):
        env.trace.append("validate")
        env.validated = (list(nodes), dict(bindings), policy)
        return validator()(nodes, bindings, policy)

    monkeypatch.setattr(definition_module, "load_principal", current_principal)
    monkeypatch.setattr(connection_module, "load_principal", binding_principal)
    monkeypatch.setattr(definition_module, "audit", audit)
    monkeypatch.setattr(definition_module, "validate_ai_bindings", validate)

    class Admission:
        async def admit(self, actor, given, request, artifact, *, capability, context, tx):
            assert capability == "release.activate" and tx.session is env
            env.trace.append("admit")
            return []

    class Outbox:
        async def append(self, tx, event):
            assert tx.session is env
            env.trace.append("outbox")
            env.events.append(event)

    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    provider = SimpleNamespace(resolve=lambda handle: pytest.fail("Activation resolved a secret"))
    secrets = ScopedSecrets({"test": provider}, (SecretGrant(scope, "private-handle", "test", "value"),))
    definitions = object.__new__(DefinitionService)
    definitions.uow = SimpleNamespace(open=opened)
    definitions.authorization = AuthorizationService()
    definitions.registry = registry
    definitions.workers = SimpleNamespace(get=Admission)
    definitions.outbox = Outbox()
    definitions.ai_policy = RecordingPolicy(env, approving("chosen", "other"))
    connections = ConnectionService(definitions.uow, definitions, registry, secrets)
    resolve = connections.resolve_binding

    async def resolve_binding(actor, given, slot, identifier, connector, *, context, tx=None):
        result = await resolve(actor, given, slot, identifier, connector, context=context, tx=tx)
        env.trace.append("resolved:" + slot)
        return result

    connections.resolve_binding = resolve_binding
    definitions.connections = SimpleNamespace(get=lambda: connections)
    env.definitions = definitions
    env.ai = revision(env, "ai")
    env.backup = revision(env, "backup")
    env.version_id, env.digest = workflow_version(env, ai_workflow())
    return env


def use_policy(env, value):
    env.definitions.ai_policy = RecordingPolicy(env, value)


async def activate(env, key="activation-key", *, version=None, slots=None, expected_revision=None):
    version_id, digest = version or (env.version_id, env.digest)
    request = ActivationRequest(
        version_id=version_id,
        artifact_digest=digest,
        scope=env.scope,
        connection_revision_ids=(slots if slots is not None else {"ai": env.ai.id, "backup": env.backup.id}),
    )
    return await env.definitions.activate(env.actor, env.scope, request, key, expected_revision, context=AuditContext())


def inserts(env):
    return [sql for sql in env.statements if sql.startswith("INSERT")]


FRESH_ADMISSION = [
    "current",
    "lock",
    "replay",
    "admit",
    "resolved:ai",
    "resolved:backup",
    "lock",
    "revision",
    "policy",
    "validate",
    "insert",
    "audit",
    "outbox",
    "save",
]


async def test_activation_checks_every_ai_step_after_bindings_and_immediately_before_insert(env):
    activation = await activate(env)
    assert env.trace == FRESH_ADMISSION
    nodes, bindings, policy = env.validated
    assert policy == approving("chosen", "other")
    assert {node.id for node in nodes if isinstance(node, ActionNode) and node.llm_profile} == {"summarize", "retry"}
    assert set(bindings) == {"ai", "backup"} and all(isinstance(b, BoundConnection) for b in bindings.values())
    assert (bindings["ai"].revision.id, bindings["backup"].revision.id) == (env.ai.id, env.backup.id)
    assert [row["id"] for row in env.activations] == [activation.id] and len(env.bound) == 2
    assert env.audits == ["release.activate"] and len(env.events) == 1


@pytest.mark.parametrize(
    "refusal,code,message",
    [
        ("provider", "WV-ACTIVATION-AI-PROVIDER", PROVIDER_MESSAGE),
        ("model", "WV-ACTIVATION-AI-MODEL", MODEL_MESSAGE),
        ("endpoint", "WV-ACTIVATION-AI-MODEL", MODEL_MESSAGE),
    ],
)
async def test_policy_refusal_writes_no_activation_connection_audit_or_replay(env, refusal, code, message):
    if refusal == "provider":
        env.backup = revision(env, "backup", provider="openai-responses")
    elif refusal == "model":
        use_policy(env, approving("chosen"))
    else:
        use_policy(env, approving("chosen", "other", url="https://elsewhere.example/v1"))
    with pytest.raises(CatalogError) as refused:
        await activate(env)
    assert (refused.value.status, refused.value.code, str(refused.value)) == (422, code, message)
    assert refused.value.result is None
    assert env.trace == FRESH_ADMISSION[: FRESH_ADMISSION.index("validate") + 1]
    assert not inserts(env) and not env.activations and not env.bound
    assert not env.audits and not env.events and not env.saved


@pytest.mark.parametrize(
    ("refusal", "code"),
    [("provider", "WV-ACTIVATION-AI-PROVIDER"), ("model", "WV-ACTIVATION-AI-MODEL")],
)
async def test_an_agent_step_with_an_unknown_provider_or_model_is_refused_at_activation(env, refusal, code):
    if refusal == "provider":
        env.ai = revision(env, "ai", provider="openai-responses")
    agent = workflow_version(env, agent_workflow("unlisted" if refusal == "model" else "chosen"))

    with pytest.raises(CatalogError) as refused:
        await activate(env, version=agent, slots={"ai": env.ai.id})

    assert (refused.value.status, refused.value.code) == (422, code)
    assert not inserts(env) and not env.activations


async def test_a_replay_returns_the_committed_activation_without_a_new_policy_admission(env):
    first = await activate(env)
    use_policy(env, approving())
    env.trace.clear()
    env.statements.clear()
    assert await activate(env) == first
    assert env.trace == ["current", "lock", "replay"]
    assert not inserts(env)
    with pytest.raises(CatalogError) as refused:
        await activate(env, "fresh-key", expected_revision=1)
    assert refused.value.code == "WV-ACTIVATION-AI-MODEL"
    assert [row["id"] for row in env.activations] == [first.id] and not inserts(env)


@pytest.mark.parametrize("snapshot", ["actor", "current"])
@pytest.mark.parametrize("key", ["activation-key", "fresh-key"])
async def test_replays_and_new_activations_require_current_activation_authority(env, snapshot, key):
    first = await activate(env)
    setattr(env, snapshot, getattr(env, snapshot).model_copy(update={"grants": ()}))
    env.trace.clear()
    env.statements.clear()
    with pytest.raises(AccessDenied):
        await activate(env, key, expected_revision=None if key == "activation-key" else 1)
    assert "replay" not in env.trace and "policy" not in env.trace and not inserts(env)
    assert [row["id"] for row in env.activations] == [first.id]


@pytest.mark.parametrize("failure", ["foreign", "unready", "narrowed-grant"])
async def test_binding_authority_and_readiness_precede_any_policy_answer(env, failure):
    # The policy would refuse this revision's provider; that answer must never be what the caller sees.
    if failure == "foreign":
        env.backup = revision(env, "backup", provider="openai-responses", environment_id=uuid4())
    elif failure == "unready":
        env.backup = revision(env, "backup", provider="openai-responses", handle="ungranted-handle")
    else:
        env.backup = revision(env, "backup", provider="openai-responses")
        env.current = env.current.model_copy(
            update={"grants": (Grant(role="deployer", scope=env.scope, resources=(str(env.ai.id),)),)}
        )
    with pytest.raises((CatalogError, AccessDenied)) as refused:
        await activate(env)
    expected = {"foreign": (404, "WV-NOT-FOUND"), "unready": (422, "WV-CONNECTION"), "narrowed-grant": None}[failure]
    if expected is None:
        # A bind grant limited to one revision carries no activation authority, so entry refuses it.
        assert isinstance(refused.value, AccessDenied) and env.trace == ["current"]
    else:
        assert (refused.value.status, refused.value.code) == expected
        assert "openai" not in str(refused.value)
    assert "policy" not in env.trace and "validate" not in env.trace and not inserts(env)


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_bind_denied_on_one_revision_is_reported_before_any_policy_read(env, monkeypatch, snapshot):
    # A role that may activate releases without binding connections, beside a bind limited to one revision.
    import firefly_weave.access.authorization as authorization_module

    roles = dict(authorization_module.ROLE_CAPABILITIES)
    roles["activator"] = frozenset({"release.activate"})
    monkeypatch.setattr(authorization_module, "ROLE_CAPABILITIES", MappingProxyType(roles))
    grants = (
        Grant.model_construct(role="activator", scope=env.scope, resources=()),
        Grant(role="deployer", scope=env.scope, resources=(str(env.ai.id),)),
    )
    setattr(env, snapshot, getattr(env, snapshot).model_copy(update={"grants": grants}))
    env.backup = revision(env, "backup", provider="openai-responses")  # the policy would refuse this provider
    with pytest.raises(AccessDenied):
        await activate(env)
    assert env.trace == ["current", "lock", "replay", "admit", "resolved:ai"]
    assert not inserts(env) and not env.activations


async def test_absent_policy_keeps_existing_activation_behavior(env):
    env.backup = revision(env, "backup", provider="openai-responses")
    use_policy(env, None)
    activation = await activate(env)
    assert env.trace == FRESH_ADMISSION and env.validated[2] is None
    assert [row["id"] for row in env.activations] == [activation.id]


async def test_activation_without_ai_steps_never_reads_the_policy(env):
    use_policy(env, CatalogError(503, "WV-AI-POLICY", "The AI policy is unavailable"))
    version = workflow_version(env, plain_workflow())
    activation = await activate(env, version=version, slots={})
    assert "policy" not in env.trace and "validate" not in env.trace
    assert [row["id"] for row in env.activations] == [activation.id]


def stray_turn_version(env):
    """A stored workflow whose only step is an action step pinned to the platform's turn action, rehashed.

    The compiler refuses such a step, so the version is forged after compilation: activation is the last line.
    """
    document = agent_workflow()
    value = json.loads(compiled(document).to_bytes())
    nodes = value["executable"]["graph"]["nodes"]
    agent = next(node for node in nodes if node["kind"] == "agent")
    stray = {key: agent[key] for key in ("id", "path", "scope", "connection", "dependency")}
    stray.update(kind="action", **{"with": {"object": {"prompt": agent["prompt"]}}})
    nodes[nodes.index(agent)] = stray
    # Without the agent the graph uses no language feature, so it is the version that AI tasks compile to.
    del value["executable"]["features"]
    value["executable"]["irVersion"] = "weave/ir-v1alpha3"
    value["digest"] = canonical_digest(value["executable"])
    identifier = uuid4()
    env.versions[identifier] = {
        "id": identifier,
        "kind": "Workflow",
        "name": document["metadata"]["name"],
        "version": document["metadata"]["version"],
        "digest": value["digest"],
        "document": document,
        "artifact": value,
        "retired": False,
    }
    return identifier, value["digest"]


async def test_activation_refuses_an_action_step_that_runs_the_turn_task_even_without_a_policy(env):
    use_policy(env, None)
    version = stray_turn_version(env)

    with pytest.raises(CatalogError) as refused:
        await activate(env, version=version, slots={"ai": env.ai.id})

    assert (refused.value.status, refused.value.code) == (422, "WV-ACTIVATION-AI-RESERVED")
    assert "insert" not in env.trace and not inserts(env) and not env.activations and not env.bound
    assert not env.audits and not env.events and not env.saved


def write_policy(path, models, *, url=ENDPOINT, credential="required"):
    entry = {
        "id": "models",
        "label": "Models",
        "url": url,
        "providers": ["openai-chat"],
        "credential": credential,
        "models": models,
    }
    path.write_bytes(render([entry]))
    path.chmod(0o600)


async def test_policy_file_changes_apply_to_the_next_activation_without_restart(env, tmp_path):
    path = tmp_path / "ai-policy.json"
    write_policy(path, ["chosen", "other"])
    env.definitions.ai_policy = APIAIPolicy(path)
    await activate(env, "first")
    write_policy(path, ["chosen"])
    with pytest.raises(CatalogError) as refused:
        await activate(env, "second", expected_revision=1)
    assert refused.value.code == "WV-ACTIVATION-AI-MODEL"
    path.write_text('{"version": 2, "endpoints": ["unreadable"')
    with pytest.raises(CatalogError) as unavailable:
        await activate(env, "third", expected_revision=1)
    assert (unavailable.value.status, unavailable.value.code) == (503, "WV-AI-POLICY")
    assert "unreadable" not in str(unavailable.value)
    write_policy(path, ["chosen", "other", "third"])
    await activate(env, "fourth", expected_revision=1)
    assert [row["revision"] for row in env.activations] == [1, 2]
    # Each admitted activation writes its revision, two slot bindings and its replay record; refusals write nothing.
    assert len(inserts(env)) == 2 * 4


async def test_served_policy_admits_a_model_that_is_not_installed_without_discovery(env, tmp_path, monkeypatch):
    def consulted(*args, **kwargs):
        raise AssertionError("Activation consulted model discovery")

    monkeypatch.setattr(AIModelCache, "get", consulted)
    monkeypatch.setattr(LumiGatewayClient, "models", consulted)
    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin=OLLAMA, purpose="model", networks=("10.246.27.0/24",), credentials="none")]
    )
    path = tmp_path / "ai-policy.json"
    write_policy(path, "served", url=OLLAMA + "/v1", credential="none")
    env.definitions.ai_policy = APIAIPolicy(path, origins)
    env.ai = revision(env, "ai", url=OLLAMA + "/v1", handle="no-credential")
    env.backup = revision(env, "backup", url=OLLAMA + "/v1", handle="no-credential")
    with po.installed(origins):
        activation = await activate(env)
    assert [row["id"] for row in env.activations] == [activation.id]


# Service wiring: production and scanned service graphs share one policy owner.


async def test_application_activation_uses_the_registered_policy_owner():
    from firefly_weave.app import make_app
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.settings import Settings

    with po.installed(po.PrivateOrigins.empty()):
        app = make_app(Settings(database_url="postgresql+asyncpg://unused@127.0.0.1:1/unused"))
        try:
            context = app.state.pyfly.context
            context.container.register_instance(UnitOfWork, UnitOfWork(app.state.resources.sessions))
            assert context.get_bean(DefinitionService).ai_policy is context.get_bean(APIAIPolicy)
        finally:
            await app.state.resources.close()


def test_scanned_service_graph_resolves_activation_without_a_policy():
    from pyfly.container import Container
    from pyfly.container.scanner import scan_package
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.app import SERVICE_PACKAGES
    from firefly_weave.persistence.uow import UnitOfWork

    container = Container()
    for package in SERVICE_PACKAGES:
        scan_package(package, container)
    sessions = async_sessionmaker()
    container.register_instance(async_sessionmaker, sessions)
    container.register_instance(UnitOfWork, UnitOfWork(sessions))
    container.register_instance(ConnectorRegistry, ConnectorRegistry())
    container.register_instance(ScopedSecrets, ScopedSecrets())
    container.register_instance(VerifierSet, VerifierSet(()))
    definitions = container.resolve(DefinitionService)
    assert isinstance(definitions.ai_policy, APIAIPolicy) and definitions.ai_policy.current() is None
