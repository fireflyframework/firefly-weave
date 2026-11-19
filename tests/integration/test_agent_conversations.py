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

"""Conversation memory on the real platform and PostgreSQL: an agent loads what earlier runs for one key remembered,
the append lands in the completion's transaction, concurrent completions both land, expiry and the sweep, isolation
between environments and workflows, and test runs."""

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.agent_conversations import conversation_key_hash
from firefly_weave.contracts.agent_turns import TURN_CAPABILITY, TURN_TASK_TYPE, turn_action_definition, turn_capability
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, AgenticConnectionAdapter
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations import agent_conversations
from firefly_weave.operations.facts import RunStartFacts
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.conversations import _loaded, record_memory
from firefly_weave.runtime.deadlines import DeadlineService
from firefly_weave.runtime.models import MemoryIntent, RunState, RuntimeEvent, Transition
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("conversation_memory_enabled")]

WORKFLOW = "support-chat"


def support_chat() -> dict:
    """An agent that remembers each customer's conversation for an hour."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": WORKFLOW, "version": "1.0.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"customerId": {"type": "string"}, "message": {"type": "string"}},
                "required": ["customerId", "message"],
            },
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
                    "tools": [],
                    "memory": {
                        "scope": "conversation",
                        "key": {"ref": "/input/customerId"},
                        "window": 24,
                        "ttlSeconds": 3600,
                    },
                }
            ],
            "output": {"ref": "/steps/support/output/result"},
        },
    }


def final(answer: str) -> dict:
    return {
        "kind": "final",
        "text": "",
        "result": answer,
        "usage": {"requests": 1, "inputTokens": 90, "outputTokens": 10},
        "provider": "openai-chat",
        "model": "fixture",
        "finishReason": "stop",
    }


@pytest.fixture
async def platform(services, access_db, provisioned, monkeypatch):
    admin, scopes = provisioned
    scope = scopes[0]
    project = scope.model_copy(update={"environment_id": None})
    author_id = await access_db[2].create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "viewer", "worker", "tenant_admin", "step_tester"):
        await access_db[2].grant(admin, author_id, Grant(role=role, scope=project if role == "developer" else scope))
    author = await access_db[2].load_principal(author_id)
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
            image_digest="sha256:" + "b" * 64,
            capabilities=[turn_capability()],
            credential_capabilities=[TURN_CAPABILITY],
        ),
        context=context,
    )
    await definitions.publish(
        author, scope, "Action", json.dumps(turn_action_definition()), "json", "turn", context=context
    )
    published = await definitions.publish(
        author, scope, "Workflow", json.dumps(support_chat()), "json", "flow", context=context
    )
    activation = await definitions.activate(
        author,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={TURN_TASK_TYPE: release.id},
            connection_revision_ids={"ai": revision.id},
        ),
        "activation",
        context=context,
    )
    instance = await workers.register_instance(
        author,
        scope,
        InstanceRequest(release_id=release.id, task_types=[TURN_CAPABILITY], capacity=4),
        context=context,
    )
    return SimpleNamespace(
        db=access_db,
        graph=graph,
        scope=scope,
        other=scopes[1],
        author=author,
        context=context,
        definitions=definitions,
        runtime=graph.resolve(RuntimeService),
        tasks=graph.resolve(TaskService),
        activation=activation,
        instance=instance,
    )


async def start(platform, customer: str, message: str, *, test: bool = False):
    test_run = (
        {"start_facts": RunStartFacts(origin="test", test=True), "capability": "step.test", "test_activation": True}
        if test
        else {}
    )
    return await platform.runtime.start(
        platform.author,
        platform.scope,
        StartRunRequest(activation_id=platform.activation.id, input={"customerId": customer, "message": message}),
        "run-" + uuid4().hex,
        context=platform.context,
        **test_run,
    )


async def scan(platform) -> float:
    """One recovery scan of the environment; returns how long it took, in seconds."""
    began = time.perf_counter()
    async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
        await platform.graph.resolve(DeadlineService).scan(tx, 100)
    return time.perf_counter() - began


async def answer(platform, output: dict) -> dict:
    """Claim the one ready turn task and complete it with ``output``; returns the turn input the model read."""
    async with platform.definitions.transaction(platform.scope, None) as tx:
        [lease] = await platform.tasks.claim(
            tx, platform.instance.id, 1, actor=platform.author, scope=platform.scope, context=platform.context
        )
    async with platform.definitions.transaction(platform.scope, None) as tx:
        await platform.tasks.complete(
            tx, lease.proof, uuid4(), output, actor=platform.author, scope=platform.scope, context=platform.context
        )
    return lease.input


async def owner_rows(platform, sql: str, **values) -> list[dict]:
    async with platform.db[1]() as session:
        return [dict(row) for row in (await session.execute(text(sql), values)).mappings()]


async def conversation(platform, customer: str, workflow: str = WORKFLOW) -> dict | None:
    rows = await owner_rows(
        platform,
        "SELECT * FROM agent_conversations WHERE workflow=:workflow AND key_hash=:hash",
        workflow=workflow,
        hash=conversation_key_hash(workflow, customer),
    )
    return rows[0] if rows else None


async def converse(platform, customer: str, message: str, reply: str, *, test: bool = False):
    """One run for ``customer``: started, its conversation delivered by a scan, and its turn answered."""
    run = await start(platform, customer, message, test=test)
    await scan(platform)
    turn = await answer(platform, final(reply))
    return run, turn


async def test_a_second_run_for_one_customer_loads_the_first_runs_exchange(platform):
    first, turn = await converse(platform, "C-4", "What is the status of order A-17?", "A-17 has shipped.")
    assert [message["role"] for message in turn["messages"]] == ["user"]

    second, turn = await converse(platform, "C-4", "What order did I ask about before?", "A-17.")

    remembered = turn["messages"][0]
    assert remembered == {
        "role": "context",
        "content": {
            "memory": [
                {"role": "user", "content": "What is the status of order A-17?"},
                {"role": "assistant", "content": "A-17 has shipped."},
            ]
        },
    }
    [event] = await owner_rows(
        platform, "SELECT data FROM run_events WHERE run_id=:run AND type='memory_loaded'", run=second.id
    )
    assert event["data"]["version"] == 1 and event["data"]["key_hash"] == conversation_key_hash(WORKFLOW, "C-4")
    stored = await conversation(platform, "C-4")
    assert stored is not None and stored["version"] == 2 and len(stored["messages"]) == 4
    assert stored["last_run_id"] == second.id
    loads = await owner_rows(platform, "SELECT run_id,version,dropped FROM agent_memory_loads ORDER BY requested_at")
    assert [(row["run_id"], row["version"], row["dropped"]) for row in loads] == [
        (first.id, 0, False),
        (second.id, 1, False),
    ]
    # The raw key is in the run's input, under the run's retention, and nowhere conversation memory writes.
    written = await owner_rows(
        platform,
        "SELECT to_jsonb(c)::text AS row FROM agent_conversations c UNION ALL "
        "SELECT to_jsonb(l)::text FROM agent_memory_loads l UNION ALL "
        "SELECT coalesce(evidence->'issued_memory_loads','null')::text "
        "|| coalesce(evidence->'memory_receipt','null')::text FROM run_event_evidence",
    )
    assert written and all("C-4" not in item["row"] for item in written)


async def test_the_delivering_events_evidence_holds_a_receipt_for_its_load(platform):
    run, _ = await converse(platform, "C-4", "Hello", "Hi.")

    [issued] = await owner_rows(
        platform,
        "SELECT v.evidence FROM run_event_evidence v JOIN run_events e ON e.id=v.id "
        "WHERE e.run_id=:run AND e.type='started'",
        run=run.id,
    )
    [delivered] = await owner_rows(
        platform,
        "SELECT v.evidence FROM run_event_evidence v JOIN run_events e ON e.id=v.id "
        "WHERE e.run_id=:run AND e.type='memory_loaded'",
        run=run.id,
    )
    [load] = await owner_rows(platform, "SELECT id FROM agent_memory_loads WHERE run_id=:run", run=run.id)
    assert issued["evidence"]["issued_memory_loads"] == [
        {"load_id": str(load["id"]), "node_id": "support", "key_hash": conversation_key_hash(WORKFLOW, "C-4")}
    ]
    receipt = delivered["evidence"]["memory_receipt"]
    assert receipt["load"]["load_id"] == str(load["id"]) and receipt["version"] == 0 and receipt["test"] is False


async def test_a_waiting_conversation_agent_gets_its_memory_on_the_next_scan(platform):
    await start(platform, "C-4", "Hello")
    [pending] = await owner_rows(platform, "SELECT delivered_at FROM agent_memory_loads")
    assert pending["delivered_at"] is None

    took = await scan(platform)

    [delivered] = await owner_rows(platform, "SELECT delivered_at,dropped FROM agent_memory_loads")
    assert delivered["delivered_at"] is not None and delivered["dropped"] is False
    turns = await owner_rows(platform, "SELECT node_id FROM task_intents WHERE status='ready'")
    assert [row["node_id"] for row in turns] == ["support#1"]
    # Reported, not asserted: how long one delivering scan took on this database.
    print(f"conversation delivery scan: {took * 1000:.1f} ms")


async def test_an_append_lands_in_the_completions_transaction(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    before = await conversation(platform, "C-4")
    intent = MemoryIntent(
        op="append",
        node_id="support",
        workflow=WORKFLOW,
        key_hash=conversation_key_hash(WORKFLOW, "C-4"),
        messages=[{"role": "user", "content": "Again"}, {"role": "assistant", "content": "Yes."}],
        window=24,
        ttl_seconds=3600,
    )
    event = RuntimeEvent(id=uuid4(), type="task_completed", timestamp=before["updated_at"], sequence=99, data={})

    with pytest.raises(RuntimeError, match="rolled back"):
        async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
            await record_memory(
                RuntimeRepository(tx),
                before["last_run_id"],
                event,
                Transition(state=RunState(status="waiting"), commands=[intent]),
            )
            raise RuntimeError("The completion is rolled back")

    assert await conversation(platform, "C-4") == before


async def test_two_runs_that_complete_with_one_key_at_once_both_append_in_commit_order(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    first = await start(platform, "C-4", "First question")
    second = await start(platform, "C-4", "Second question")
    await scan(platform)
    async with platform.definitions.transaction(platform.scope, None) as tx:
        leases = await platform.tasks.claim(
            tx, platform.instance.id, 2, actor=platform.author, scope=platform.scope, context=platform.context
        )
    assert len(leases) == 2

    async def complete(lease, reply):
        # A completion that finds the project's operation lock taken is refused for now and retried, as a worker does.
        for _ in range(50):
            try:
                async with platform.definitions.transaction(platform.scope, None) as tx:
                    await platform.tasks.complete(
                        tx,
                        lease.proof,
                        uuid4(),
                        final(reply),
                        actor=platform.author,
                        scope=platform.scope,
                        context=platform.context,
                    )
                return
            except CatalogError as refused:
                if refused.status != 429:
                    raise
                await asyncio.sleep(0.05)
        raise AssertionError("The completion never got the operation lock")

    await asyncio.gather(complete(leases[0], "One."), complete(leases[1], "Two."))

    stored = await conversation(platform, "C-4")
    assert stored["version"] == 3 and len(stored["messages"]) == 6
    prompts = [message["content"] for message in stored["messages"] if message["role"] == "user"]
    assert prompts[0] == "Hello" and sorted(prompts[1:]) == ["First question", "Second question"]
    assert stored["last_run_id"] in {first.id, second.id}


async def test_an_expired_conversation_loads_empty_and_the_next_append_starts_it_over(platform):
    await converse(platform, "C-4", "Old question", "Old answer.")
    async with platform.db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE agent_conversations SET updated_at=updated_at-interval '2 hours',"
                "expires_at=clock_timestamp()-interval '1 second'"
            )
        )

    run, turn = await converse(platform, "C-4", "New question", "New answer.")

    assert [message["role"] for message in turn["messages"]] == ["user"]
    [event] = await owner_rows(
        platform, "SELECT data FROM run_events WHERE run_id=:run AND type='memory_loaded'", run=run.id
    )
    assert event["data"]["expired"] is True and event["data"]["messages"] == []
    stored = await conversation(platform, "C-4")
    assert stored["version"] == 2 and [m["content"] for m in stored["messages"]] == ["New question", "New answer."]


async def insert_expired(platform, count: int) -> None:
    async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
        await tx.session.execute(
            text(
                "INSERT INTO agent_conversations(tenant_id,project_id,environment_id,workflow,key_hash,messages,"
                "version,last_run_id,created_at,updated_at,expires_at) SELECT :tenant,:project,:environment,"
                ":workflow,encode(sha256(convert_to('k'||n,'UTF8')),'hex'),'[]'::jsonb,1,:run,"
                "now()-interval '2 days',now()-interval '2 days',now()-interval '1 day'+n*interval '1 millisecond' "
                "FROM generate_series(1,:count) n"
            ),
            {
                "tenant": platform.scope.tenant_id,
                "project": platform.scope.project_id,
                "environment": platform.scope.environment_id,
                "workflow": WORKFLOW,
                "run": uuid4(),
                "count": count,
            },
        )


async def test_the_sweep_deletes_at_most_500_expired_rows_per_pass_and_never_a_live_one(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    await insert_expired(platform, 501)

    swept = []
    for _ in range(3):
        async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
            swept.append(await agent_conversations.sweep(tx, await RuntimeRepository(tx).now()))

    assert swept == [500, 1, 0]
    left = await owner_rows(platform, "SELECT workflow,key_hash FROM agent_conversations")
    assert left == [{"workflow": WORKFLOW, "key_hash": conversation_key_hash(WORKFLOW, "C-4")}]


async def test_the_sweep_keeps_a_conversation_an_append_refreshed(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    async with platform.db[1].begin() as session:
        await session.execute(
            text("UPDATE agent_conversations SET updated_at=now()-interval '2 days',expires_at=now()-interval '1 day'")
        )
    # An append refreshes it before the sweep reaches it.
    await converse(platform, "C-4", "Again", "Yes.")

    async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
        assert await agent_conversations.sweep(tx, await RuntimeRepository(tx).now()) == 0
    assert (await conversation(platform, "C-4"))["version"] == 2


async def test_another_environment_never_sees_a_conversation(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    row = {
        "node_id": "support",
        "workflow": WORKFLOW,
        "key_hash": conversation_key_hash(WORKFLOW, "C-4"),
        "test": False,
    }

    async with UnitOfWork(platform.db[0]).open(platform.other) as tx:
        repository = RuntimeRepository(tx)
        loaded = await _loaded(repository, row, await repository.now())
        listed = (await tx.session.execute(text("SELECT count(*) FROM agent_conversations"))).scalar()

    assert (loaded["version"], loaded["messages"], listed) == (0, [], 0)


async def test_another_workflow_with_the_same_key_has_its_own_conversation(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    other = conversation_key_hash("billing-chat", "C-4")
    intent = MemoryIntent(
        op="append",
        node_id="billing",
        workflow="billing-chat",
        key_hash=other,
        messages=[{"role": "user", "content": "Bill?"}, {"role": "assistant", "content": "Paid."}],
        window=24,
        ttl_seconds=3600,
    )
    stored = await conversation(platform, "C-4")
    event = RuntimeEvent(id=uuid4(), type="task_completed", timestamp=stored["updated_at"], sequence=99, data={})
    async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
        await record_memory(
            RuntimeRepository(tx),
            stored["last_run_id"],
            event,
            Transition(state=RunState(status="waiting"), commands=[intent]),
        )

    assert (await conversation(platform, "C-4"))["version"] == 1
    billing = await conversation(platform, "C-4", "billing-chat")
    assert billing is not None and billing["key_hash"] == other != stored["key_hash"]
    assert [m["content"] for m in billing["messages"]] == ["Bill?", "Paid."]


async def test_a_test_run_neither_loads_nor_remembers(platform):
    await converse(platform, "C-4", "Hello", "Hi.")
    before = await conversation(platform, "C-4")
    async with platform.db[1].begin() as session:
        values = {
            "tenant": platform.scope.tenant_id,
            "project": platform.scope.project_id,
            "environment": platform.scope.environment_id,
            "id": platform.activation.id,
            "principal": platform.author.id,
            "draft": uuid4(),
        }
        # The activation becomes a live draft test activation, as Studio's draft test creates one.
        await session.execute(
            text("UPDATE activation_revisions SET test=true WHERE id=:id"), {"id": platform.activation.id}
        )
        await session.execute(
            text(
                "INSERT INTO test_activation_details(tenant_id,project_id,environment_id,activation_id,principal_id,"
                "source,draft_id,draft_revision,side_effect,created_at,expires_at) VALUES(:tenant,:project,"
                ":environment,:id,:principal,'draft',:draft,1,'read_only',now(),now()+interval '1 hour')"
            ),
            values,
        )
        # The environment allows test calls, as its test policy must for any test activation to run.
        await session.execute(
            text(
                "INSERT INTO environment_test_policies(tenant_id,project_id,environment_id,test_calls,revision,"
                "updated_at,updated_by) VALUES(:tenant,:project,:environment,'enabled',1,now(),:principal)"
            ),
            values,
        )

    run, turn = await converse(platform, "C-4", "Test question", "Test answer.", test=True)

    assert [message["role"] for message in turn["messages"]] == ["user"]
    [event] = await owner_rows(
        platform, "SELECT data FROM run_events WHERE run_id=:run AND type='memory_loaded'", run=run.id
    )
    assert event["data"]["test"] is True and event["data"]["messages"] == [] and event["data"]["version"] == 0
    [load] = await owner_rows(platform, "SELECT test FROM agent_memory_loads WHERE run_id=:run", run=run.id)
    assert load["test"] is True
    assert await conversation(platform, "C-4") == before

    # The store refuses a test run's append too, even if one were asked for.
    intent = MemoryIntent(
        op="append",
        node_id="support",
        workflow=WORKFLOW,
        key_hash=before["key_hash"],
        messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        window=24,
        ttl_seconds=3600,
    )
    event = RuntimeEvent(id=uuid4(), type="task_completed", timestamp=before["updated_at"], sequence=99, data={})
    async with UnitOfWork(platform.db[0]).open(platform.scope) as tx:
        await record_memory(
            RuntimeRepository(tx), run.id, event, Transition(state=RunState(status="waiting"), commands=[intent])
        )
    assert await conversation(platform, "C-4") == before
