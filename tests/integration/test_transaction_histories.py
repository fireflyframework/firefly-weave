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

"""Histories of transactions on real PostgreSQL export and replay consistently: runs of this version, whose incidents
can meet and open again, and runs an earlier version started, before and after a decision moved them on."""

import pytest
from stuck_transactions import (
    CONFIRMS,
    FINISHED,
    LOOKUP,
    active,
    beside,
    by_key,
    confirm_stuck,
    reconcile,
    resolve,
    retried_and_never_claimed,
    stuck,
    two_stuck,
)
from transaction_platform import build, hold, submit, transaction, workflow, writes_as_an_earlier_version

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    platform = await build(worker_setup, services, access_db, monkeypatch)
    yield platform
    await platform.shows_every_incident()


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


@pytest.fixture
async def history(services, access_db):
    from firefly_weave.operations.history import HistoryService

    return services(access_db[0]).resolve(HistoryService)


async def replays(platform, history, run, *rules) -> None:
    """The run's history exports and replays to the same transitions; its events that say ``rules`` are these."""
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        exported = await history.export(tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext())
    report = exported.replay
    assert (report.status, report.diagnostics) == ("consistent", [])
    assert tuple(event.type for event in exported.events if "transaction_rules" in event.data) == rules


async def test_two_stuck_confirms_decided_one_by_one_replay(platform, history):
    run, held = await two_stuck(platform)
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    await resolve(platform, (await by_key(platform, run.id))[CONFIRMS[1]], "retry_safe")
    await platform.complete(await platform.lease("hold2#confirm"))
    await replays(platform, history, run, "started", "incident_resolved", "incident_resolved")


async def test_a_step_that_failed_beside_the_decision_and_was_retried_replays(platform, history):
    steps = [transaction(hold(), beside(submit(), LOOKUP))]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    await platform.complete(await platform.lease("hold"))
    deciding = await platform.lease("submit")
    await platform.fail(await platform.lease("lookup"), "REJECTED", outcome="failed")
    await platform.scan()
    await platform.complete(deciding)
    await resolve(platform, await active(platform, run.id), "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    await platform.complete(await platform.lease("lookup"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    await replays(platform, history, run, "started", "incident_resolved")


async def test_an_incident_that_opened_twice_replays(platform, history):
    run, incident = await confirm_stuck(platform)
    again = await retried_and_never_claimed(platform, run, incident)
    await resolve(platform, again, "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    await replays(platform, history, run, "started", "incident_resolved", "incident_resolved")


async def test_a_run_of_an_earlier_version_replays_as_it_was_recorded(platform, history, monkeypatch):
    with monkeypatch.context() as earlier:
        writes_as_an_earlier_version(earlier)
        run, incident = await confirm_stuck(platform)
        await resolve(platform, incident, "retry_safe")
        await platform.complete(await platform.lease("hold#confirm"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    await replays(platform, history, run)


async def test_a_run_of_an_earlier_version_replays_once_a_decision_moved_it_on(
    platform, reconciler, history, monkeypatch
):
    with monkeypatch.context() as earlier:
        writes_as_an_earlier_version(earlier)
        run, held = await two_stuck(platform)
    await reconcile(platform, held[CONFIRMS[0]], reconciler)
    assert (await platform.state(run.id))["status"] == "succeeded"
    await replays(platform, history, run, "incident_resolved")


async def test_a_leftover_an_earlier_version_left_replays_once_it_is_closed(platform, reconciler, history, monkeypatch):
    with monkeypatch.context() as earlier:
        writes_as_an_earlier_version(earlier)
        run, held = await two_stuck(platform)
        await reconcile(platform, held[(await platform.record(run.id))["incident_key"]], reconciler)
    await resolve(platform, await active(platform, run.id), "retry_safe")
    assert (await platform.state(run.id))["status"] == "succeeded"
    await replays(platform, history, run, "incident_resolved")


async def test_a_called_confirm_an_earlier_version_left_replays_once_it_is_closed(
    platform, reconciler, history, monkeypatch
):
    called = {
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
        },
    }
    steps = [transaction(hold(), hold("hold2", confirm=called), submit())]
    pins = {"reverse-target-addition": platform.reverse.id}
    activation = await platform.publish(workflow("move", steps), **pins)
    with monkeypatch.context() as earlier:
        writes_as_an_earlier_version(earlier)
        run = await platform.start(activation)
        for key in ("hold", "hold2", "submit"):
            await platform.complete(await platform.lease(key))
        await stuck(platform, run.id, "hold#confirm")
        # The confirm's own run fails for good, and a person ends it.
        await platform.fail(await platform.lease("remove"), "REJECTED", outcome="failed")
        await platform.scan()
        [call] = await platform.query(
            "SELECT child_run_id FROM run_calls WHERE caller_run_id=:run AND node_id='hold2#confirm'", run=run.id
        )
        await resolve(platform, await active(platform, call["child_run_id"]), "terminate")
        await platform.scan()
        held = await by_key(platform, run.id)
        assert set(held) == {"hold#confirm:1", "hold2#confirm:0"}
        await reconcile(platform, held["hold#confirm:1"], reconciler, FINISHED)
    left = await active(platform, run.id)
    assert left["node_id"] == "hold2#confirm"
    await resolve(platform, left, "retry_safe")
    assert (await platform.state(run.id))["status"] == "succeeded"
    # No run of the confirm was started again, and replay expects none.
    calls = await platform.query("SELECT generation FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert [row["generation"] for row in calls] == [0]
    await replays(platform, history, run, "incident_resolved")


async def test_a_history_whose_decision_lost_its_rules_replays_inconsistently(
    platform, reconciler, history, monkeypatch
):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.operations.replay import replay

    with monkeypatch.context() as earlier:
        writes_as_an_earlier_version(earlier)
        run, held = await two_stuck(platform)
    await reconcile(platform, held[CONFIRMS[0]], reconciler)
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        exported = await history.export(tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext())
    [stored] = await platform.query("SELECT artifact FROM runs WHERE id=:run", run=run.id)
    events = [
        event.model_copy(update={"data": {k: v for k, v in event.data.items() if k != "transaction_rules"}})
        for event in exported.events
    ]
    report = replay(import_artifact(stored["artifact"]), tuple(events))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-TRANSITION"])
