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

"""Scoped append-only history and source-pinned exports on real PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_history_export_and_concurrent_append(
    client, headers, env_url, queued_task, task_service, transaction_factory, worker_ids
):
    from firefly_weave.operations.history import HistoryService

    assert HistoryService
    url = f"{env_url}/runs/{queued_task.id}"
    first = await client.get(url + "/history?limit=1", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["high_water_sequence"] == 1
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.complete(tx, lease.proof, uuid4(), 9)
    export = await client.get(url + "/export", headers=headers)
    assert export.status_code == 200, export.text
    body = export.json()
    assert body["replay"]["status"] == "consistent", body
    assert body["replay"]["final_state"]["output"] == 9
    assert len(body["events"]) == 2 and "source" not in body
    replay = await client.get(url + "/replay", headers=headers)
    assert replay.json() == body["replay"]
    assert (await client.get(url + "/history", headers={})).status_code == 401


async def test_authority_cursor_and_immutable_evidence(
    queued_task, worker_setup, services, access_db, transaction_factory
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Principal
    from firefly_weave.operations.history import HistoryService

    history = services(access_db[0]).resolve(HistoryService)
    actor, scope = worker_setup[3], worker_setup[4]
    async with transaction_factory() as tx:
        with pytest.raises(AccessDenied):
            await history.page(
                tx,
                queued_task.id,
                None,
                10,
                actor=Principal(id=uuid4(), kind="human"),
                scope=scope,
                context=AuditContext(),
            )
        page = await history.page(tx, queued_task.id, None, 10, actor=actor, scope=scope, context=AuditContext())
        assert page.events[0].evidence is not None
        privileges = (
            await tx.session.execute(
                text(
                    "SELECT has_table_privilege(current_user,'run_event_evidence','UPDATE'), "
                    "has_table_privilege(current_user,'run_event_evidence','DELETE')"
                )
            )
        ).one()
        assert privileges == (False, False)


async def test_stable_cursor_excludes_new_events_and_rechecks_grants(
    queued_task, worker_setup, task_service, transaction_factory, worker_ids, access_db, services
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations.history import HistoryService

    history = services(access_db[0]).resolve(HistoryService)
    actor, scope = worker_setup[3:5]
    args = dict(actor=actor, scope=scope, context=AuditContext())
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="BUSINESS_REJECTED"))
    async with transaction_factory() as tx:
        first = await history.page(tx, queued_task.id, None, 1, **args)
    assert first.high_water_sequence == 2 and first.next_cursor
    async with transaction_factory() as tx:
        await worker_setup[1].cancel(tx, queued_task.id, "private operational reason", **args)
    async with transaction_factory() as tx:
        second = await history.page(tx, queued_task.id, first.next_cursor, 100, **args)
        assert [e.sequence for e in second.events] == [2] and second.next_cursor is None
        assert second.high_water_sequence == 2
        with pytest.raises(CatalogError, match="Cursor"):
            from firefly_weave.operations.history import decode

            decode(first.next_cursor, scope, uuid4(), 3)
        exported = await history.export(tx, queued_task.id, **args)
        assert exported.replay.status == "consistent", exported.replay
        assert "private operational reason" not in exported.model_dump_json()
    async with access_db[1].begin() as session:
        await session.execute(text("DELETE FROM role_bindings WHERE principal_id=:id"), {"id": actor.id})
    async with transaction_factory() as tx:
        for operation in (
            lambda: history.page(tx, queued_task.id, first.next_cursor, 1, **args),
            lambda: history.export(tx, queued_task.id, **args),
        ):
            with pytest.raises(AccessDenied):
                await operation()


async def test_source_formatting_is_pinned_and_history_reads_are_immutable(
    queued_task, worker_setup, worker_runtime_fixture, transaction_factory, services, access_db
):
    import json

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    actor, scope = worker_setup[3:5]
    args = dict(actor=actor, scope=scope, context=AuditContext())
    history = services(access_db[0]).resolve(HistoryService)
    definitions = worker_setup[1].definitions
    reformatted = json.dumps(json.loads(worker_runtime_fixture["source"]), indent=4)
    await definitions.publish(actor, scope, "Workflow", reformatted, "json", "format-again", context=AuditContext())
    async with transaction_factory() as tx:
        before = (
            await tx.session.execute(
                text(
                    "SELECT data,transition,response,evidence FROM run_events "
                    "JOIN run_event_evidence USING(tenant_id,project_id,environment_id,run_id,id,sequence) "
                    "WHERE run_id=:id"
                ),
                {"id": queued_task.id},
            )
        ).all()
        exported = await history.export(tx, queued_task.id, **args)
        assert exported.source_reference.source_hash == worker_runtime_fixture["artifact"].source_hash
        assert exported.source_reference.version_id == worker_setup[5].request.version_id
        assert exported.node_paths["work"] in exported.source_map
        assert exported.replay.status == "incomplete"
        after = (
            await tx.session.execute(
                text(
                    "SELECT data,transition,response,evidence FROM run_events "
                    "JOIN run_event_evidence USING(tenant_id,project_id,environment_id,run_id,id,sequence) "
                    "WHERE run_id=:id"
                ),
                {"id": queued_task.id},
            )
        ).all()
        assert before == after


async def test_ignored_late_receipt_is_separate_from_accepted_history(
    queued_task, worker_setup, task_service, worker_ids, transaction_factory, services, access_db
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    actor, scope = worker_setup[3:5]
    args = dict(actor=actor, scope=scope, context=AuditContext())
    history = services(access_db[0]).resolve(HistoryService)
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await worker_setup[1].cancel(tx, queued_task.id, "cancel", **args)
    async with transaction_factory() as tx:
        before = await history.export(tx, queued_task.id, **args)
        receipt = await task_service.complete(tx, lease.proof, uuid4(), 99)
    async with transaction_factory() as tx:
        after = await history.export(tx, queued_task.id, **args)
        assert after.events == before.events and after.replay == before.replay
        assert after.replay.status == "consistent"
        assert len(after.auxiliary_receipts) == 1
        assert after.auxiliary_receipts[0].receipt_id == receipt.completion_id
        assert after.auxiliary_receipts[0].status == "ignored"
        assert "accepted_output_hash" not in after.model_dump_json()


async def test_sidecar_fk_and_rollback_are_atomic(queued_task, transaction_factory, access_db):
    from sqlalchemy.exc import DBAPIError

    async with transaction_factory() as tx:
        original = (
            (
                await tx.session.execute(
                    text("SELECT * FROM run_event_evidence WHERE run_id=:id"), {"id": queued_task.id}
                )
            )
            .mappings()
            .one()
        )
        for column, value in (("id", uuid4()), ("sequence", 999), ("environment_id", uuid4())):
            values = {**original, column: value}
            with pytest.raises(DBAPIError):
                async with tx.session.begin_nested():
                    await tx.session.execute(
                        text(
                            "INSERT INTO run_event_evidence VALUES(:tenant_id,:project_id,"
                            ":environment_id,:run_id,:id,:sequence,cast(:evidence AS jsonb))"
                        ),
                        {**values, "evidence": "{}"},
                    )
        assert (
            await tx.session.execute(
                text("SELECT count(*) FROM run_event_evidence WHERE run_id=:id"), {"id": queued_task.id}
            )
        ).scalar_one() == 1


async def test_legacy_missing_facts_export_is_honestly_incomplete(
    queued_task, transaction_factory, worker_setup, access_db, services
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    # Fixture models a pre-C5 run: no accepted row is amended by the product.
    async with access_db[1].begin() as session:
        await session.execute(text("DELETE FROM run_event_evidence WHERE run_id=:id"), {"id": queued_task.id})
    async with transaction_factory() as tx:
        result = (
            await services(access_db[0])
            .resolve(HistoryService)
            .export(tx, queued_task.id, actor=worker_setup[3], scope=worker_setup[4], context=AuditContext())
        )
        assert result.events[0].evidence is None
        assert result.replay.status == "incomplete" and result.replay.last_verified_sequence == 0
        assert result.source_reference is None


async def test_incident_resolution_receipt_and_export_remain_immutable(
    queued_task, task_service, worker_setup, transaction_factory, worker_ids, services, access_db
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.operations.history import HistoryService
    from firefly_weave.operations.incidents import IncidentService

    args = dict(actor=worker_setup[3], scope=worker_setup[4], context=AuditContext())
    graph = services(access_db[0])
    incidents, history = graph.resolve(IncidentService), graph.resolve(HistoryService)
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="BUSINESS_REJECTED"))
        incident = (await incidents.list(tx, queued_task.id, **args))[0]
        receipt = await incidents.resolve(
            tx,
            incident.id,
            IncidentResolution(
                receipt_id=uuid4(),
                kind="accept_reconciled_result",
                output=11,
                reason="confidential reason",
                evidence_reference="private ref",
            ),
            incident.revision,
            **args,
        )
    async with transaction_factory() as tx:
        before = (await tx.session.execute(text("SELECT * FROM incident_resolution_receipts"))).all()
        exported = await history.export(tx, queued_task.id, **args)
        assert exported.replay.status == "consistent", exported.replay
        assert exported.replay.final_state.output == 11
        assert exported.events[-1].data["receipt_id"] == str(receipt.resolution.receipt_id)
        assert (
            "confidential reason" not in exported.model_dump_json() and "private ref" not in exported.model_dump_json()
        )
        assert (await tx.session.execute(text("SELECT * FROM incident_resolution_receipts"))).all() == before


async def test_export_budget_returns_prefix_and_never_embeds_source(
    queued_task, worker_setup, transaction_factory, access_db, services, monkeypatch
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations import history as module

    monkeypatch.setattr(module, "MAX_BYTES", 64 * 1024)
    async with transaction_factory() as tx:
        result = (
            await services(access_db[0])
            .resolve(module.HistoryService)
            .export(tx, queued_task.id, actor=worker_setup[3], scope=worker_setup[4], context=AuditContext())
        )
        assert result.bounded_prefix and result.events == []
        assert result.replay.status == "incomplete"
        assert result.replay.diagnostics[0].code == "WV-REPLAY-LIMIT"
        assert result.replay.last_verified_sequence == 0


async def test_unavailable_legacy_never_exports_sidecar_input(
    queued_task, worker_setup, transaction_factory, access_db, services
):
    import json

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    async with access_db[1].begin() as session:
        # Retained policy-blocked snapshot; product reads must not trust the sidecar as a disclosure bypass.
        await session.execute(
            text("UPDATE runs SET state=state || cast(:state AS jsonb) WHERE id=:id"),
            {"id": queued_task.id, "state": json.dumps({"unavailable": True})},
        )
        await session.execute(
            text(
                "UPDATE run_event_evidence SET evidence=jsonb_set(evidence,"
                "'{initial_input,value}',cast(:value AS jsonb)) WHERE run_id=:id"
            ),
            {"id": queued_task.id, "value": json.dumps("private-test-input")},
        )
    async with transaction_factory() as tx:
        result = (
            await services(access_db[0])
            .resolve(HistoryService)
            .export(tx, queued_task.id, actor=worker_setup[3], scope=worker_setup[4], context=AuditContext())
        )
        assert result.replay.status == "incomplete"
        assert "private-test-input" not in result.model_dump_json()
        assert result.events[0].evidence is None and result.events[0].data == {}


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    import json

    if getattr(request, "param", None) == "run_deadline":
        document = json.loads(worker_runtime_fixture["source"])
        document["spec"]["timeoutSeconds"] = 20
        return {**worker_runtime_fixture, "source": json.dumps(document)}
    if getattr(request, "param", None) != "large_state":
        return worker_runtime_fixture
    document = json.loads(worker_runtime_fixture["source"])
    document["spec"]["steps"] = [
        {"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(4)
    ] + document["spec"]["steps"]
    return {**worker_runtime_fixture, "source": json.dumps(document)}


@pytest.mark.parametrize("worker_runtime_fixture", ["large_state"], indirect=True)
async def test_export_withholds_oversized_reconstructed_state(
    worker_setup, transaction_factory, services, access_db, monkeypatch
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.operations import history as module
    from firefly_weave.operations.exports import bounded_size

    actor, scope, activation = worker_setup[3:6]
    run = await worker_setup[1].start(
        actor, scope, StartRunRequest(activation_id=activation.id, input="x" * 90000), "large", context=AuditContext()
    )
    monkeypatch.setattr(module, "MAX_BYTES", 200000)
    async with transaction_factory() as tx:
        result = (
            await services(access_db[0])
            .resolve(module.HistoryService)
            .export(tx, run.id, actor=actor, scope=scope, context=AuditContext())
        )
        assert result.replay.last_verified_sequence == 1
        assert result.replay.status == "incomplete" and result.replay.final_state is None
        assert result.replay.diagnostics[0].code == "WV-REPLAY-OUTPUT-LIMIT"
        assert result.replay.omissions[0].path == "/final_state"
        assert bounded_size(result, 200000) is not None


@pytest.mark.parametrize("worker_runtime_fixture", ["run_deadline"], indirect=True)
async def test_timing_uses_locked_admission_before_delayed_event(
    client,
    headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
    monkeypatch,
    worker_runtime_fixture,
):
    from datetime import timedelta

    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.workers.repository import WorkerRepository

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        effective = lease.deadline
    original = WorkerRepository.now
    observed = []

    async def checked(self):
        value = await original(self)
        observed.append(value)
        return value

    async def delayed(self):
        return lease.deadline + timedelta(days=1)

    with monkeypatch.context() as patch:
        patch.setattr(WorkerRepository, "now", checked)
        patch.setattr(RuntimeRepository, "now", delayed)
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), 9)
    response = await client.get(f"{env_url}/runs/{queued_task.id}/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replay"]["status"] == "consistent", body["replay"]
    from datetime import datetime

    event = body["events"][1]
    receipt = event["evidence"]["task_receipt"]
    assert datetime.fromisoformat(receipt["timing"]["checked_at"]) == observed[0]
    assert datetime.fromisoformat(receipt["timing"]["effective_deadline"]) == effective
    assert datetime.fromisoformat(receipt["task"]["deadline"]) > lease.deadline
    assert observed[0] < effective < datetime.fromisoformat(event["timestamp"])

    import json

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.operations.replay import replay
    from firefly_weave.runtime.models import RuntimeEvent

    artifact = compile_source(
        worker_runtime_fixture["source"], format="json", catalog=worker_runtime_fixture["catalog"]
    ).artifact
    events = tuple(RuntimeEvent.model_validate_json(json.dumps(e)) for e in body["events"])
    contradictory = events[1].model_copy(deep=True)
    contradictory.evidence["task_receipt"]["timing"]["effective_deadline"] = receipt["task"]["deadline"]
    report = replay(artifact, (events[0], contradictory))
    assert (report.status, report.last_verified_sequence) == ("inconsistent", 1)
    legacy = events[1].model_copy(deep=True)
    legacy.evidence["task_receipt"].pop("timing")
    report = replay(artifact, (events[0], legacy))
    assert (report.status, report.last_verified_sequence) == ("incomplete", 1)
