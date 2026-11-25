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

"""Transaction events on real PostgreSQL: one outbox event per terminal or in-doubt state, written with the change."""

import json

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from stuck_transactions import (
    CONFIRMS,
    PAUSE,
    active,
    beside,
    by_key,
    confirm_stuck,
    failed_beside,
    reconcile,
    resolve,
    two_stuck,
)
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    platform = await build(worker_setup, services, access_db, monkeypatch)
    yield platform
    await platform.shows_every_incident()


async def events(platform, run_id):
    return await platform.query(
        "SELECT payload FROM integration_events WHERE payload->>'type' LIKE 'transaction.%' "
        "AND payload->>'resource_id'=:run ORDER BY payload->>'emitted_at'",
        run=str(run_id),
    )


async def test_each_terminal_or_in_doubt_state_emits_one_event_in_the_same_transaction(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    committed = await platform.start(activation, "M-1")
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("submit"))
    await platform.complete(await platform.lease("hold#confirm"))
    [event] = await events(platform, committed.id)
    payload = event["payload"]
    assert payload["type"] == "transaction.completed"
    facts = payload["payload"]["transaction"]
    assert (facts["instance_key"], facts["business_key"], facts["state"], facts["test"]) == (
        "move",
        "M-1",
        "completed",
        False,
    )
    assert [step["instance_key"] for step in facts["steps"]["done"]] == ["hold", "add", "submit"]

    doubtful = await platform.start(activation, "M-2")
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [event] = await events(platform, doubtful.id)
    facts = event["payload"]["payload"]["transaction"]
    [incident] = await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=doubtful.id)
    assert (facts["state"], facts["incident_id"], facts["cause"]["code"]) == (
        "in_doubt",
        str(incident["id"]),
        "WV-TXN-UNDO-STUCK",
    )
    assert "input" not in str(facts) and "output" not in str(facts)


async def test_a_retried_projection_never_emits_twice(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    await platform.scan()
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.cancelled"


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def test_a_force_cancel_of_a_run_too_large_for_the_kernel_emits_force_ended(platform, reconciler, monkeypatch):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime import repository

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "stop", actor=reconciler, scope=platform.scope, context=AuditContext(), skip_undo=True
        )
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.force_ended"
    assert event["payload"]["payload"]["transaction"]["state"] == "force_ended"


async def test_force_end_of_a_held_run_emits_force_ended(platform, reconciler, monkeypatch):
    import asyncio
    from uuid import uuid4

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.runtime import kernel

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(kernel, "STATE_BYTES", 1)
    await asyncio.sleep(5.2)
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert incident["code"] == "WV-TXN-RUN-OVERSIZED"
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            incident["id"],
            IncidentResolution(receipt_id=uuid4(), kind="terminate", reason="Checked both systems."),
            incident["revision"],
            actor=reconciler,
            scope=platform.scope,
            context=AuditContext(),
        )
    assert (await platform.state(run.id))["status"] == "cancelled"
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.force_ended"


async def test_a_record_that_stays_in_doubt_at_a_later_revision_emits_no_second_event(platform):
    import asyncio

    branches = {
        "id": "branches",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [add("add")], "output": {"literal": None}},
            "b": {"steps": [add("add2")], "output": {"literal": None}},
        },
    }
    activation = await platform.publish(workflow("move-in-parallel", [transaction(hold(), branches, submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    first = await platform.lease("add")
    await platform.lease("add2")
    await platform.fail(first, "UNAVAILABLE")
    await platform.scan()
    # add2 is still in flight: the rollback waits for it until its own deadline plus the margin, then is in doubt.
    # Its lease ended at its deadline, so the same scan's recovery then settles it as unknown, which changes the
    # record again but not its state.
    await asyncio.sleep(61)
    await platform.scan()
    record = await platform.record(run.id)
    assert record["state"] == "in_doubt"
    rows = await platform.query(
        "SELECT type,transition FROM run_events WHERE run_id=:run ORDER BY sequence DESC LIMIT 2", run=run.id
    )
    [(latest, latest_record), (doubted, doubted_record)] = [
        (row["type"], row["transition"]["state"]["transactions"]["move"]) for row in rows
    ]
    assert (doubted, doubted_record["status"], latest, latest_record["status"]) == (
        "wait_elapsed",
        "in_doubt",
        "step_failed",
        "in_doubt",
    )
    assert record["revision"] == latest_record["revision"] > doubted_record["revision"]
    assert [event["payload"]["type"] for event in await events(platform, run.id)] == ["transaction.in_doubt"]


async def revision(platform, key: str, run) -> int:
    [row] = await platform.query(
        "SELECT revision FROM incidents WHERE run_id=:run AND incident_key=:key", run=run.id, key=key
    )
    return row["revision"]


async def test_the_revision_of_an_open_incident_rises_with_its_transactions_record(platform, reconciler):
    run, held = await two_stuck(platform, PAUSE)
    opened = {key: row["revision"] for key, row in held.items()}
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    # The step beside it is tried again: once for that decision, whatever it changed.
    assert await revision(platform, CONFIRMS[1], run) == opened[CONFIRMS[1]] + 1
    # An attempt that fails and is tried again is no change of what a person decides about.
    await platform.fail(await platform.lease("hold#confirm"), "UNAVAILABLE")
    assert await revision(platform, CONFIRMS[1], run) == opened[CONFIRMS[1]] + 1
    await platform.scan()
    await platform.execute(
        "UPDATE task_intents SET next_attempt_at=NULL WHERE run_id=:run AND status='ready'", run=run.id
    )
    assert await revision(platform, CONFIRMS[1], run) == opened[CONFIRMS[1]] + 1
    [shown] = (await by_key(platform, run.id)).values()
    await platform.complete(await platform.lease("hold#confirm"))
    assert await revision(platform, CONFIRMS[1], run) == opened[CONFIRMS[1]] + 2
    # A dialog that was open meanwhile is refused, and it writes nothing.
    state = await platform.state(run.id)
    with pytest.raises(CatalogError) as refused:
        await reconcile(platform, shown, reconciler)
    assert (refused.value.status, refused.value.code) == (409, "WV-INCIDENT-REVISION")
    assert await platform.state(run.id) == state
    [now] = (await by_key(platform, run.id)).values()
    record = {"as": "completed", "participants": [{"instance_key": "hold2", "outcome": "confirmed"}]}
    await reconcile(platform, now, reconciler, {**record, "blockOutput": "moved"})


async def test_an_incident_that_opens_again_raises_the_one_beside_it_and_itself_once(platform):
    run, held = await two_stuck(platform)
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    before = {key: await revision(platform, key, run) for key in CONFIRMS}
    await platform.execute(
        "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
        "to_jsonb(cast((clock_timestamp()-interval '1 second') AS text))) "
        "WHERE run_id=:run AND node_id='hold#confirm'",
        run=run.id,
    )
    await platform.scan()
    assert set(await by_key(platform, run.id)) == set(CONFIRMS)
    assert {key: await revision(platform, key, run) for key in CONFIRMS} == {
        key: value + 1 for key, value in before.items()
    }


async def test_a_transaction_beside_it_raises_no_revision(platform):
    moves = transaction(hold(), add(), submit())
    other = {**transaction(hold("hold3", holdSeconds=600), add("add3")), "id": "other"}
    run = await platform.start(await platform.publish(workflow("move", [beside(moves, other)])))
    await platform.complete(await platform.lease("hold3"))
    await platform.lease("add3")
    for key in ("hold", "add", "submit"):
        await platform.complete(await platform.lease(key))
    await platform.fail(await platform.lease("hold#confirm"), "REJECTED", outcome="failed")
    await platform.scan()
    before = await active(platform, run.id)
    # The other transaction goes back on its own time, which nothing holds.
    await platform.execute(
        "WITH due AS (UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' "
        "WHERE run_id=:run AND node_id='other#hold' RETURNING run_id,deadline) UPDATE runs SET state="
        "jsonb_set(state,'{transactions,other,hold_by}',to_jsonb(due.deadline)) FROM due WHERE runs.id=due.run_id",
        run=run.id,
    )
    await platform.scan()
    assert (await platform.record(run.id, "other"))["state"] == "compensating"
    assert await active(platform, run.id) == before


async def test_raising_a_revision_changes_no_transition(platform):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.contracts.operations import RecordedEvidence
    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    run, held = await two_stuck(platform)
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    assert await revision(platform, CONFIRMS[1], run) > held[CONFIRMS[1]]["revision"]
    [stored] = await platform.query("SELECT artifact,request FROM runs WHERE id=:run", run=run.id)
    rows = await platform.query(
        "SELECT r.id,r.sequence,r.type,r.data,r.created_at,e.evidence FROM run_events r JOIN run_event_evidence e "
        "ON e.run_id=r.run_id AND e.sequence=r.sequence WHERE r.run_id=:run ORDER BY r.sequence",
        run=run.id,
    )
    artifact = import_artifact(stored["artifact"])
    state = RunState(input=stored["request"]["input"])
    for row in rows:
        event = RuntimeEvent(
            id=row["id"], type=row["type"], timestamp=row["created_at"], sequence=row["sequence"], data=row["data"]
        )
        result = transition(state, event, artifact)
        recorded = RecordedEvidence.model_validate_json(json.dumps(row["evidence"]))
        assert transition_digest(result) == recorded.expected_transition_digest, row["type"]
        state = result.state


@pytest.mark.parametrize("decided", [0, 1])
@pytest.mark.parametrize("kind", ["accept_reconciled_result", "terminate"])
async def test_a_run_names_the_incident_a_person_resolved(platform, reconciler, kind, decided):
    run, held = await two_stuck(platform)
    incident = held[CONFIRMS[decided]]
    if kind == "terminate":
        await resolve(platform, incident, kind, actor=reconciler)
    else:
        await reconcile(platform, incident, reconciler)
    assert (await platform.record(run.id))["incident_key"] == CONFIRMS[decided]
    page = await platform.runtime.transactions(platform.actor, platform.scope, run.id, context=AuditContext())
    [item] = page["items"]
    assert item["incident_id"] == str(incident["id"])
    assert (item["resolution"]["kind"], item["resolution"]["actor_id"]) == (kind, str(reconciler.id))


def in_doubt_counted(reader: InMemoryMetricReader) -> int:
    data = reader.get_metrics_data()
    return sum(
        point.value
        for resource in (data.resource_metrics if data else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "weave.transactions"
        for point in metric.data.data_points
        if point.attributes["error_code"] == "WV-TXN-CONFIRM-STUCK"
    )


async def test_the_next_incident_of_a_transaction_in_doubt_is_announced(platform, monkeypatch):
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    telemetry = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    monkeypatch.setattr(platform.definitions.uow, "telemetry", telemetry)
    run, held = await two_stuck(platform)
    named = (await platform.record(run.id))["incident_key"]
    [other] = set(CONFIRMS) - {named}
    await resolve(platform, held[named], "retry_safe")
    # A subscriber learns which incident holds the transaction now; it is one transaction in doubt all the same.
    sent = [event["payload"]["payload"]["transaction"] for event in await events(platform, run.id)]
    assert [(item["state"], item["incident_id"]) for item in sent] == [
        ("in_doubt", str(held[named]["id"])),
        ("in_doubt", str(held[other]["id"])),
    ]
    assert in_doubt_counted(reader) == 1
    # The record changes again and still names the same incident: nothing more is sent.
    await platform.complete(await platform.lease(named.removesuffix(":1")))
    assert len(await events(platform, run.id)) == 2


async def test_a_step_stuck_beside_the_decision_is_announced_with_its_incident(platform):
    run, incident = await failed_beside(platform)
    [event] = await events(platform, run.id)
    facts = event["payload"]["payload"]["transaction"]
    assert (facts["state"], facts["incident_id"]) == ("in_doubt", str(incident["id"]))
    assert facts["cause"] == {"code": "WV-TXN-AFTER-PIVOT-STUCK", "step": "lookup", "origin_code": "REJECTED"}


async def test_the_event_that_puts_a_transaction_in_doubt_raises_its_incident_once(platform):
    # The failed attempt opened it, and the failure that is final changed what it says.
    _, incident = await confirm_stuck(platform)
    assert incident["revision"] == 2
