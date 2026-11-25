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
"""An AI task's step test checks the AI policy before it writes anything."""

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_step_test_run import CONTEXT, StepTests

from firefly_weave.access.models import Grant
from firefly_weave.ai_policy import AIPolicy, PolicyEndpoint
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.agentic import (
    AGENTIC_DESCRIPTOR,
    CONNECTOR_REFERENCE,
    TASK_TYPE,
    TASK_VERSION,
    AgenticConnectionAdapter,
    action_definition,
    task_capability,
)
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.workers import ReleaseRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.step_tests.service import StepTestService
from firefly_weave.workers.service import WorkerService

pytestmark = pytest.mark.integration
ENDPOINT = "https://api.openai.com/v1"
ASK = {
    "id": "ask",
    "kind": "llm",
    "uses": "weave-agentic-generate@1.0.0",
    "profile": "assistant",
    "prompt": {"literal": "Summarize the customer"},
    "context": {"literal": None},
    "connection": "provider",
}


def workflow(*steps):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "model-flow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {},
            "llmProfiles": {
                "assistant": {
                    "provider": "openai-chat",
                    "model": "fixture",
                    "options": {"max_tokens": 256},
                    "maxCalls": 2,
                    "timeoutSeconds": 10,
                    "outputSchema": {"type": "boolean"},
                }
            },
            "connections": {"provider": {"connector": CONNECTOR_REFERENCE}},
            "steps": list(steps),
            "output": {"literal": None},
        },
    }


def approving(*models):
    entry = PolicyEndpoint(
        id="models",
        label="Models",
        url=ENDPOINT,
        providers=("openai-chat",),
        credential="required",
        models=models,
    )
    return SimpleNamespace(current=lambda: AIPolicy(version=2, endpoints=(entry,)))


@pytest.fixture
async def ai_steps(services, access_db, provisioned, monkeypatch):
    """Step tests where the AI action, a model connection and a worker release for it are ready."""
    sessions, owner, access, _ = access_db
    admin, scopes = provisioned
    scope = scopes[0]
    actor_id = await access.create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "tenant_admin"):
        role_scope = scope.model_copy(update={"environment_id": None}) if role == "developer" else scope
        await access.grant(admin, actor_id, Grant(role=role, scope=role_scope))
    actor = await access.load_principal(actor_id)
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_TEST"),)
    )
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "private-llm-provider-key")
    graph = services(sessions, registry=registry, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(AGENTIC_DESCRIPTOR.manifest.value), "json", "connector", context=CONTEXT
    )
    revision = await graph.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="models",
            connector_version_id=connector.id,
            config={"provider": "openai-chat", "endpoint": ENDPOINT, "secretSlot": "apiKey"},
            secretRef={"apiKey": "approved"},
            allowed_destinations=("https://api.openai.com",),
        ),
        context=CONTEXT,
    )
    await graph.resolve(WorkerService).register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "a" * 64,
            capabilities=[task_capability()],
            credential_capabilities=[f"{TASK_TYPE}@{TASK_VERSION}"],
        ),
        context=CONTEXT,
    )
    await definitions.publish(
        actor, scope, "Action", json.dumps(action_definition()), "json", "action", context=CONTEXT
    )
    helper = StepTests(graph.resolve(StepTestService), access, admin, admin, scope, uuid4(), owner)
    tester = await access.create_principal(admin, "application")
    for role in ("developer", "step_tester", "deployer"):
        role_scope = scope.model_copy(update={"environment_id": None}) if role == "developer" else scope
        resources = (str(revision.id),) if role == "deployer" else ()
        await access.grant(admin, tester, Grant(role=role, scope=role_scope, resources=resources))
    helper.tester = await access.load_principal(tester)
    await helper.policy("enabled")

    async def start(document, value="What is it?"):
        helper.draft = uuid4()
        async with owner() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO draft_revisions(id,tenant_id,project_id,revision,document) "
                    "VALUES(:id,:tenant,:project,1,cast(:document AS jsonb))"
                ),
                {
                    "id": helper.draft,
                    "tenant": scope.tenant_id,
                    "project": scope.project_id,
                    "document": json.dumps(document),
                },
            )
        request = helper.request(
            kind="llm",
            step_id="ask",
            uses=ASK["uses"],
            profile="assistant",
            input={"prompt": value, "context": value},
            connections={"provider": str(revision.id)},
            acknowledged_side_effect="non_idempotent",
        )
        return await helper.start(helper.tester, request, key=uuid4().hex)

    async def refused(document, value="What is it?"):
        with pytest.raises(CatalogError) as raised:
            await start(document, value)
        assert await helper.nothing_written()
        assert await helper.scalar("SELECT count(*) FROM runs") == 0
        assert await helper.scalar("SELECT count(*) FROM access_audit WHERE action='step_test.start'") == 0
        return raised.value.status, raised.value.code

    return SimpleNamespace(start=start, refused=refused, tests=helper)


async def test_a_model_the_ai_policy_doesnt_approve_writes_nothing(ai_steps):
    definitions = ai_steps.tests.service.definitions
    definitions.ai_policy = approving("another")
    assert await ai_steps.refused(workflow(copy.deepcopy(ASK))) == (422, "WV-ACTIVATION-AI-MODEL")
    definitions.ai_policy = approving("fixture")
    assert (await ai_steps.start(workflow(ASK))).status == "running"
