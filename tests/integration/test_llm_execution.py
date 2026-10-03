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

"""Canonical model tasks cross the real activation, lease and replay boundaries."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_secret_admission import assert_absent

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.compiler.canonical import canonical_digest
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
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.operations import IncidentResolution
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.workers import CredentialGrantRequest, CredentialRequest, InstanceRequest, ReleaseRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

pytestmark = pytest.mark.integration


@pytest.fixture
async def llm_task(services, access_db, provisioned, monkeypatch, request):
    admin, scopes = provisioned
    scope = scopes[0]
    actor_id = await access_db[2].create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "viewer", "worker", "tenant_admin"):
        await access_db[2].grant(
            admin,
            actor_id,
            Grant(role=role, scope=scope.model_copy(update={"environment_id": None}) if role == "developer" else scope),
        )
    actor = await access_db[2].load_principal(actor_id)
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_TEST"),)
    )
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "private-llm-provider-key")
    graph = services(access_db[0], registry=registry, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    connections = graph.resolve(ConnectionService)
    workers = graph.resolve(WorkerService)
    tasks = graph.resolve(TaskService)
    connector = await definitions.publish(
        actor,
        scope,
        "Connector",
        json.dumps(AGENTIC_DESCRIPTOR.manifest.value),
        "json",
        "connector",
        context=AuditContext(),
    )
    revision = await connections.create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="models",
            connector_version_id=connector.id,
            config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
            secretRef={"apiKey": "approved"},
            allowed_destinations=("https://api.openai.com",),
        ),
        context=AuditContext(),
    )
    capability = f"{TASK_TYPE}@{TASK_VERSION}"
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "a" * 64, capabilities=[task_capability()], credential_capabilities=[capability]
        ),
        context=AuditContext(),
    )
    await definitions.publish(
        actor, scope, "Action", json.dumps(action_definition()), "json", "action", context=AuditContext()
    )
    shared_context = getattr(request, "param", None) == "shared-context"
    result_schema = {"type": "object", "properties": {"marker": {"type": "string"}}, "required": ["marker"]}
    profile = {
        "provider": "openai-chat",
        "model": "fixture",
        "options": {"max_tokens": 256},
        "maxCalls": 2,
        "timeoutSeconds": 10,
        "outputSchema": result_schema if shared_context else getattr(request, "param", {"type": "boolean"}),
    }
    source = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "model-flow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "outputSchema": {"type": "boolean"},
            "timeoutSeconds": 120,
            "llmProfiles": {"assistant": profile},
            "connections": {"provider": {"connector": CONNECTOR_REFERENCE}},
            "steps": [
                {
                    "id": "ask",
                    "kind": "llm",
                    "uses": "weave-agentic-generate@1.0.0",
                    "profile": "assistant",
                    "prompt": {"ref": "/input/text"},
                    "context": {"literal": {}},
                    "connection": "provider",
                }
            ],
            "output": {"literal": True} if hasattr(request, "param") else {"ref": "/steps/ask/output/result"},
        },
    }
    if shared_context:
        source["spec"]["outputSchema"] = result_schema
        source["spec"]["steps"].extend(
            [
                {"id": "pause", "kind": "signal", "name": "continue", "timeoutSeconds": 60, "payloadSchema": {}},
                {
                    **source["spec"]["steps"][0],
                    "id": "followup",
                    "context": {"ref": "/steps/ask/output/result"},
                },
                {**source["spec"]["steps"][0], "id": "independent"},
            ]
        )
        source["spec"]["output"] = {"ref": "/steps/followup/output/result"}
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(source), "json", "flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={TASK_TYPE: release.id},
            connection_revision_ids={"provider": revision.id},
        ),
        "activation",
        context=AuditContext(),
    )
    run = await graph.resolve(RuntimeService).start(
        actor,
        scope,
        StartRunRequest(activation_id=activation.id, input={"text": "Return true"}),
        "run",
        context=AuditContext(),
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=[capability], capacity=1),
        context=AuditContext(),
    )
    async with definitions.transaction(scope, None) as tx:
        lease = (await tasks.claim(tx, instance.id, 1, **authority))[0]
    return SimpleNamespace(
        definitions=definitions,
        tasks=tasks,
        workers=workers,
        history=graph.resolve(HistoryService),
        incidents=graph.resolve(IncidentService),
        authority=authority,
        revision=revision,
        run=run,
        lease=lease,
        release=release,
        capability=capability,
        profile=profile,
        activation=activation,
        instance=instance,
        reload=lambda: services(access_db[0], registry=registry, secrets=secrets),
    )


def valid_output():
    return {
        "result": True,
        "provider": "openai-chat",
        "model": "fixture",
        "usage": {"requests": 1, "inputTokens": 10, "outputTokens": 2},
    }


async def test_pinned_model_task_credentials_completion_and_replay(llm_task, access_db):
    setup = llm_task
    assert setup.lease.capability == setup.capability
    assert setup.lease.input["profile"]["model"] == setup.profile["model"]
    assert setup.lease.input["profile"]["maxCalls"] == 2
    assert setup.lease.input["prompt"] == "Return true"
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        context = await setup.tasks.context(tx, setup.lease.proof, **setup.authority)
    assert context.connection.revision_id == setup.revision.id
    assert context.connection.connector == CONNECTOR_REFERENCE
    assert context.connection.secret_slots == ["apiKey"]
    credential_request = CredentialRequest(
        lease=setup.lease.proof, connection_revision_id=setup.revision.id, slot="apiKey"
    )
    with pytest.raises(AccessDenied):
        await setup.tasks.credentials(credential_request, **setup.authority)
    await setup.workers.grant_connection(
        setup.authority["actor"],
        setup.authority["scope"],
        CredentialGrantRequest(
            release_id=setup.release.id, connection_revision_id=setup.revision.id, capability=setup.capability
        ),
        context=AuditContext(),
    )
    assert (await setup.tasks.credentials(credential_request, **setup.authority)).value == "private-llm-provider-key"
    completion_id = uuid4()
    for _ in range(2):
        async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
            receipt = await setup.tasks.complete(
                tx, setup.lease.proof, completion_id, valid_output(), **setup.authority
            )
        assert receipt.status == "completed"
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        exported = await setup.history.export(tx, setup.run.id, **setup.authority)
    assert exported.replay.status == "consistent"
    assert exported.replay.final_state.status == "succeeded"
    assert exported.replay.final_state.output is True
    assert len(exported.events) == 2
    assert (
        "private-llm-provider-key"
        not in exported.model_dump_json() + context.model_dump_json() + setup.lease.model_dump_json()
    )
    async with access_db[1]() as tx:
        for table, column in (("run_events", "data"), ("task_intents", "payload"), ("completion_receipts", "payload")):
            assert "private-llm-provider-key" not in str(
                (await tx.execute(text(f"SELECT {column} FROM {table}"))).all()
            )


@pytest.mark.parametrize("invalid", ["result", "provider", "model", "usage"])
async def test_profile_output_guard_is_durable_and_replayable(llm_task, invalid):
    setup = llm_task
    output = valid_output()
    if invalid == "result":
        output["result"] = "wrong type"
    elif invalid == "usage":
        output["usage"]["requests"] = 3
    else:
        output[invalid] = "anthropic" if invalid == "provider" else "other-model"
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        await setup.tasks.complete(tx, setup.lease.proof, uuid4(), output, **setup.authority)
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        exported = await setup.history.export(tx, setup.run.id, **setup.authority)
    assert exported.replay.status == "incomplete"
    assert exported.replay.last_verified_sequence == 2
    assert exported.replay.diagnostics[0].code == "WV-REPLAY-OPEN-PREFIX"
    assert exported.replay.final_state.status == "suspended"
    assert exported.replay.final_state.steps == {}


@pytest.mark.parametrize(
    "llm_task",
    [
        {"type": "string", "x-secret": True},
        {"$defs": {"private": {"type": "string", "writeOnly": True}}, "$ref": "#/$defs/private"},
    ],
    indirect=True,
)
async def test_model_profile_secret_output_cannot_enter_durable_history(llm_task, access_db):
    setup = llm_task
    output = valid_output()
    output["result"] = "private-model-result-canary"
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        receipt = await setup.tasks.complete(tx, setup.lease.proof, uuid4(), output, **setup.authority)
    assert receipt.status == "rejected" and receipt.accepted_output_hash is None
    assert receipt.reason_code == "WV-SCHEMA-SECRET_VALUE"
    async with access_db[1]() as tx:
        incident_id = await tx.scalar(text("SELECT id FROM incidents WHERE run_id=:run"), {"run": setup.run.id})
    with pytest.raises(CatalogError, match="Reconciled output violates"):
        async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
            await setup.incidents.resolve(
                tx,
                incident_id,
                IncidentResolution(
                    kind="accept_reconciled_result",
                    receipt_id=uuid4(),
                    output=output,
                    reason="Reviewed",
                    evidence_reference="operator-review",
                ),
                1,
                **setup.authority,
            )
    await assert_absent(
        access_db[1],
        "private-model-result-canary",
        canonical_digest(output),
        canonical_digest({"status": "completed", "output": output}),
    )
    async with setup.definitions.transaction(setup.authority["scope"], None) as tx:
        exported = await setup.history.export(tx, setup.run.id, **setup.authority)
    assert exported.events[-1].type == "incident_opened"
    assert exported.events[-1].data["code"] == "WV-SCHEMA-SECRET_VALUE"


@pytest.mark.parametrize("llm_task", ["shared-context"], indirect=True)
async def test_explicit_ai_context_survives_wait_reload_and_completion_retry_without_cross_run_memory(llm_task):
    setup = llm_task
    actor, scope = setup.authority["actor"], setup.authority["scope"]
    first_result = {"marker": "first-execution"}
    second_result = {"marker": "second-execution"}
    first_completion = uuid4()
    async with setup.definitions.transaction(scope, None) as tx:
        await setup.tasks.complete(
            tx, setup.lease.proof, first_completion, {**valid_output(), "result": first_result}, **setup.authority
        )
    graph = setup.reload()
    runtime = graph.resolve(RuntimeService)
    second = await runtime.start(
        actor,
        scope,
        StartRunRequest(activation_id=setup.activation.id, input={"text": "Second execution"}),
        "second-context-run",
        context=AuditContext(),
    )
    async with setup.definitions.transaction(scope, None) as tx:
        second_lease = (await graph.resolve(TaskService).claim(tx, setup.instance.id, 1, **setup.authority))[0]
        assert second_lease.input["context"] == {}
        assert second_lease.input["prompt"] == "Second execution"
        await graph.resolve(TaskService).complete(
            tx, second_lease.proof, uuid4(), {**valid_output(), "result": second_result}, **setup.authority
        )
    for run, expected, prompt in (
        (setup.run, first_result, "Return true"),
        (second, second_result, "Second execution"),
    ):
        graph = setup.reload()
        waiting = await graph.resolve(RuntimeService).read(actor, scope, run.id, context=AuditContext())
        assert waiting.state.status == "waiting"
        assert waiting.state.active == ["pause"]
        assert waiting.state.steps["ask"]["output"]["result"] == expected
        async with setup.definitions.transaction(scope, None) as tx:
            await graph.resolve(SignalService).deliver(tx, run.id, "resume", "continue", {}, **setup.authority)
        graph = setup.reload()
        tasks = graph.resolve(TaskService)
        async with setup.definitions.transaction(scope, None) as tx:
            followup = (await tasks.claim(tx, setup.instance.id, 1, **setup.authority))[0]
        assert followup.input["context"] == expected
        assert followup.input["prompt"] == prompt
        completion = uuid4()
        result = {**valid_output(), "result": expected}
        with pytest.raises(RuntimeError, match="completion transaction rolled back"):
            async with setup.definitions.transaction(scope, None) as tx:
                await tasks.complete(tx, followup.proof, completion, result, **setup.authority)
                raise RuntimeError("completion transaction rolled back")
        graph = setup.reload()
        tasks = graph.resolve(TaskService)
        async with setup.definitions.transaction(scope, None) as tx:
            context = await tasks.heartbeat(tx, followup.proof, **setup.authority)
            assert context.input == followup.input
            assert context.input["context"] == expected
        for _ in range(2):
            async with setup.definitions.transaction(scope, None) as tx:
                acknowledgment = await tasks.complete(tx, followup.proof, completion, result, **setup.authority)
                assert acknowledgment.status == "completed"
        async with setup.definitions.transaction(scope, None) as tx:
            independent = (await tasks.claim(tx, setup.instance.id, 1, **setup.authority))[0]
            assert independent.input["context"] == {}
            assert independent.input["prompt"] == prompt
            await tasks.complete(tx, independent.proof, uuid4(), result, **setup.authority)
        graph = setup.reload()
        async with setup.definitions.transaction(scope, None) as tx:
            exported = await graph.resolve(HistoryService).export(tx, run.id, **setup.authority)
        assert exported.replay.status == "consistent"
        assert exported.replay.final_state.status == "succeeded"
        assert exported.replay.final_state.output == expected
        assert len(exported.events) == 5
