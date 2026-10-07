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

"""Real PostgreSQL presence observations, scoped drain fences, and lease completion."""

import asyncio
from uuid import uuid4

import pytest
import test_connections
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.workers import WorkerControlRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import UnitOfWork

pytestmark = pytest.mark.integration
connections = test_connections.connections


async def operator(worker_setup, access_db, provisioned, resources=()):
    actor, scope = worker_setup[3:5]
    access = access_db[2]
    await access.grant(provisioned[0], actor.id, Grant(role="worker_operator", scope=scope, resources=resources))
    return await access.load_principal(actor.id)


async def test_registration_is_unknown_and_empty_poll_is_recent(worker_setup, task_service, transaction_factory):
    _, _, workers, actor, scope, _, instances = worker_setup
    before = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    assert before.presence == "unknown" and before.available_capacity is None
    assert before.last_seen_at is None and before.revision == 1
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, instances[0].id, 1) == []
    seen = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    assert seen.presence == "recent" and seen.available_capacity == 1 and seen.active_leases == 0
    assert seen.last_seen_at <= seen.observed_at < seen.presence_expires_at
    untouched = await workers.read(actor, scope, instances[1].id, context=AuditContext())
    assert untouched.presence == "unknown"


async def test_stale_capacity_and_live_heartbeat_refresh_exact_owner(
    worker_setup, queued_task, task_service, transaction_factory, access_db
):
    _, _, workers, actor, scope, _, instances = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, instances[0].id, 1))[0]
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '61 seconds' WHERE id=:id"),
            {"id": instances[0].id},
        )
    stale = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    assert stale.presence == "stale" and stale.available_capacity is None and stale.active_leases == 1
    async with transaction_factory() as tx:
        await task_service.heartbeat(tx, lease.proof)
    seen = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    assert seen.presence == "recent" and seen.available_capacity == 0 and seen.active_leases == 1
    assert (await workers.read(actor, scope, instances[1].id, context=AuditContext())).presence == "unknown"


async def test_drain_preserves_current_lease_and_resume_reopens_capacity(
    worker_setup, queued_task, task_service, transaction_factory, access_db, provisioned
):
    _, runtime, workers, _, scope, activation, instances = worker_setup
    actor = await operator(worker_setup, access_db, provisioned)
    worker = instances[0].id
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker, 1))[0]
    command = WorkerControlRequest(expected_revision=1)
    drained = await workers.control(actor, scope, worker, command, "drain", draining=True, context=AuditContext())
    assert drained.draining and drained.revision == 2 and drained.active_leases == 1
    assert drained.available_capacity == 0
    assert (
        await workers.control(actor, scope, worker, command, "drain", draining=True, context=AuditContext()) == drained
    )
    await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=5), "second", context=AuditContext()
    )
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker, 1) == []
        await task_service.heartbeat(tx, lease.proof)
        assert (await task_service.complete(tx, lease.proof, uuid4(), 42)).status == "completed"
    with pytest.raises(CatalogError, match="changed"):
        await workers.control(actor, scope, worker, command, "stale", draining=False, context=AuditContext())
    resumed = await workers.control(
        actor,
        scope,
        worker,
        WorkerControlRequest(expected_revision=2),
        "resume",
        draining=False,
        context=AuditContext(),
    )
    assert not resumed.draining and resumed.revision == 3 and resumed.available_capacity == 1
    async with transaction_factory() as tx:
        assert len(await task_service.claim(tx, worker, 1)) == 1


async def test_control_current_grant_exact_resource_and_scope(worker_setup, access_db, provisioned):
    _, _, workers, actor, scope, _, instances = worker_setup
    request = WorkerControlRequest(expected_revision=1)
    with pytest.raises(AccessDenied):
        await workers.control(actor, scope, instances[0].id, request, "denied", draining=True, context=AuditContext())
    actor = await operator(worker_setup, access_db, provisioned, (str(instances[0].id),))
    with pytest.raises(AccessDenied):
        await workers.control(actor, scope, instances[1].id, request, "other", draining=True, context=AuditContext())
    await workers.control(actor, scope, instances[0].id, request, "ok", draining=True, context=AuditContext())
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": actor.id})
    with pytest.raises(AuthenticationFailed):
        await workers.control(actor, scope, instances[0].id, request, "ok", draining=True, context=AuditContext())
    # Same tenant, a different environment: database policy must reject even raw inventory SQL.
    alternate = scope.model_copy(update={"environment_id": uuid4()})
    async with UnitOfWork(access_db[0]).open(alternate) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM worker_instances")) == 0


async def test_claim_and_drain_serialize_without_revoking_winning_lease(
    worker_setup, queued_task, task_service, transaction_factory, access_db, provisioned
):
    _, _, workers, _, scope, _, instances = worker_setup
    actor = await operator(worker_setup, access_db, provisioned)
    worker = instances[0].id

    async def claim():
        async with transaction_factory() as tx:
            return await task_service.claim(tx, worker, 1)

    leases, drained = await asyncio.wait_for(
        asyncio.gather(
            claim(),
            workers.control(
                actor,
                scope,
                worker,
                WorkerControlRequest(expected_revision=1),
                "race",
                draining=True,
                context=AuditContext(),
            ),
        ),
        timeout=10,
    )
    assert drained.draining
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker, 1) == []
        if leases:
            assert (await task_service.complete(tx, leases[0].proof, uuid4(), 42)).status == "completed"


async def test_invalid_heartbeat_cannot_refresh_presence(
    worker_setup, queued_task, task_service, transaction_factory, access_db
):
    _, _, workers, actor, scope, _, instances = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, instances[0].id, 1))[0]
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '61 seconds' WHERE id=:id"),
            {"id": instances[0].id},
        )
    before = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.heartbeat(tx, lease.proof.model_copy(update={"token": "forged"}))
    after = await workers.read(actor, scope, instances[0].id, context=AuditContext())
    assert after.last_seen_at == before.last_seen_at and after.presence == "stale"


async def test_control_rejects_changed_replay_and_revoked_worker(worker_setup, access_db, provisioned):
    _, _, workers, _, scope, _, instances = worker_setup
    actor = await operator(worker_setup, access_db, provisioned)
    worker = instances[0].id
    await workers.control(
        actor, scope, worker, WorkerControlRequest(expected_revision=1), "one", draining=True, context=AuditContext()
    )
    with pytest.raises(CatalogError, match="another request"):
        await workers.control(
            actor,
            scope,
            worker,
            WorkerControlRequest(expected_revision=2),
            "one",
            draining=True,
            context=AuditContext(),
        )
    await workers.revoke(actor, scope, worker, context=AuditContext())
    with pytest.raises(CatalogError, match="revoked"):
        await workers.control(
            actor,
            scope,
            worker,
            WorkerControlRequest(expected_revision=3),
            "resume",
            draining=False,
            context=AuditContext(),
        )


async def test_worker_control_http_contract(
    client, authenticated_client, access_db, provisioned, headers, env_url, worker_setup
):
    _, _, _, _, scope, _, instances = worker_setup
    async with access_db[0]() as session:
        principal = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    access = access_db[2]
    await access.grant(provisioned[0], principal, Grant(role="viewer", scope=scope))
    url = env_url + "/workers/" + str(instances[0].id)
    read = await client.get(url, headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["presence"] == "unknown" and read.json()["available_capacity"] is None
    body = {"expected_revision": 1}
    denied = await client.post(url + "/drain", headers={**headers, "Idempotency-Key": "drain"}, json=body)
    assert denied.status_code == 403
    await access.grant(
        provisioned[0], principal, Grant(role="worker_operator", scope=scope, resources=(str(instances[0].id),))
    )
    missing = await client.post(url + "/drain", headers=headers, json=body)
    assert missing.status_code == 422
    drained = await client.post(url + "/drain", headers={**headers, "Idempotency-Key": "drain"}, json=body)
    assert drained.status_code == 200, drained.text
    assert drained.json()["draining"] and drained.json()["revision"] == 2
    assert drained.headers["cache-control"] == "no-store"
    resumed = await client.post(
        url + "/resume", headers={**headers, "Idempotency-Key": "resume"}, json={"expected_revision": 2}
    )
    assert resumed.status_code == 200 and not resumed.json()["draining"], resumed.text
    wrong = env_url + "/workers/" + str(instances[1].id) + "/drain"
    assert (await client.post(wrong, headers={**headers, "Idempotency-Key": "wrong"}, json=body)).status_code == 403


async def test_resource_limited_operator_can_read_only_its_worker(worker_setup, access_db, provisioned):
    _, _, workers, _, scope, _, instances = worker_setup
    access = access_db[2]
    identifier = await access.create_principal(provisioned[0], "application")
    await access.grant(
        provisioned[0], identifier, Grant(role="worker_operator", scope=scope, resources=(str(instances[0].id),))
    )
    actor = await access.load_principal(identifier)
    assert (await workers.read(actor, scope, instances[0].id, context=AuditContext())).presence == "unknown"
    with pytest.raises(AccessDenied):
        await workers.read(actor, scope, instances[1].id, context=AuditContext())
    with pytest.raises(AccessDenied):
        await workers.list(actor, scope, context=AuditContext())
