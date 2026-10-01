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

"""Task-owned real PostgreSQL evidence for classified durable boundaries."""

import json
from uuid import uuid4

import pytest
import test_connections
import test_definitions as catalog_tests
import test_waits
from sqlalchemy import text

from firefly_weave.access.models import Grant
from firefly_weave.compiler.canonical import canonical_digest

waiting_run = test_waits.waiting_run
connections = test_connections.connections

pytestmark = pytest.mark.integration
author = catalog_tests.author
publication_request = catalog_tests.publication_request


async def assert_absent(owner, *needles, retained=None):
    tables = (
        "runs",
        "run_events",
        "step_instances",
        "task_intents",
        "mutation_idempotency",
        "completion_receipts",
        "incidents",
        "incident_resolution_receipts",
        "signal_receipts",
        "trigger_routes",
        "trigger_receipts",
        "definition_versions",
        "definition_sources",
        "draft_revisions",
        "schedule_revisions",
        "schedule_occurrences",
        "connection_revisions",
        "worker_releases",
        "task_leases",
        "run_policy_blocks",
        "wait_wakeups",
        "access_audit",
        "run_retry_links",
        "activation_revisions",
    )
    found = []
    async with owner() as session:
        for table in tables:
            rows = (await session.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t"))).scalars()
            rows = [row for row in rows if row not in (retained or {}).get(table, [])]
            if any(needle in row for row in rows for needle in needles):
                found.append(table)
    assert not found, "classified material persisted in: " + ", ".join(found)


async def test_public_marked_input_never_persists(
    author, access_db, provisioned, headers, project_url, env_url, publication_request, caplog
):
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    schema = {"type": "object", "properties": {"credential": {"x-secret": True}}}
    source = publication_request["source"].replace("inputSchema: {}", "inputSchema: " + json.dumps(schema))
    published = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "secret-flow"},
        json={"format": "yaml", "source": source},
    )
    assert published.status_code == 201
    activated = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "secret-activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert activated.status_code == 201
    canary = "C4a-" + str(uuid4())
    request = {"activation_id": activated.json()["id"], "input": {"credential": canary}}
    response = await author[0].post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "secret-run"}, json=request
    )
    # Assert durable evidence before HTTP status so RED proves the actual gap.
    await assert_absent(access_db[1], canary, canonical_digest(request), canonical_digest(canary))
    assert response.status_code == 422
    schedule = await author[0].post(env_url + "/schedules", headers=headers, json={"cron": "* * * * *", **request})
    assert schedule.status_code == 422
    await assert_absent(access_db[1], canary, canonical_digest(request))
    assert canary not in response.text and canary not in caplog.text
    for value in (None, "", {}, []):
        request["input"] = {"credential": value}
        response = await author[0].post(
            env_url + "/runs", headers={**headers, "Idempotency-Key": str(uuid4())}, json=request
        )
        assert response.status_code == 422
    request["input"] = {}
    response = await author[0].post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "absent-secret"}, json=request
    )
    assert response.status_code == 201 and response.json()["state"]["output"] == 7


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    schema = {"type": "object", "properties": {"credential": {"x-secret": True}}}
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["spec"]["outputSchema"] = schema
    action = load_definition(action)
    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    contract["outputSchema"] = schema
    catalog = CatalogSnapshot.from_definitions([action], tasks=[contract])
    document = json.loads(worker_runtime_fixture["source"])
    document["spec"]["timeoutSeconds"] = 600
    if getattr(request, "param", None) == "parallel":
        document["spec"]["steps"] = [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    name: {
                        "steps": [
                            {"id": name, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
                        ],
                        "output": {"ref": f"/steps/{name}/output"},
                    }
                    for name in ("a", "b")
                },
            }
        ]
        document["spec"]["output"] = {"ref": "/steps/fork/output"}
    if getattr(request, "param", None) == "signal":
        document["spec"]["steps"] = [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "worker": {
                        "steps": [
                            {"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
                        ],
                        "output": {"ref": "/steps/work/output"},
                    },
                    "signal": {
                        "steps": [
                            {
                                "id": "approval",
                                "kind": "signal",
                                "name": "approve",
                                "timeoutSeconds": 300,
                                "payloadSchema": {"properties": {"credential": {"writeOnly": True}}},
                            }
                        ],
                        "output": {"ref": "/steps/approval/output"},
                    },
                },
            }
        ]
        document["spec"]["output"] = {"ref": "/steps/fork/output"}
    source = json.dumps(document)
    result = compile_source(source, format="json", catalog=catalog)
    assert result.ok
    return {
        **worker_runtime_fixture,
        "catalog": catalog,
        "action": action,
        "artifact": result.artifact,
        "source": source,
    }


async def test_marked_output_safe_rejection_receipt(
    task_service, transaction_factory, queued_task, worker_ids, access_db, caplog
):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    identifier = uuid4()
    canary = "C4a-output-" + str(uuid4())
    output = {"credential": canary}
    fingerprint = canonical_digest({"status": "completed", "output": output})
    with pytest.raises(RuntimeError, match="rollback"):
        async with transaction_factory() as tx:
            assert (await task_service.complete(tx, lease.proof, identifier, output)).status == "rejected"
            raise RuntimeError("rollback")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM incidents")) == 0
        assert await session.scalar(text("SELECT status FROM task_leases")) == "active"
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, lease.proof, identifier, output)
    await assert_absent(access_db[1], canary, canonical_digest(output), fingerprint)
    assert receipt.status == "rejected" and receipt.accepted_output_hash is None
    assert receipt.reason_code == "WV-SCHEMA-SECRET_VALUE"
    for changed in (output, {}, {"credential": "changed"}):
        async with transaction_factory() as tx:
            assert await task_service.complete(tx, lease.proof, identifier, changed) == receipt
    with pytest.raises((AccessDenied, CatalogError)):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), {})
    async with access_db[1]() as session:
        state = await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["status"] == "suspended"
        assert await session.scalar(text("SELECT count(*) FROM incidents")) == 1
        assert await session.scalar(text("SELECT count(*) FROM completion_receipts")) == 1
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 2
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE worker_instances SET revoked=true"))
    with pytest.raises((AccessDenied, CatalogError)):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, identifier, output)
    assert canary not in caplog.text


async def test_source_and_draft_literals_never_persist(author, headers, project_url, publication_request, access_db):
    import yaml

    canary = "C4a-source-" + str(uuid4())
    document = yaml.safe_load(publication_request["source"])
    document["spec"]["inputSchema"] = {"x-secret": True, "default": canary}
    response = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "literal"},
        json={"format": "json", "source": json.dumps(document)},
    )
    assert response.status_code == 422 and canary not in response.text
    url = project_url + "/drafts/" + str(uuid4())
    response = await author[0].put(url, headers=headers, json={"document": document})
    assert response.status_code == 422 and canary not in response.text
    await assert_absent(access_db[1], canary, canonical_digest(document))
    response = await author[0].put(url, headers=headers, json={"document": {"incomplete": True}})
    assert response.status_code == 201
    first_etag = response.headers["etag"]
    draft = {"spec": {"outputSchema": {"x-secret": True}, "output": {"ref": "/input"}}}
    response = await author[0].put(url, headers={**headers, "If-Match": first_etag}, json={"document": draft})
    assert response.status_code == 200
    assert (await author[0].get(url, headers=headers)).json()["document"] == draft
    assert (
        await author[0].put(url, headers={**headers, "If-Match": first_etag}, json={"document": {}})
    ).status_code == 412


async def test_legacy_read_and_mutation_replay_withheld(worker_setup, queued_task, access_db):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError

    _, runtime, _, actor, scope, activation, _ = worker_setup
    canary = "C4a-legacy-" + str(uuid4())
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE runs SET state=(state-'admission_policy') || cast(:legacy AS jsonb) WHERE id=:id"),
            {"id": queued_task.id, "legacy": json.dumps({"input": {"credential": canary}})},
        )
    with pytest.raises(CatalogError) as rejected:
        await runtime.read(actor, scope, queued_task.id, context=AuditContext())
    assert rejected.value.code == "WV-LEGACY-UNAVAILABLE"
    assert canary not in json.dumps(rejected.value.result)
    # Fixture queued_task uses this stable start identity.
    with pytest.raises(CatalogError) as replay:
        await runtime.start(
            actor, scope, StartRunRequest(activation_id=activation.id, input=3), "run", context=AuditContext()
        )
    assert replay.value.code == "WV-LEGACY-UNAVAILABLE"


async def test_ignored_marked_output_retains_no_digest(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, access_db
):
    from firefly_weave.access.audit import AuditContext

    _, runtime, _, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with transaction_factory() as tx:
        await runtime.cancel(tx, queued_task.id, "stop", actor=actor, scope=scope, context=AuditContext())
    canary = "C4a-late-" + str(uuid4())
    output = {"credential": canary}
    identifier = uuid4()
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, lease.proof, identifier, output)
    assert receipt.status == "rejected" and receipt.accepted_output_hash is None
    async with transaction_factory() as tx:
        assert await task_service.complete(tx, lease.proof, identifier, {}) == receipt
    await assert_absent(access_db[1], canary, canonical_digest({"status": "completed", "output": output}))
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM incidents")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 2


async def test_reconciled_marked_output_does_not_create_resolution(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, services, access_db
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations.incidents import IncidentService

    _, _, _, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="HANDLER_FAILED"))
    incidents = services(access_db[0]).resolve(IncidentService)
    async with transaction_factory() as tx:
        incident = (await incidents.list(tx, queued_task.id, actor=actor, scope=scope, context=AuditContext()))[0]
    canary = "C4a-reconcile-" + str(uuid4())
    request = IncidentResolution(
        receipt_id=uuid4(),
        kind="accept_reconciled_result",
        reason="checked",
        evidence_reference="external-evidence",
        output={"credential": canary},
    )
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await incidents.resolve(
                tx, incident.id, request, incident.revision, actor=actor, scope=scope, context=AuditContext()
            )
    await assert_absent(access_db[1], canary, canonical_digest(request.model_dump(mode="json")))
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == 0


@pytest.mark.parametrize("worker_runtime_fixture", ["parallel"], indirect=True)
@pytest.mark.parametrize("malformed", [False, True])
async def test_legacy_cancel_quarantines_and_revokes(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, access_db, malformed
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError

    _, runtime, _, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        leases = []
        for worker in worker_ids:
            leases.extend(await task_service.claim(tx, worker, 1))
        assert len(leases) == 2
        lease = leases[0]
    canary = "C4a-old-" + str(uuid4())
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE runs SET state=(state-'admission_policy') || cast(:legacy AS jsonb) WHERE id=:id"),
            {"id": queued_task.id, "legacy": json.dumps({"input": {"credential": canary}})},
        )
        if malformed:
            await session.execute(text("UPDATE runs SET artifact='{}',activation='{}',request='{}'"))
        # Owner fixture represents an already-retained pre-policy immutable event.
        await session.execute(
            text("UPDATE run_events SET data=data || cast(:legacy AS jsonb) WHERE sequence=1"),
            {"legacy": json.dumps({"old_payload": canary})},
        )
        historic = await session.scalar(text("SELECT to_jsonb(e)::text FROM run_events e WHERE sequence=1"))
    async with transaction_factory() as tx:
        ack = await runtime.cancel(tx, queued_task.id, "stop", actor=actor, scope=scope, context=AuditContext())
    assert ack.unavailable and ack.status == "cancelled"
    async with transaction_factory() as tx:
        assert await runtime.cancel(tx, queued_task.id, "stop", actor=actor, scope=scope, context=AuditContext()) == ack
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), {"credential": canary})
    async with access_db[1]() as session:
        assert set((await session.execute(text("SELECT status FROM task_leases"))).scalars()) == {"control_revoked"}
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 2
        assert await session.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await session.scalar(text("SELECT to_jsonb(e)::text FROM run_events e WHERE sequence=1")) == historic
    await assert_absent(access_db[1], canary, canonical_digest(canary), retained={"run_events": [historic]})


async def test_policy_blocks_advance_bounded_claim_pages(
    worker_setup,
    queued_task,
    access_db,
    worker_ids,
    task_service,
    transaction_factory,
    monkeypatch,
    services,
    provisioned,
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.repository import RuntimeRepository

    _, runtime, _, actor, scope, activation, _ = worker_setup
    runs = [queued_task]
    for index in range(4):
        runs.append(
            await runtime.start(
                actor,
                scope,
                StartRunRequest(activation_id=activation.id, input=3),
                "page-" + str(index),
                context=AuditContext(),
            )
        )
    async with access_db[1].begin() as session:
        tasks = (await session.execute(text("SELECT id,run_id FROM task_intents ORDER BY id"))).all()
        blocked = [row.run_id for row in tasks[:4]]
        for run_id in blocked:
            await session.execute(text("UPDATE runs SET state=state-'admission_policy' WHERE id=:id"), {"id": run_id})
    original = RuntimeRepository.candidates

    async def small_page(self, release, references, *, limit=100):
        return await original(self, release, references, limit=2)

    monkeypatch.setattr(RuntimeRepository, "candidates", small_page)
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.workers.leases import TaskService

    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE worker_instances SET revoked=true WHERE id=:id"), {"id": worker_ids[1]})
    with pytest.raises((AccessDenied, CatalogError)):
        async with transaction_factory() as tx:
            await task_service.claim(tx, worker_ids[1], 1)
    async with UnitOfWork(access_db[0]).open(provisioned[1][1]) as tx:
        with pytest.raises((AccessDenied, CatalogError)):
            await worker_setup[0].claim(
                tx, worker_ids[0], 1, actor=actor, scope=provisioned[1][1], context=AuditContext()
            )
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 0
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[0], 1) == []
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[0], 1) == []
    restarted = services(access_db[0]).resolve(TaskService)
    async with transaction_factory() as tx:
        assert len(await restarted.claim(tx, worker_ids[0], 1, actor=actor, scope=scope, context=AuditContext())) == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 4
        assert not await session.scalar(text("SELECT has_table_privilege('weave_app','run_policy_blocks','UPDATE')"))


@pytest.mark.parametrize(
    "waiting_run",
    [
        test_waits.SOURCE.replace(
            "approved: {type: boolean}", "approved: {type: boolean}, credential: {writeOnly: true}"
        )
    ],
    indirect=True,
)
async def test_signal_ingress_classified_presence(client, headers, env_url, waiting_run, access_db):
    canary = "C4a-signal-" + str(uuid4())
    request = {
        "eventId": "classified",
        "name": "customer-approved",
        "payload": {"approved": True, "credential": canary},
    }
    response = await client.post(f"{env_url}/runs/{waiting_run}/signals", headers=headers, json=request)
    assert response.status_code == 422
    await assert_absent(access_db[1], canary, canonical_digest(request))
    request["payload"] = {"approved": True}
    assert (
        await client.post(f"{env_url}/runs/{waiting_run}/signals", headers=headers, json=request)
    ).status_code == 202


async def test_webhook_classification_before_receipt(worker_setup, access_db, services, monkeypatch):
    import hashlib

    import test_webhooks

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.triggers.models import TriggerRequest
    from firefly_weave.triggers.service import WebhookService

    _, _, _, actor, scope, activation, _ = worker_setup
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_WEBHOOK", "webhook-canary")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "webhook", "env", "WEAVE_CONNECTION_SECRET_WEBHOOK"),)
    )
    service = services(access_db[0], secrets=secrets).resolve(WebhookService)
    trigger = await service.create(
        actor,
        scope,
        TriggerRequest(
            name="classified",
            kind="run",
            activation_id=activation.id,
            secret_ref="webhook",
            payload_schema={"properties": {"credential": {"x-secret": True}}},
        ),
        context=AuditContext(),
    )
    canary = "C4a-hook-" + str(uuid4())
    raw = json.dumps({"eventId": "event-1", "payload": {"credential": canary}}).encode()
    with pytest.raises(CatalogError):
        await service.receive(trigger.id, raw, test_webhooks.signed(raw))
    await assert_absent(access_db[1], canary, hashlib.sha256(raw).hexdigest())


async def test_connection_config_rejects_but_typed_handles_survive(connections, access_db):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError

    service, definitions, actor, scope, request = connections
    row = await definitions.read(
        actor, scope, "Connector", request.connector_version_id, export=True, context=AuditContext()
    )
    document = row["document"]
    document["metadata"]["version"] = "1.0.1"
    document["spec"]["authSchema"]["properties"]["token"]["writeOnly"] = True
    document["spec"]["configSchema"] = {"properties": {"credential": {"x-secret": True}}}
    version = await definitions.publish(
        actor, scope, "Connector", json.dumps(document), "json", "classified-config", context=AuditContext()
    )
    canary = "C4a-config-" + str(uuid4())
    request = request.model_copy(update={"connector_version_id": version.id, "config": {"credential": canary}})
    with pytest.raises(CatalogError):
        await service.create_revision(actor, scope, request, context=AuditContext())
    await assert_absent(access_db[1], canary, canonical_digest(request.model_dump(mode="json")))
    revision = await service.create_revision(
        actor, scope, request.model_copy(update={"config": {}}), context=AuditContext()
    )
    assert revision.secret_refs == {"token": "approved"}


@pytest.mark.parametrize("malformed", [False, True])
async def test_legacy_overall_timeout_block_is_atomic_and_once(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, access_db, services, malformed
):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.deadlines import DeadlineService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE runs SET state=state-'admission_policy'"))
        if malformed:
            await session.execute(text("UPDATE runs SET artifact='{}',activation='{}',request='{}'"))
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='@run'")
        )
    service = services(access_db[0]).resolve(DeadlineService)
    with pytest.raises(RuntimeError, match="rollback"):
        async with transaction_factory() as tx:
            assert await service.tick(tx, 1) == 1
            raise RuntimeError("rollback")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 0
        assert await session.scalar(text("SELECT status FROM task_leases")) == "active"
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 0
    async with transaction_factory() as tx:
        assert await service.tick(tx, 1) == 1
    async with transaction_factory() as tx:
        assert await service.tick(tx, 1) == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "timed_out"
        assert await session.scalar(text("SELECT status FROM task_leases")) == "policy_blocked"
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), {})


@pytest.mark.parametrize("worker_runtime_fixture", ["signal"], indirect=True)
@pytest.mark.parametrize("earlier", [False, True])
async def test_future_legacy_signal_deadline_after_claim_block(
    earlier, task_service, transaction_factory, queued_task, worker_ids, access_db, services
):
    import asyncio

    from firefly_weave.runtime.deadlines import DeadlineService

    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE runs SET state=state-'admission_policy'"))
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[0], 1) == []
    async with access_db[1].begin() as session:
        assert await session.scalar(text("SELECT terminal_signals_verified FROM run_policy_blocks"))
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id<>'@run'")
        )
        if earlier:
            await session.execute(
                text(
                    "INSERT INTO signal_receipts SELECT :id,tenant_id,project_id,environment_id,id,"
                    "'buffered','approve','{}',:hash,clock_timestamp()-interval '5 seconds',false FROM runs"
                ),
                {"id": uuid4(), "hash": canonical_digest({})},
            )
    service = services(access_db[0]).resolve(DeadlineService)

    async def tick():
        async with transaction_factory() as tx:
            return await service.tick(tx, 1)

    assert sum(await asyncio.gather(tick(), tick())) == (0 if earlier else 1)
    assert await tick() == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == (
            "waiting" if earlier else "timed_out"
        )
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == (0 if earlier else 1)
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='signal_received'")) == 0


@pytest.mark.parametrize("worker_runtime_fixture", ["signal"], indirect=True)
async def test_malformed_legacy_deadline_pages_do_not_starve_valid_run(
    worker_setup, queued_task, access_db, services, transaction_factory
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.deadlines import DeadlineService

    _, runtime, _, actor, scope, activation, _ = worker_setup
    for index in range(4):
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input=3),
            "malformed-page-" + str(index),
            context=AuditContext(),
        )
    async with access_db[1].begin() as session:
        identifiers = list((await session.execute(text("SELECT id FROM runs ORDER BY id"))).scalars())
        for identifier in identifiers[:4]:
            await session.execute(
                text(
                    "UPDATE runs SET state=state-'admission_policy', "
                    "artifact=jsonb_set(artifact,'{executable,graph,nodes}','{}') WHERE id=:id"
                ),
                {"id": identifier},
            )
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id<>'@run'")
        )
    service = services(access_db[0]).resolve(DeadlineService)
    for expected in (0, 0, 1, 0):
        async with transaction_factory() as tx:
            assert await service.tick(tx, 2) == expected
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 4
        assert not await session.scalar(text("SELECT bool_or(terminal_signals_verified) FROM run_policy_blocks"))
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1
        event = (await session.execute(text("SELECT data FROM run_events WHERE type='timed_out'"))).scalar_one()
        assert event["node_id"] != "@run" and event["wait_id"]


async def test_policy_fence_survives_cancel_and_denies_live_operations(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, access_db
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.repository import RuntimeRepository

    _, runtime, _, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE runs SET state=state-'admission_policy'"))
    async with transaction_factory() as tx:
        row = await RuntimeRepository(tx).run(queued_task.id, lock=True)
        assert await runtime.observe_unavailable(tx, row)
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.heartbeat(tx, lease.proof)
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), {})
    from firefly_weave.contracts.workers import CredentialRequest

    with pytest.raises(CatalogError):
        await task_service.credentials(
            CredentialRequest(lease=lease.proof, connection_revision_id=uuid4(), slot="token")
        )
    async with transaction_factory() as tx:
        await runtime.cancel(tx, queued_task.id, "stop", actor=actor, scope=scope, context=AuditContext())
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM task_leases")) == "policy_blocked"
        assert await session.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 1


@pytest.mark.parametrize(
    "waiting_run",
    [
        test_waits.SOURCE.replace(
            "approved: {type: boolean}", "approved: {type: boolean}, credential: {writeOnly: true}"
        )
    ],
    indirect=True,
)
async def test_public_legacy_problem_replay_and_control_ack(client, headers, env_url, waiting_run, access_db):
    canary = "C4a-public-legacy-" + str(uuid4())
    async with access_db[1].begin() as session:
        request = await session.scalar(text("SELECT request FROM runs"))
        await session.execute(
            text("UPDATE runs SET state=(state-'admission_policy') || cast(:value AS jsonb)"),
            {"value": json.dumps({"input": {"credential": canary}})},
        )
    url = f"{env_url}/runs/{waiting_run}"
    response = await client.get(url, headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "WV-LEGACY-UNAVAILABLE"
    result = response.json()["result"]
    assert result["available"] is False and result["value"] == {"id": waiting_run, "status": "waiting"}
    assert result["omissions"] == [{"path": "/state", "reason": "uncertain_derived"}]
    replay = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "signal-run"}, json=request)
    assert replay.status_code == 409 and replay.json()["code"] == "WV-LEGACY-UNAVAILABLE"
    response = await client.post(url + "/cancel", headers=headers, json={"reason": canary})
    assert response.status_code == 200
    acknowledgment = response.json()
    assert set(acknowledgment) == {
        "id",
        "status",
        "accepted_sequence",
        "unavailable",
        "omissions",
        "external_effects_may_continue",
    }
    assert acknowledgment["status"] == "cancelled" and acknowledgment["unavailable"] is True
    assert all(item["reason"] == "uncertain_derived" for item in acknowledgment["omissions"])
    assert (await client.post(url + "/cancel", headers=headers, json={"reason": "again"})).json() == acknowledgment
    assert (await client.get(url, headers=headers)).json()["result"]["value"]["status"] == "cancelled"
    await assert_absent(access_db[1], canary, canonical_digest(canary))


@pytest.mark.parametrize("case", ["composed", "nested_composed", "coalesce"])
@pytest.mark.parametrize("operation", ["draft", "update", "publish"])
async def test_effective_literals_never_reach_durable_authoring(
    case, operation, author, headers, project_url, publication_request, access_db
):
    import yaml

    canary = "C4a-fix1-" + str(uuid4())
    document = yaml.safe_load(publication_request["source"])
    if case == "composed":
        document["spec"]["inputSchema"] = {"allOf": [{"x-secret": True}, {"default": canary}]}
    elif case == "nested_composed":
        document["spec"]["inputSchema"] = {
            "properties": {"p": {"writeOnly": True}},
            "allOf": [{"properties": {"p": {"examples": [canary]}}}],
        }
    else:
        document["spec"]["outputSchema"] = {"properties": {"p": {"x-secret": True}}}
        document["spec"]["output"] = {
            "op": {"name": "coalesce", "args": [{"ref": "/input"}, {"literal": {"p": canary}}]}
        }
    url = project_url + "/drafts/" + str(uuid4())
    request_headers = headers
    if operation == "update":
        first = await author[0].put(url, headers=headers, json={"document": {"incomplete": True}})
        assert first.status_code == 201
        request_headers = {**headers, "If-Match": first.headers["etag"]}
    if operation == "publish":
        response = await author[0].post(
            project_url + "/workflows",
            headers={**headers, "Idempotency-Key": "composed-literal"},
            json={"format": "json", "source": json.dumps(document)},
        )
    else:
        response = await author[0].put(url, headers=request_headers, json={"document": document})
    await assert_absent(access_db[1], canary, canonical_digest(document), canonical_digest(canary))
    assert response.status_code == 422 and canary not in response.text


@pytest.mark.parametrize("worker_runtime_fixture", ["parallel"], indirect=True)
@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("revocation", ["instance", "grant"])
async def test_policy_block_replays_prior_receipt_without_payload_proof(
    rejected,
    revocation,
    task_service,
    transaction_factory,
    queued_task,
    worker_ids,
    worker_setup,
    access_db,
    provisioned,
):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.repository import RuntimeRepository

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        prior = await task_service.complete(tx, lease.proof, uuid4(), {"credential": "rejected"} if rejected else {})
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE runs SET state=state-'admission_policy'"))
        original = await session.scalar(text("SELECT to_jsonb(r)::text FROM completion_receipts r"))
        events = await session.scalar(text("SELECT count(*) FROM run_events"))
    async with transaction_factory() as tx:
        row = await RuntimeRepository(tx).run(queued_task.id, lock=True)
        assert await worker_setup[1].observe_unavailable(tx, row)
    canary = "C4a-fix1-replay-" + str(uuid4())
    for changed in ({}, {"credential": canary}):
        async with transaction_factory() as tx:
            receipt = await task_service.complete(tx, lease.proof, prior.completion_id, changed)
        if rejected:
            assert receipt == prior
        else:
            assert receipt.unavailable is True and receipt.payload_match == "unavailable"
            assert receipt.status == prior.status and receipt.accepted_at == prior.accepted_at
            assert (
                "accepted_output_hash" not in receipt.model_dump()
                and prior.accepted_output_hash not in receipt.model_dump_json()
            )
    with pytest.raises((CatalogError, AccessDenied)):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), {})
    with pytest.raises((CatalogError, AccessDenied)):
        async with transaction_factory() as tx:
            await task_service.complete(
                tx, lease.proof.model_copy(update={"owner": worker_ids[1]}), prior.completion_id, {}
            )
    async with access_db[1].begin() as session:
        assert await session.scalar(text("SELECT to_jsonb(r)::text FROM completion_receipts r")) == original
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == events
        if revocation == "instance":
            await session.execute(text("UPDATE worker_instances SET revoked=true"))
        else:
            grant_id = await session.scalar(
                text("SELECT id FROM role_bindings WHERE principal_id=:actor AND role='worker'"),
                {"actor": worker_setup[3].id},
            )
    if revocation == "grant":
        await access_db[2].revoke_grant(provisioned[0], worker_setup[4], grant_id)
    with pytest.raises((CatalogError, AccessDenied)):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, prior.completion_id, {})
    await assert_absent(
        access_db[1], canary, canonical_digest({"status": "completed", "output": {"credential": canary}})
    )


async def test_prior_provenance_cannot_bypass_current_artifact_policy_or_starve_work(
    worker_setup, queued_task, task_service, worker_ids, transaction_factory, access_db, monkeypatch
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.compiler.ir import ArtifactEnvelope
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.repository import RuntimeRepository

    _, runtime, _, actor, scope, activation, _ = worker_setup
    for index in range(4):
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input=3),
            "prior-policy-" + str(index),
            context=AuditContext(),
        )
    canary = "C4a-fix1-prior-policy-" + str(uuid4())
    schema = {"allOf": [{"x-secret": True}, {"default": canary}]}
    async with access_db[1].begin() as session:
        identifiers = list((await session.execute(text("SELECT run_id FROM task_intents ORDER BY id"))).scalars())
        for identifier in identifiers[:4]:
            envelope = await session.scalar(text("SELECT artifact FROM runs WHERE id=:id"), {"id": identifier})
            envelope["executable"]["inputSchema"] = canonical_digest(schema)
            envelope["executable"]["schemas"][canonical_digest(schema)] = schema
            envelope["digest"] = canonical_digest(envelope["executable"])
            ArtifactEnvelope.model_validate(envelope)
            await session.execute(
                text("UPDATE runs SET artifact=cast(:artifact AS jsonb) WHERE id=:id"),
                {"artifact": json.dumps(envelope), "id": identifier},
            )
        assert await session.scalar(text("SELECT bool_and(state->>'admission_policy'='classified-v1') FROM runs"))
        artifacts = list((await session.execute(text("SELECT artifact FROM runs ORDER BY id"))).scalars())
    original = RuntimeRepository.candidates

    async def small_page(self, release, references, *, limit=100):
        return await original(self, release, references, limit=2)

    monkeypatch.setattr(RuntimeRepository, "candidates", small_page)
    for expected in (0, 0, 1):
        async with transaction_factory() as tx:
            assert len(await task_service.claim(tx, worker_ids[0], 1)) == expected
    async with transaction_factory() as tx:
        acknowledgment = await runtime.cancel(
            tx, identifiers[0], "stop", actor=actor, scope=scope, context=AuditContext()
        )
    assert acknowledgment.unavailable and acknowledgment.status == "cancelled"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_policy_blocks")) == 4
        assert list((await session.execute(text("SELECT artifact FROM runs ORDER BY id"))).scalars()) == artifacts
        retained_runs = list((await session.execute(text("SELECT to_jsonb(r)::text FROM runs r"))).scalars())
        for row in (await session.execute(text("SELECT state,request,activation FROM runs"))).all():
            assert canary not in json.dumps(list(row))
    await assert_absent(access_db[1], canary, canonical_digest(canary), retained={"runs": retained_runs})
