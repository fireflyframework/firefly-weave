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

"""Scoped run creation, activation pins, accepted events, and rollback behavior."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
import test_definitions as catalog_tests
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration
author = catalog_tests.author
publication_request = catalog_tests.publication_request


@pytest.fixture
async def start_request(author, access_db, provisioned, headers, project_url, env_url, publication_request):
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    published = await author[0].post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "flow"},
        json={
            **publication_request,
            "source": publication_request["source"].replace("inputSchema: {}", "inputSchema: {type: integer}"),
        },
    )
    assert published.status_code == 201, published.text
    activated = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert activated.status_code == 201, activated.text
    return {"activation_id": activated.json()["id"], "input": 3}


async def test_duplicate_start_returns_same_run(client, headers, env_url, start_request, access_db):
    h = {**headers, "Idempotency-Key": "order-42"}

    async def attempt():
        return await catalog_tests.retry_capacity(lambda: client.post(env_url + "/runs", headers=h, json=start_request))

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert all(r.status_code in {200, 201} for r in results), [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    assert results[0].json()["state"]["output"] == 7
    different = await client.post(env_url + "/runs", headers=h, json={**start_request, "input": 4})
    assert different.status_code == 409
    async with access_db[1]() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM runs")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM run_events")) == 1


async def test_invalid_input_leaves_no_rows(client, headers, env_url, start_request, access_db):
    response = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "bad"}, json={**start_request, "input": "wrong"}
    )
    assert response.status_code == 422
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 0


async def test_caller_rollback_is_atomic(services, author, access_db, start_request):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.service import RuntimeService

    sessions, owner, access, _ = access_db
    actor = await access.load_principal(author[1].id)
    service = RuntimeService(services(sessions).resolve(DefinitionService))
    with pytest.raises(RuntimeError, match="rollback"):
        async with UnitOfWork(sessions).open(author[2]) as tx:
            result = await service.start(
                actor,
                author[2],
                StartRunRequest.model_validate_json(json.dumps(start_request)),
                "rollback",
                context=AuditContext(),
                tx=tx,
            )
            async with owner() as observer:
                assert await observer.scalar(text("SELECT count(*) FROM runs")) == 0
            assert result.state.status == "succeeded"
            raise RuntimeError("rollback")
    async with owner() as observer:
        for table in ("runs", "run_events", "step_instances", "task_intents", "run_deadlines"):
            assert await observer.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_no_generic_public_apply(client, headers, env_url, start_request):
    response = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "run"}, json=start_request)
    assert response.status_code == 201, response.text
    forged = await client.post(
        env_url + "/runs/" + response.json()["id"] + "/apply",
        headers=headers,
        json={"type": "task_completed", "output": 9},
    )
    assert forged.status_code == 404


async def test_operator_without_catalog_read_and_revoked_replay(services, author, access_db, start_request):
    import json

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.service import RuntimeService

    actor, scope = author[1:]
    operator = actor.model_copy(update={"grants": (Grant(role="operator", scope=scope),)})
    service = RuntimeService(services(access_db[0]).resolve(DefinitionService))
    request = StartRunRequest.model_validate_json(json.dumps(start_request))
    view = await service.start(operator, scope, request, "operator", context=AuditContext())
    assert view.state.status == "succeeded"
    revoked = operator.model_copy(update={"grants": ()})
    with pytest.raises(AccessDenied):
        await service.start(revoked, scope, request, "operator", context=AuditContext())
    with pytest.raises(AccessDenied):
        await service.start(
            operator,
            scope.model_copy(update={"environment_id": uuid4()}),
            request,
            "operator",
            context=AuditContext(),
        )


async def test_activation_update_preserves_run_pin(author, client, headers, env_url, start_request):
    created = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "pin"}, json=start_request)
    assert created.status_code == 201
    activation = created.json()["activation"]
    updated = await client.post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "new-revision", "If-Match": '"1"'},
        json=activation["request"],
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["id"] != activation["id"]
    read = await client.get(env_url + "/runs/" + created.json()["id"], headers=headers)
    assert read.json() == created.json()
    historical = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "historical"}, json=start_request
    )
    assert historical.status_code == 201


async def test_event_ids_sequence_and_untrusted_completion(services, author, access_db, start_request):
    import json

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.models import RuntimeEvent
    from firefly_weave.runtime.service import RuntimeService

    sessions, owner, access, _ = access_db
    actor = await access.load_principal(author[1].id)
    service = RuntimeService(services(sessions).resolve(DefinitionService))
    view = await service.start(
        actor,
        author[2],
        StartRunRequest.model_validate_json(json.dumps(start_request)),
        "event",
        context=AuditContext(),
    )
    async with owner() as session:
        row = (
            (
                await session.execute(
                    text("SELECT id,created_at,sequence,data FROM run_events WHERE run_id=:id"), {"id": view.id}
                )
            )
            .mappings()
            .one()
        )
    assert row["sequence"] == 1
    accepted = RuntimeEvent(id=row["id"], type="started", timestamp=row["created_at"], data=row["data"])
    assert await service.apply(actor, author[2], view.id, accepted, context=AuditContext()) == view
    with pytest.raises(CatalogError, match="another payload"):
        await service.apply(
            actor, author[2], view.id, accepted.model_copy(update={"data": {"changed": True}}), context=AuditContext()
        )
    with pytest.raises(AccessDenied):
        await service.apply(
            actor, author[2], view.id, accepted.model_copy(update={"type": "task_completed"}), context=AuditContext()
        )


async def test_unsupported_ir_and_worker_admission_fail_closed(
    services, author, access_db, start_request, monkeypatch, worker_runtime_fixture
):
    import json

    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.service import RuntimeService

    definitions = services(access_db[0]).resolve(DefinitionService)
    actor = await access_db[2].load_principal(author[1].id)
    async with definitions.transaction(author[2], None) as tx:
        activation, envelope = await definitions.runtime_snapshot(
            actor, author[2], UUID(start_request["activation_id"]), context=AuditContext(), tx=tx
        )
    original = json.loads(json.dumps(envelope))
    for artifact in (
        {**original, "executable": {**original["executable"], "irVersion": "unsupported"}},
        json.loads(worker_runtime_fixture["artifact"].to_bytes()),
    ):

        async def snapshot(*args, artifact=artifact, **kwargs):
            return activation, artifact

        monkeypatch.setattr(definitions, "runtime_snapshot", snapshot)
        with pytest.raises(CatalogError):
            await RuntimeService(definitions).start(
                actor,
                author[2],
                StartRunRequest.model_validate_json(json.dumps(start_request)),
                "unsupported",
                context=AuditContext(),
            )
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


@pytest.fixture
async def admitted_runtime_fixture(services, author, access_db, worker_runtime_fixture):
    from firefly_weave.contracts.workers import ReleaseRequest
    from firefly_weave.workers.service import WorkerService

    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = (
        await services(access_db[0])
        .resolve(WorkerService)
        .register_release(
            author[1],
            author[2],
            ReleaseRequest(image_digest="sha256:" + "a" * 64, capabilities=[contract]),
            context=AuditContext(),
        )
    )
    return {**worker_runtime_fixture, "release_id": release.id}


async def test_durable_task_intents_rollback_and_restart_scan(
    services, author, access_db, start_request, admitted_runtime_fixture
):
    worker_runtime_fixture = admitted_runtime_fixture
    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.contracts.runtime import RunView, StartRunRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import event_hash

    sessions, owner, access, _ = access_db
    actor = await access.load_principal(author[1].id)
    definitions = services(sessions).resolve(DefinitionService)
    async with definitions.transaction(author[2], None) as tx:
        activation, _ = await definitions.runtime_snapshot(
            actor, author[2], UUID(start_request["activation_id"]), context=AuditContext(), tx=tx
        )
    # This is a trusted storage fixture, not a production activation/readiness bypass.
    activation = activation.model_copy(
        update={
            "request": activation.request.model_copy(
                update={"worker_release_ids": {"echo": worker_runtime_fixture["release_id"]}}
            )
        }
    )
    flow = worker_runtime_fixture["artifact"]
    accepted = RuntimeEvent(id=uuid4(), type="started", timestamp=datetime(2026, 9, 29, tzinfo=UTC), sequence=1)
    transition_result = transition(RunState(input=3), accepted, flow)
    view = RunView(id=uuid4(), activation=activation, artifact_digest=flow.digest, state=transition_result.state)
    request = StartRunRequest.model_validate_json(json.dumps(start_request))
    for rollback in (True, False):
        try:
            async with UnitOfWork(sessions).open(author[2]) as tx:
                repository = RuntimeRepository(tx, services(sessions).resolve(DefinitionService).outbox)
                from firefly_weave.operations.facts import RunStartFacts

                await repository.insert(
                    view,
                    json.loads(flow.to_bytes()),
                    request,
                    actor.id,
                    start_facts=RunStartFacts(),
                    at=accepted.timestamp,
                )
                await repository.persist(view, accepted, transition_result, event_hash(accepted))
                assert len(await repository.ready()) == 1
                async with UnitOfWork(sessions).open(author[2], mutation=False) as observer:
                    assert await RuntimeRepository(observer).ready() == []
                if rollback:
                    raise RuntimeError("rollback")
        except RuntimeError:
            pass
        async with owner() as observer:
            for table in ("runs", "run_events", "step_instances", "task_intents"):
                assert await observer.scalar(text(f"SELECT count(*) FROM {table}")) == (0 if rollback else 1)
    # Fresh scanner/session discovers committed work without an in-memory notification.
    async with UnitOfWork(sessions).open(author[2]) as tx:
        tasks = await RuntimeRepository(tx).ready()
        assert tasks[0]["worker_release_id"] == worker_runtime_fixture["release_id"]
        assert tasks[0]["payload"]["action_reference"] == "echo-action@2.0.0"
        assert tasks[0]["payload"]["task_version"] == "1.2.0"
        assert tasks[0]["operation_key"] == f"{view.id}:work"


async def test_runtime_tables_force_rls_and_immutable_pins(access_db):
    sessions, owner, _, _ = access_db
    async with owner() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('runs','run_events','task_intents','step_instances','run_deadlines')"
                )
            )
        ).all()
        assert len(rows) == 5
        assert all(row[1] and row[2] for row in rows)
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert not await session.scalar(text("SELECT has_column_privilege(current_user,'runs','artifact','UPDATE')"))
        assert not await session.scalar(text("SELECT has_table_privilege(current_user,'run_events','UPDATE')"))


async def test_rollback_between_event_and_task_insert(
    services, author, access_db, start_request, admitted_runtime_fixture, monkeypatch
):
    worker_runtime_fixture = admitted_runtime_fixture
    from datetime import UTC, datetime

    from firefly_weave.contracts.runtime import RunView, StartRunRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.facts import RunStartFacts
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import event_hash

    sessions, owner, access, _ = access_db
    actor = await access.load_principal(author[1].id)
    definitions = services(sessions).resolve(DefinitionService)
    async with definitions.transaction(author[2], None) as tx:
        activation, _ = await definitions.runtime_snapshot(
            actor, author[2], UUID(start_request["activation_id"]), context=AuditContext(), tx=tx
        )
    activation = activation.model_copy(
        update={
            "request": activation.request.model_copy(
                update={"worker_release_ids": {"echo": worker_runtime_fixture["release_id"]}}
            )
        }
    )
    artifact = worker_runtime_fixture["artifact"]
    event = RuntimeEvent(id=uuid4(), type="started", timestamp=datetime.now(UTC), sequence=1)
    result = transition(RunState(input=5), event, artifact)
    view = RunView(id=uuid4(), activation=activation, artifact_digest=artifact.digest, state=result.state)
    with pytest.raises(RuntimeError, match="injected"):
        async with UnitOfWork(sessions).open(author[2]) as tx:
            repository = RuntimeRepository(tx, services(sessions).resolve(DefinitionService).outbox)
            original = repository.execute

            async def injected(sql, **values):
                if "INSERT INTO task_intents" in sql:
                    assert await tx.session.scalar(text("SELECT count(*) FROM run_events")) == 1
                    raise RuntimeError("injected")
                await original(sql, **values)

            monkeypatch.setattr(repository, "execute", injected)
            await repository.insert(
                view,
                json.loads(artifact.to_bytes()),
                StartRunRequest.model_validate_json(json.dumps(start_request)),
                actor.id,
                start_facts=RunStartFacts(),
                at=event.timestamp,
            )
            await repository.persist(view, event, result, event_hash(event))
    async with owner() as session:
        for table in ("runs", "run_events", "step_instances", "task_intents"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_retirement_blocks_new_start_but_preserves_existing_run(client, headers, env_url, start_request):
    started = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "retirement"}, json=start_request
    )
    assert started.status_code == 201
    retired = await client.post(
        env_url + "/activations/" + start_request["activation_id"] + "/retire",
        headers={**headers, "Idempotency-Key": "retire", "If-Match": '"1"'},
        json={},
    )
    assert retired.status_code == 200, retired.text
    rejected = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "new"}, json=start_request)
    assert rejected.status_code == 422
    retry = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "retirement"}, json=start_request
    )
    assert retry.json() == started.json()
    read = await client.get(env_url + "/runs/" + started.json()["id"], headers=headers)
    assert read.json() == started.json()


@pytest.mark.parametrize("duration", [9_007_199_254_740_991, 1_000_000_000_000])
async def test_public_start_persists_deadline_range_incident(
    author, client, headers, project_url, env_url, start_request, publication_request, access_db, duration
):
    source = publication_request["source"].replace("name: pure", "name: deadline-overflow")
    source = source.replace("spec:\n", f"spec:\n  timeoutSeconds: {duration}\n")
    published = await client.post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "overflow-flow"},
        json={"format": "yaml", "source": source},
    )
    assert published.status_code == 201, published.text
    activated = await client.post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "overflow-activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert activated.status_code == 201, activated.text
    request = {"activation_id": activated.json()["id"], "input": {}}
    started = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "overflow-run"}, json=request)
    assert started.status_code == 201, started.text
    assert started.json()["state"]["status"] == "suspended"
    assert started.json()["state"]["incident"] == "WV-RUNTIME-DEADLINE-RANGE"
    replay = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "overflow-run"}, json=request)
    assert replay.json() == started.json()
    async with access_db[1]() as session:
        for table in ("task_intents", "run_deadlines", "step_instances"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 1


async def test_restricted_catalog_blocks_new_service_admission_but_preserves_replay_and_read(
    services, author, access_db, start_request
):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.service import RuntimeService

    service = RuntimeService(services(access_db[0]).resolve(DefinitionService))
    actor, scope = author[1:]
    actor = await access_db[2].load_principal(actor.id)
    request = StartRunRequest.model_validate_json(json.dumps(start_request))
    first = await service.start(actor, scope, request, "before-restriction", context=AuditContext())
    service.definitions.registry.set_operational_guard(lambda: False)
    assert (await service.read(actor, scope, first.id, context=AuditContext())).id == first.id
    assert (await service.start(actor, scope, request, "before-restriction", context=AuditContext())).id == first.id
    with pytest.raises(CatalogError) as rejected:
        await service.start(actor, scope, request, "after-restriction", context=AuditContext())
    assert rejected.value.code == "WV-COMPATIBILITY"
