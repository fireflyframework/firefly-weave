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

"""An AI agent on the real platform: its reviews as human tasks, a changing call held in doubt, and its evidence."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import TaskCapability
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.agent_turns import TURN_CAPABILITY, TURN_TASK_TYPE, turn_action_definition, turn_capability
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, AgenticConnectionAdapter
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.human_tasks import (
    AssignmentBindingRequest,
    CompleteHumanTask,
    HumanTaskCommand,
    ReassignHumanTask,
)
from firefly_weave.contracts.operations import IncidentResolution
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.human_tasks.service import HumanTaskService
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.deadlines import DeadlineService
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

pytestmark = pytest.mark.integration

CANCEL_TASK = "orders.cancel@1.0.0"
REVIEW = "support#1.1.review"
ORDER = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}
STATUS = {"type": "object", "properties": {"status": {"type": "string"}}}


def cancel_capability() -> TaskCapability:
    return TaskCapability(
        taskType="orders.cancel",
        taskVersion="1.0.0",
        inputSchema=ORDER,
        outputSchema=STATUS,
        sideEffect="non_idempotent",
        timeoutSeconds=60,
    )


def cancel_action() -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "orders-cancel", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "orders.cancel", "taskVersion": "1.0.0"},
            "inputSchema": ORDER,
            "outputSchema": STATUS,
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 60,
        },
    }


def support_desk(review: dict) -> dict:
    """An agent that may cancel an order once a reviewer other than the run's starter approves the call."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "support-desk", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
            "outputSchema": {},
            "timeoutSeconds": 3600,
            "llmProfiles": {
                "support": {
                    "provider": "openai-chat",
                    "model": "fixture",
                    "options": {"max_tokens": 256},
                    "outputSchema": {},
                }
            },
            "connections": {"ai": {"connector": CONNECTOR_REFERENCE}},
            "steps": [
                {
                    "id": "support",
                    "kind": "agent",
                    "uses": "weave-agentic-turn@1.0.0",
                    "profile": "support",
                    "connection": "ai",
                    "instructions": {"literal": "You answer order questions."},
                    "prompt": {"ref": "/input/message"},
                    "tools": [
                        {
                            "name": "cancel_order",
                            "description": "Cancel an order.",
                            "action": "orders-cancel@1.0.0",
                            "review": review,
                        }
                    ],
                    "limits": {"timeoutSeconds": 900},
                    "onError": "continue",
                }
            ],
            "output": {"ref": "/steps/support/error/code"},
        },
    }


def usage() -> dict:
    return {"requests": 1, "inputTokens": 90, "outputTokens": 10}


def cancel_turn() -> dict:
    return {
        "kind": "tool_calls",
        "text": "",
        "toolCalls": [{"id": "t1c1", "name": "cancel_order", "arguments": {"id": "A-17"}}],
        "usage": usage(),
        "provider": "openai-chat",
        "model": "fixture",
        "finishReason": "tool_calls",
    }


@pytest.fixture
def review_expiry():
    return 86400


@pytest.fixture
async def agent(services, access_db, provisioned, monkeypatch, review_expiry):
    admin, scopes = provisioned
    scope = scopes[0]
    project = scope.model_copy(update={"environment_id": None})

    async def principal(kind: str, roles: tuple[str, ...]):
        identifier = await access_db[2].create_principal(admin, kind)
        for role in roles:
            await access_db[2].grant(
                admin, identifier, Grant(role=role, scope=project if role == "developer" else scope)
            )
        return await access_db[2].load_principal(identifier)

    author = await principal("application", ("developer", "deployer", "operator", "viewer", "worker", "tenant_admin"))
    # The run's starter also manages human tasks: a distinct review shuts them out all the same.
    starter = await principal("human", ("operator", "viewer", "task_participant", "task_manager"))
    reviewer = await principal("human", ("viewer", "task_participant", "task_manager"))
    context = AuditContext()
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, AgenticConnectionAdapter())
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_TEST"),)
    )
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "private-agent-provider-key")
    graph = services(access_db[0], registry=registry, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    workers = graph.resolve(WorkerService)
    humans = graph.resolve(HumanTaskService)
    connector = await definitions.publish(
        author, scope, "Connector", json.dumps(AGENTIC_DESCRIPTOR.manifest.value), "json", "connector", context=context
    )
    revision = await graph.resolve(ConnectionService).create_revision(
        author,
        scope,
        ConnectionRequest(
            name="models",
            connector_version_id=connector.id,
            config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
            secretRef={"apiKey": "approved"},
            allowed_destinations=("https://api.openai.com",),
        ),
        context=context,
    )
    release = await workers.register_release(
        author,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "a" * 64,
            capabilities=[turn_capability(), cancel_capability()],
            credential_capabilities=[TURN_CAPABILITY],
        ),
        context=context,
    )
    for key, document in (("turn", turn_action_definition()), ("cancel", cancel_action())):
        await definitions.publish(author, scope, "Action", json.dumps(document), "json", key, context=context)
    binding = await humans.put_binding(
        reviewer,
        scope,
        AssignmentBindingRequest(name="support-leads", principal_ids=[starter.id, reviewer.id]),
        "support-leads",
        context=context,
    )
    review = {"assignment": "support-leads", "distinctReviewer": True, "expirySeconds": review_expiry}
    published = await definitions.publish(
        author, scope, "Workflow", json.dumps(support_desk(review)), "json", "flow", context=context
    )
    activation = await definitions.activate(
        author,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={TURN_TASK_TYPE: release.id, "orders.cancel": release.id},
            connection_revision_ids={"ai": revision.id},
            assignment_binding_ids={"support-leads": binding.binding_id},
        ),
        "activation",
        context=context,
    )
    run = await graph.resolve(RuntimeService).start(
        starter,
        scope,
        StartRunRequest(activation_id=activation.id, input={"message": "Cancel A-17"}),
        "run",
        context=context,
    )
    instance = await workers.register_instance(
        author,
        scope,
        InstanceRequest(release_id=release.id, task_types=[TURN_CAPABILITY, CANCEL_TASK], capacity=4),
        context=context,
    )
    setup = SimpleNamespace(
        db=access_db,
        graph=graph,
        scope=scope,
        definitions=definitions,
        tasks=graph.resolve(TaskService),
        humans=humans,
        incidents=graph.resolve(IncidentService),
        starter=starter,
        reviewer=reviewer,
        worker=dict(actor=author, scope=scope, context=context),
        context=context,
        run=run,
        instance=instance,
    )
    # The model asks to cancel the order: its call waits for a review.
    lease = await claim(setup)
    assert lease.capability == TURN_CAPABILITY
    setup.turn_input = lease.input
    await complete(setup, lease, cancel_turn())
    return setup


async def claim(setup):
    async with setup.definitions.transaction(setup.scope, None) as tx:
        return (await setup.tasks.claim(tx, setup.instance.id, 1, **setup.worker))[0]


async def complete(setup, lease, output):
    async with setup.definitions.transaction(setup.scope, None) as tx:
        await setup.tasks.complete(tx, lease.proof, uuid4(), output, **setup.worker)


async def rows(setup, sql, **values):
    async with setup.db[1]() as session:
        return [dict(row) for row in (await session.execute(text(sql), {"run": setup.run.id, **values})).mappings()]


async def review_task(setup) -> dict:
    [row] = await rows(setup, "SELECT * FROM human_tasks WHERE run_id=:run AND node_id=:node", node=REVIEW)
    return row


async def command(setup, actor, task, name, request, key):
    return await setup.humans.command(actor, setup.scope, task["id"], name, request, key, context=setup.context)


async def approve(setup) -> dict:
    task = await review_task(setup)
    await command(setup, setup.reviewer, task, "claim", HumanTaskCommand(expected_revision=1), "claim")
    await command(
        setup,
        setup.reviewer,
        task,
        "complete",
        CompleteHumanTask(expected_revision=2, decision="approve", data={}),
        "complete",
    )
    return task


async def tick(setup, node: str) -> None:
    """The deadline at ``node`` is due now: its row and the run's own record of it move to the same past instant, as
    if that much time had passed, and the deadline service fires it."""
    async with setup.db[1].begin() as session:
        due = await session.scalar(text("SELECT clock_timestamp()-interval '1 second'"))
        await session.execute(
            text("UPDATE run_deadlines SET deadline=:due WHERE run_id=:run AND node_id=:node"),
            {"run": setup.run.id, "node": node, "due": due},
        )
        await session.execute(
            text(
                "UPDATE runs SET state=jsonb_set(state, ARRAY['waits', cast(:node AS text)], "
                "to_jsonb(cast(:due AS text))) WHERE id=:run"
            ),
            {"run": setup.run.id, "node": node, "due": due.isoformat()},
        )
    async with UnitOfWork(setup.db[0]).open(setup.scope) as tx:
        assert await setup.graph.resolve(DeadlineService).tick(tx, 100) == 1


async def test_the_runs_starter_cannot_see_or_answer_a_distinct_review(agent):
    task = await review_task(agent)

    for attempt in (
        lambda: agent.humans.read(agent.starter, agent.scope, task["id"], context=agent.context),
        lambda: command(agent, agent.starter, task, "claim", HumanTaskCommand(expected_revision=1), "claim"),
        lambda: command(
            agent,
            agent.starter,
            task,
            "complete",
            CompleteHumanTask(expected_revision=1, decision="approve", data={}),
            "complete",
        ),
        # Managing human tasks doesn't reopen the door: reassigning it to someone else is refused too.
        lambda: command(
            agent,
            agent.starter,
            task,
            "reassign",
            ReassignHumanTask(expected_revision=1, principal_id=agent.reviewer.id, reason="Please review"),
            "reassign",
        ),
    ):
        with pytest.raises(CatalogError) as refused:
            await attempt()
        assert (refused.value.status, refused.value.code) == (403, "WV-REVIEW-DISTINCT")
    listed = await agent.humans.list(agent.starter, agent.scope, context=agent.context)
    assert str(task["id"]) not in {item["id"] for item in listed["items"]}
    seen = await agent.humans.list(agent.reviewer, agent.scope, context=agent.context)
    assert str(task["id"]) in {item["id"] for item in seen["items"]}


async def test_an_approval_dispatches_the_stored_call_with_the_digest_from_the_task_row(agent):
    task = await approve(agent)

    [event] = await rows(agent, "SELECT data FROM run_events WHERE run_id=:run AND type='human_completed'")
    assert event["data"]["input_digest"] == task["context"]["inputDigest"]
    # The digest of the stored input, as the review showed it: sha256 over its canonical JSON.
    assert event["data"]["input_digest"] == "sha256:" + canonical_digest({"id": "A-17"})
    lease = await claim(agent)
    assert (lease.capability, lease.input) == (CANCEL_TASK, {"id": "A-17"})


@pytest.mark.parametrize("review_expiry", [300])
async def test_an_expired_review_closes_as_expired(agent):
    await tick(agent, REVIEW)

    task = await review_task(agent)
    assert task["status"] == "expired"
    # The run goes on: the model hears the review expired, in a second turn.
    [run] = await rows(agent, "SELECT state->>'status' AS status FROM runs WHERE id=:run")
    turns = await rows(agent, "SELECT node_id FROM task_intents WHERE run_id=:run AND node_id='support#2'")
    assert run["status"] == "waiting" and turns == [{"node_id": "support#2"}]
    audit = await rows(
        agent, "SELECT action FROM human_task_audit WHERE task_id=:task ORDER BY revision", task=task["id"]
    )
    assert [row["action"] for row in audit] == ["created", "expired"]


async def held(agent):
    """The approved cancel is running when the agent's own deadline passes."""
    await approve(agent)
    lease = await claim(agent)
    assert lease.capability == CANCEL_TASK
    await tick(agent, "support")
    return lease


async def test_a_held_calls_lease_is_revoked_and_its_task_waits_at_its_incident(agent):
    await held(agent)

    [task] = await rows(agent, "SELECT id,status FROM task_intents WHERE run_id=:run AND node_id='support#1.1'")
    assert task["status"] == "incident"
    leases = await rows(agent, "SELECT status FROM task_leases WHERE task_id=:task", task=task["id"])
    assert leases and all(row["status"] in {"revoked", "control_revoked"} for row in leases)
    incidents = await rows(agent, "SELECT node_id,code,status FROM incidents WHERE run_id=:run")
    assert incidents == [{"node_id": "support#1.1", "code": "WV-TASK-AMBIGUOUS", "status": "active"}]


async def test_once_the_held_call_is_reconciled_the_agents_failure_goes_on(agent):
    await held(agent)
    [incident] = await rows(agent, "SELECT id FROM incidents WHERE run_id=:run AND status='active'")

    async with agent.definitions.transaction(agent.scope, None) as tx:
        await agent.incidents.resolve(
            tx,
            incident["id"],
            IncidentResolution(
                kind="accept_reconciled_result",
                receipt_id=uuid4(),
                output={"status": "cancelled"},
                reason="The order system shows A-17 cancelled",
                evidence_reference="ticket-17",
            ),
            1,
            **agent.worker,
        )

    [run] = await rows(agent, "SELECT state FROM runs WHERE id=:run")
    assert (run["state"]["status"], run["state"]["output"]) == ("succeeded", "WV-AGENT-TIMEOUT")
