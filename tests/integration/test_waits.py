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

"""Durable signal arbitration on actual PostgreSQL and native HTTP."""

import asyncio

import pytest
import test_definitions as catalog_tests
from sqlalchemy import text

from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration
author = catalog_tests.author

SOURCE = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: approval, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {type: object}
  timeoutSeconds: 600
  steps:
    - id: approval
      kind: signal
      name: customer-approved
      timeoutSeconds: 300
      payloadSchema: {type: object, properties: {approved: {type: boolean}}, required: [approved]}
  output: {ref: /steps/approval/output}
"""


@pytest.fixture
async def waiting_run(author, access_db, provisioned, headers, project_url, env_url, request):
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    response = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "signal-flow"},
        json={"format": "yaml", "source": getattr(request, "param", SOURCE)},
    )
    assert response.status_code == 201, response.text
    published = response.json()
    response = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "signal-activation"},
        json={
            "version_id": published["id"],
            "artifact_digest": published["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    response = await author[0].post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "signal-run"},
        json={"activation_id": response.json()["id"], "input": {}},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"]["status"] == "waiting"
    return response.json()["id"]


EVENT = {"eventId": "approval-1", "name": "customer-approved", "payload": {"approved": True}}


async def test_duplicate_terminal_retry_and_conflict(client, headers, env_url, waiting_run, access_db):
    url = f"{env_url}/runs/{waiting_run}/signals"
    results = await asyncio.gather(*(client.post(url, headers=headers, json=EVENT) for _ in range(3)))
    assert all(r.status_code in {202, 429} for r in results), [r.text for r in results]
    assert any(r.status_code == 202 for r in results)
    results = [await client.post(url, headers=headers, json=EVENT) if r.status_code == 429 else r for r in results]
    assert all(r.status_code == 202 for r in results), [r.text for r in results]
    assert results[0].json() == results[1].json() == results[2].json()
    assert (await client.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()["state"]["status"] == "succeeded"
    assert (await client.post(url, headers=headers, json={**EVENT, "payload": {"approved": False}})).status_code == 409
    assert (await client.post(url, headers=headers, json={**EVENT, "eventId": "new"})).status_code == 409
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM signal_receipts")) == 1
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1


async def test_invalid_signal_is_not_acknowledged(client, headers, env_url, waiting_run):
    url = f"{env_url}/runs/{waiting_run}/signals"
    for event in ({**EVENT, "name": "unknown"}, {**EVENT, "payload": {"approved": "wrong"}}):
        assert (await client.post(url, headers=headers, json=event)).status_code == 422


async def test_no_early_wakeup_then_timeout(services, client, headers, env_url, waiting_run, author, access_db):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    service = services(access_db[0]).resolve(DeadlineService)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await service.tick(tx, 100) == 0
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await service.tick(tx, 100) == 1
        assert await service.tick(tx, 100) == 0
    assert (await client.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()["state"]["status"] == "timed_out"


async def test_timeout_races_signal_once(
    services, client, headers, env_url, waiting_run, author, access_db, replica_apps
):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))

    async def tick():
        context = replica_apps[1].state.pyfly.context
        async with context.get_bean(UnitOfWork).open(author[2]) as tx:
            return await context.get_bean(DeadlineService).tick(tx, 100)

    response, _ = await asyncio.gather(
        client.post(f"{env_url}/runs/{waiting_run}/signals", headers=headers, json=EVENT), tick()
    )
    assert response.status_code in {202, 409}
    assert (await client.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()["state"]["status"] == "timed_out"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1


@pytest.mark.parametrize(
    "waiting_run",
    [
        SOURCE.replace(
            "  output: {ref:",
            "    - {id: second, kind: signal, name: second-signal, timeoutSeconds: 300, "
            "payloadSchema: {type: object}}\n  output: {ref:",
        )
    ],
    indirect=True,
)
async def test_early_signal_consumed_once(client, headers, env_url, waiting_run, access_db):
    url = f"{env_url}/runs/{waiting_run}/signals"
    early = {"eventId": "early", "name": "second-signal", "payload": {"early": True}}
    assert (await client.post(url, headers=headers, json=early)).status_code == 202
    view = (await client.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()
    assert view["state"]["active"] == ["approval"]
    assert (await client.post(url, headers=headers, json=EVENT)).status_code == 202
    view = (await client.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()
    assert view["state"]["status"] == "succeeded"
    assert view["state"]["steps"]["second"]["output"] == {"early": True}
    assert (await client.post(url, headers=headers, json=early)).status_code == 202
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM signal_receipts WHERE consumed")) == 2
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 2


async def test_signal_authority_precedes_terminal_replay(
    services, client, headers, env_url, waiting_run, access_db, author
):
    from uuid import UUID

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.signals import SignalService

    url = f"{env_url}/runs/{waiting_run}/signals"
    assert (await client.post(url, headers=headers, json=EVENT)).status_code == 202
    stale = await access_db[2].load_principal(author[1].id)
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='operator'"), {"id": stale.id}
        )
    service = services(access_db[0]).resolve(SignalService)
    with pytest.raises(AccessDenied):
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            await service.deliver(
                tx,
                UUID(waiting_run),
                EVENT["eventId"],
                EVENT["name"],
                EVENT["payload"],
                actor=stale,
                scope=author[2],
                context=AuditContext(),
            )


async def test_signal_caller_rollback_removes_acknowledgment_and_transition(services, access_db, author, waiting_run):
    from uuid import UUID

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.signals import SignalService

    service = services(access_db[0]).resolve(SignalService)
    actor = await access_db[2].load_principal(author[1].id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            await service.deliver(
                tx,
                UUID(waiting_run),
                EVENT["eventId"],
                EVENT["name"],
                EVENT["payload"],
                actor=actor,
                scope=author[2],
                context=AuditContext(),
            )
            assert await tx.session.scalar(text("SELECT count(*) FROM signal_receipts")) == 1
            raise RuntimeError("rollback")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM signal_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 0
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "waiting"


@pytest.mark.parametrize(
    "waiting_run",
    [
        """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: restricted-wait, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  timeoutSeconds: 600
  steps:
    - {id: pause, kind: wait, durationSeconds: 300}
  output: {literal: done}
"""
    ],
    indirect=True,
)
async def test_restricted_deadline_scan_never_runs_duration_continuation(services, waiting_run, author, access_db):
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    service = services(access_db[0]).resolve(DeadlineService)
    async with access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id<>'@run'")
        )
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        report = await service.scan(tx, 10, terminal_only=True)
        assert report.elapsed == report.timed_out == 0
    async with access_db[1].begin() as owner:
        assert await owner.scalar(text("SELECT state->>'status' FROM runs")) == "waiting"
        assert await owner.scalar(text("SELECT count(*) FROM wait_wakeups")) == 0
        await owner.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='@run'")
        )
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        report = await service.scan(tx, 10, terminal_only=True)
        assert report.elapsed == 0 and report.timed_out == 1
    async with access_db[1]() as owner:
        state = await owner.scalar(text("SELECT state FROM runs"))
        assert state["status"] == "timed_out" and not state.get("unavailable", False)
