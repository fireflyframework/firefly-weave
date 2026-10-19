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

"""Real database lease fencing, accepted receipts, and worker grant isolation."""

import asyncio
from uuid import uuid4

import pytest
import test_connections
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration
connections = test_connections.connections


async def test_competing_workers_cannot_both_own_one_task(task_service, transaction_factory, queued_task, worker_ids):
    async def claim(worker_id):
        async with transaction_factory() as tx:
            return await task_service.claim(tx, worker_id, limit=1)

    claims = await asyncio.gather(*(claim(w) for w in worker_ids))
    assert sum(len(batch) for batch in claims) == 1


async def test_completion_atomic_replay_conflict(task_service, transaction_factory, queued_task, worker_ids, access_db):
    from firefly_weave.definitions.models import CatalogError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    completion = uuid4()
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, lease.proof, completion, 42)
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    async with transaction_factory() as tx:
        assert await task_service.complete(tx, lease.proof, completion, 42) == receipt
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, completion, 43)
    async with access_db[1]() as tx:
        state = await tx.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["status"] == "succeeded" and state["output"] == 42
        assert await tx.scalar(text("SELECT count(*) FROM completion_receipts")) == 1


@pytest.mark.parametrize("mutation", ["token", "owner", "generation", "expiry", "revoked"])
async def test_invalid_proof_denied(mutation, task_service, transaction_factory, queued_task, worker_ids, access_db):
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    proof = lease.proof
    if mutation == "token":
        proof = proof.model_copy(update={"token": "forged"})
    elif mutation == "owner":
        proof = proof.model_copy(update={"owner": worker_ids[1]})
    elif mutation == "generation":
        proof = proof.model_copy(update={"generation": proof.generation + 1})
    else:
        async with access_db[1].begin() as tx:
            await tx.execute(
                text(
                    "UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"
                    if mutation == "expiry"
                    else "UPDATE worker_instances SET revoked=true"
                )
            )
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises((AccessDenied, CatalogError)):
        async with transaction_factory() as tx:
            await task_service.complete(tx, proof, uuid4(), 42)


async def test_deadline_schema_budget_and_rollback(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    from firefly_weave.definitions.models import CatalogError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        renewed = await task_service.heartbeat(tx, lease.proof)
        assert renewed.expires_at <= renewed.deadline
    for output in ["wrong", "x" * (1024 * 1024 + 1)]:
        with pytest.raises((ValueError, CatalogError)):
            async with transaction_factory() as tx:
                await task_service.complete(tx, lease.proof, uuid4(), output)
    with pytest.raises(RuntimeError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), 8)
            raise RuntimeError("rollback")
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM run_events")) == 1


async def test_failure_is_durable_and_never_silently_retried(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    from firefly_weave.contracts.workers import TaskError

    error = TaskError(completion_id=uuid4(), code="CONNECTION_LOST", outcome="unknown")
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with transaction_factory() as tx:
        receipt = await task_service.fail(tx, lease.proof, error)
        assert receipt.status == "failed"
    async with transaction_factory() as tx:
        assert await task_service.fail(tx, lease.proof, error) == receipt
        assert await task_service.claim(tx, worker_ids[1], 1) == []
    async with access_db[1]() as tx:
        policy = await tx.scalar(text("SELECT policy FROM task_leases"))
        assert policy["side_effect"] == "read_only" and policy["retry"]["maxAttempts"] >= 1
        data = await tx.scalar(text("SELECT data FROM run_events WHERE type='task_failed'"))
        assert data["output"]["outcome"] == "unknown"


async def test_current_principal_required_for_accepted_replay(
    task_service, transaction_factory, queued_task, worker_ids, worker_setup, access_db
):
    from firefly_weave.access.oidc import AuthenticationFailed

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        completion = uuid4()
        await task_service.complete(tx, lease.proof, completion, 4)
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": worker_setup[3].id})
    with pytest.raises(AuthenticationFailed):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, completion, 4)


async def test_incompatible_release_and_instance_constraints(worker_setup):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.models import CatalogError

    _, runtime, workers, actor, scope, activation, _ = worker_setup
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "b" * 64,
            capabilities=[
                {
                    "taskType": "other",
                    "taskVersion": "1.0.0",
                    "inputSchema": {},
                    "outputSchema": {},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                }
            ],
        ),
        context=AuditContext(),
    )
    with pytest.raises(CatalogError):
        await workers.definitions.activate(
            actor,
            scope,
            activation.request.model_copy(update={"worker_release_ids": {"echo": release.id}}),
            "incompatible",
            1,
            context=AuditContext(),
        )
    with pytest.raises(AccessDenied):
        await workers.register_instance(
            actor,
            scope,
            InstanceRequest(release_id=release.id, task_types=["echo@1.2.0"], capacity=1),
            context=AuditContext(),
        )


async def test_expired_attempt_retained_no_reclaim(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    async with transaction_factory() as tx:
        await task_service.claim(tx, worker_ids[0], 1)
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[1], 1) == []
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM task_leases")) == 1


async def test_worker_rls_scoped_fk_and_immutable_grants(worker_setup, access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    sessions, owner, _, _ = access_db
    async with owner() as tx:
        rows = (
            await tx.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname "
                    "IN "
                    "('worker_releases','worker_instances','task_leases',"
                    "'completion_receipts','worker_connection_grants')"
                )
            )
        ).all()
        assert len(rows) == 5 and all(all(r) for r in rows)
    async with UnitOfWork(sessions).open(provisioned[1][1]) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM worker_releases")) == 0
        for table in ("worker_releases", "completion_receipts"):
            assert not await tx.session.scalar(
                text("SELECT has_table_privilege(current_user,:table,'UPDATE')"), {"table": table}
            )
        assert not await tx.session.scalar(
            text("SELECT has_column_privilege(current_user,'task_leases','token_hash','UPDATE')")
        )


async def test_native_http_release_to_completion(
    client, authenticated_client, access_db, provisioned, headers, project_url, env_url, worker_runtime_fixture
):
    import json
    from uuid import UUID

    from firefly_weave.workers.leases import TaskService
    from firefly_weave.workers.service import WorkerService

    container = client._transport.app.state.pyfly.context.container
    tasks = container.resolve(TaskService)
    workers = container.resolve(WorkerService)
    assert tasks.workers is workers and workers.definitions.workers.get() is workers
    assert container.resolve(TaskService) is tasks
    assert not hasattr(tasks, "actor") and not hasattr(tasks, "scope")

    from firefly_weave.access.models import Grant
    from firefly_weave.sdk.transport import WorkerTransport

    sessions, owner, access, _ = access_db
    scope = provisioned[1][0]
    async with sessions() as tx:
        principal = await tx.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    for role in ("developer", "deployer", "operator", "worker"):
        await access.grant(
            provisioned[0],
            principal,
            Grant(role=role, scope=scope.model_copy(update={"environment_id": None}) if role == "developer" else scope),
        )
    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = await client.post(
        env_url + "/worker-releases",
        headers=headers,
        json={"image_digest": "sha256:" + "a" * 64, "capabilities": [contract]},
    )
    assert release.status_code == 201, release.text
    for kind, source in [
        ("actions", json.dumps(worker_runtime_fixture["action"].model_dump(by_alias=True))),
        ("workflows", worker_runtime_fixture["source"]),
    ]:
        response = await client.post(
            project_url + "/" + kind,
            headers={**headers, "Idempotency-Key": kind},
            json={"source": source, "format": "json"},
        )
        assert response.status_code == 201, response.text
    activation = await client.post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "activation"},
        json={
            "version_id": response.json()["id"],
            "artifact_digest": response.json()["digest"],
            "scope": scope.model_dump(mode="json"),
            "worker_release_ids": {"echo": release.json()["id"]},
        },
    )
    assert activation.status_code == 201, activation.text
    run = await client.post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "run"},
        json={"activation_id": activation.json()["id"], "input": 6},
    )
    assert run.status_code == 201 and run.json()["state"]["status"] == "waiting", run.text
    instance = await client.post(
        env_url + "/workers",
        headers=headers,
        json={"release_id": release.json()["id"], "task_types": ["echo@1.2.0"], "capacity": 2},
    )
    assert instance.status_code == 201, instance.text
    # Same real authenticated machine identity becomes a worker, retaining authoring grants;
    # native policy must still prohibit worker authoring.
    async with owner.begin() as tx:
        await tx.execute(text("UPDATE principals SET kind='worker' WHERE id=:id"), {"id": principal})
    forbidden = await client.post(
        env_url + "/worker-releases",
        headers=headers,
        json={"image_digest": "sha256:" + "b" * 64, "capabilities": [contract]},
    )
    assert forbidden.status_code == 403, forbidden.text
    client.headers.update(headers)
    transport = WorkerTransport(client, env_url, UUID(instance.json()["id"]))
    lease = (await transport.claim(1))[0]
    assert lease.input == 6 and lease.capability == "echo@1.2.0"
    await transport.heartbeat(lease.proof)
    receipt = await transport.complete(lease.proof, uuid4(), 12)
    assert receipt.status == "completed"
    async with owner() as tx:
        assert (await tx.scalar(text("SELECT state FROM runs")))["output"] == 12


@pytest.fixture
async def credential_task(services, connections, access_db, provisioned, worker_runtime_fixture):
    import json

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import (
        CredentialGrantRequest,
        CredentialRequest,
        InstanceRequest,
        ReleaseRequest,
    )
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.leases import TaskService
    from firefly_weave.workers.service import WorkerService

    connection_service, definitions, actor, scope, request = connections
    for role in ("worker", "operator"):
        await access_db[2].grant(provisioned[0], actor.id, Grant(role=role, scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    revision = await connection_service.create_revision(actor, scope, request, context=AuditContext())
    graph = services(access_db[0], registry=connection_service.registry, secrets=connection_service.secrets)
    definitions = graph.resolve(DefinitionService)
    workers = graph.resolve(WorkerService)
    runtime = graph.resolve(RuntimeService)
    tasks = graph.resolve(TaskService)
    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "c" * 64, capabilities=[contract], credential_capabilities=["echo@1.2.0"]
        ),
        context=AuditContext(),
    )
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["spec"]["connection"] = {"connector": "example@1.0.0"}
    await definitions.publish(actor, scope, "Action", json.dumps(action), "json", "action", context=AuditContext())
    flow = json.loads(worker_runtime_fixture["source"])
    flow["spec"]["connections"] = {"api": {"connector": "example@1.0.0"}}
    flow["spec"]["steps"][0]["connection"] = "api"
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(flow), "json", "flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={"echo": release.id},
            connection_revision_ids={"api": revision.id},
        ),
        "activate",
        context=AuditContext(),
    )
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=7), "run", context=AuditContext()
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=["echo@1.2.0"], capacity=1),
        context=AuditContext(),
    )
    async with definitions.transaction(scope, None) as tx:
        lease = (await tasks.claim(tx, instance.id, 1, **authority))[0]
    grant = CredentialGrantRequest(release_id=release.id, connection_revision_id=revision.id, capability="echo@1.2.0")
    request = CredentialRequest(lease=lease.proof, connection_revision_id=revision.id, slot="token")
    return tasks, workers, authority, grant, request, run


async def test_credentials_require_grant_live_proof_and_redact(credential_task, monkeypatch, access_db):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    tasks, workers, authority, grant, request, run = credential_task
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "credential-canary")
    original = tasks.secrets.resolve
    monkeypatch.setattr(
        tasks.secrets,
        "resolve",
        lambda scope, handle: original(scope, handle).model_copy(update={"provider_version": "rotation-2"}),
    )
    with pytest.raises(AccessDenied):
        await tasks.credentials(request, **authority)
    await workers.grant_connection(authority["actor"], authority["scope"], grant, context=authority["context"])
    credential = await tasks.credentials(request, **authority)
    assert credential.value == "credential-canary"
    assert "credential-canary" not in repr(credential) + credential.model_dump_json()
    async with access_db[1]() as tx:
        assert credential.expires_at == await tx.scalar(text("SELECT expires_at FROM task_leases"))
        event = await tx.scalar(text("SELECT event FROM access_audit WHERE action='credential.resolve'"))
        assert event is not None and event["details"]["provider_version"] == "rotation-2"
        assert event["details"]["generation"] == request.lease.generation
        assert "credential-canary" not in str(event)
        for table, column in [
            ("completion_receipts", "payload"),
            ("run_events", "data"),
            ("worker_releases", "payload"),
            ("task_intents", "payload"),
        ]:
            assert "credential-canary" not in str((await tx.execute(text(f"SELECT {column} FROM {table}"))).all())
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    with pytest.raises(CatalogError):
        await tasks.credentials(request, **authority)


async def test_provider_io_releases_locks_and_rechecks_revocation(credential_task, monkeypatch, access_db):
    import threading

    from firefly_weave.definitions.models import CatalogError

    tasks, workers, authority, grant, request, run = credential_task
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "credential-canary")
    await workers.grant_connection(authority["actor"], authority["scope"], grant, context=authority["context"])
    started, finish = threading.Event(), threading.Event()
    original = tasks.secrets.resolve

    def resolve(scope, handle):
        started.set()
        assert finish.wait(3)
        return original(scope, handle)

    monkeypatch.setattr(tasks.secrets, "resolve", resolve)
    pending = asyncio.create_task(tasks.credentials(request, **authority))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        async with access_db[1].begin() as tx:
            await tx.execute(text("SELECT id FROM runs WHERE id=:id FOR UPDATE NOWAIT"), {"id": run.id})
            await tx.execute(text("UPDATE worker_instances SET revoked=true"))
        finish.set()
        with pytest.raises(CatalogError):
            await pending
    finally:
        finish.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_generation_fences_old_attempt_and_claim_capacity(
    worker_setup, queued_task, task_service, transaction_factory, worker_ids, access_db
):
    from firefly_weave.definitions.models import CatalogError

    async with transaction_factory() as tx:
        old = (await task_service.claim(tx, worker_ids[0], 1))[0]
    # Simulate the retry policy's authorized requeue decision; lease handling itself never makes this decision.
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second',status='expired'")
        )
        await tx.execute(text("UPDATE task_intents SET status='ready'"))
    async with transaction_factory() as tx:
        new = (await task_service.claim(tx, worker_ids[1], 1))[0]
        assert new.proof.generation == old.proof.generation + 1
        assert new.proof.token != old.proof.token and new.operation_key == old.operation_key
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, old.proof, uuid4(), 99)
    async with transaction_factory() as tx:
        await task_service.complete(tx, new.proof, uuid4(), 42)


async def test_input_oversize_leaves_no_task(worker_setup, access_db):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError

    _, runtime, _, actor, scope, activation, _ = worker_setup
    with pytest.raises((ValueError, CatalogError)):
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input="x" * (1024 * 1024 + 1)),
            "oversize",
            context=AuditContext(),
        )
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM task_intents")) == 0


async def test_claim_preserves_capacity_across_concurrent_requests(
    worker_setup, queued_task, task_service, transaction_factory, worker_ids
):
    from firefly_weave.contracts.runtime import StartRunRequest

    _, runtime, _, actor, scope, activation, _ = worker_setup
    await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=4), "second", context=AuditContext()
    )

    async def claim():
        async with transaction_factory() as tx:
            return await task_service.claim(tx, worker_ids[0], 2)

    results = await asyncio.gather(claim(), claim())
    assert sum(len(batch) for batch in results) == 1


@pytest.mark.parametrize("mismatch", ["scope", "run", "node", "missing_admission", "late_admission"])
async def test_internal_verified_port_rejects_mismatched_context(
    mismatch, worker_setup, queued_task, task_service, transaction_factory, worker_ids
):
    from dataclasses import replace

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.workers.leases import _TaskOperation

    tasks, runtime, workers, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with transaction_factory() as tx:
        verified = await _TaskOperation(
            actor, scope, AuditContext(), workers, runtime, tasks.connections, tasks.secrets
        ).check(tx, lease.proof, "task.complete")
        if mismatch in {"missing_admission", "late_admission"}:
            verified = replace(
                verified, checked_at=None if mismatch == "missing_admission" else verified.attempt["deadline"]
            )
            with pytest.raises(AccessDenied):
                await runtime.finish_verified_task(tx, verified, uuid4(), 4)
            return
        task = dict(verified.task)
        task[{"scope": "tenant_id", "run": "run_id", "node": "node_id"}[mismatch]] = (
            uuid4() if mismatch != "node" else "other-node"
        )
        with pytest.raises(AccessDenied):
            await runtime.finish_verified_task(tx, replace(verified, task=task), uuid4(), 4)


async def test_compatible_task_not_starved_by_incompatible_page(
    worker_setup, worker_runtime_fixture, access_db, monkeypatch
):
    import json
    from uuid import UUID

    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest

    tasks, runtime, workers, actor, scope, activation, _ = worker_setup
    definitions = workers.definitions
    echo = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(image_digest="sha256:" + "d" * 64, capabilities=[echo, {**echo, "taskType": "other"}]),
        context=AuditContext(),
    )
    echo_activation = await definitions.activate(
        actor,
        scope,
        activation.request.model_copy(update={"worker_release_ids": {"echo": release.id}}),
        "mixed-echo",
        1,
        context=AuditContext(),
    )
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["metadata"]["name"] = "other-action"
    action["spec"]["implementation"]["taskType"] = "other"
    await definitions.publish(
        actor, scope, "Action", json.dumps(action), "json", "other-action", context=AuditContext()
    )
    flow = json.loads(worker_runtime_fixture["source"])
    flow["metadata"]["name"] = "other-flow"
    flow["spec"]["steps"][0]["uses"] = "other-action@2.0.0"
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(flow), "json", "other-flow", context=AuditContext()
    )
    other_activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={"other": release.id},
        ),
        "mixed-other",
        context=AuditContext(),
    )
    from firefly_weave.runtime import repository

    identifiers = iter([UUID(int=index) for index in range(1, 102)] + [UUID(int=2**127)])
    monkeypatch.setattr(repository, "uuid4", lambda: next(identifiers))
    for index in range(101):
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=other_activation.id, input=index),
            f"other-{index}",
            context=AuditContext(),
        )
    await runtime.start(
        actor, scope, StartRunRequest(activation_id=echo_activation.id, input=999), "eligible", context=AuditContext()
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=["echo@1.2.0"], capacity=1),
        context=AuditContext(),
    )
    async with definitions.transaction(scope, None) as tx:
        claimed = await tasks.claim(tx, instance.id, 1, actor=actor, scope=scope, context=AuditContext())
    assert len(claimed) == 1 and claimed[0].input == 999
