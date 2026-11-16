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

"""Changing or deleting a rule while the evaluator works on it, on real PostgreSQL: both take the rule's state row
before its alert, so neither waits on the other in the opposite order, and the alert ends resolved; a rule saved
between the evaluator's read and its write gets nothing written from that read."""

import asyncio
import time
from datetime import timedelta

import pytest
from test_alert_loop_integration import CONTEXT, at, create, draft, evaluator, rows, state  # noqa: F401
from test_alert_rules_api import add_channel
from test_operations_statistics import owner
from test_operations_summary import add_incident

from firefly_weave.operations import alert_loop
from firefly_weave.operations.alert_loop import AlertLoop

pytestmark = pytest.mark.integration


async def waiting_for_a_lock(case) -> None:
    """Return once a session of this test's database waits for a lock."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        waiting = await owner(
            case,
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'",
        )
        if waiting[0]["n"]:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("nothing waited for a lock")


async def firing_rule(case, monkeypatch):
    """A firing rule whose repeat the evaluator is writing at 900 s, paused after it wrote the rule's state."""
    run = await case.start("alert-change-race")
    await add_incident(case, run.id, "first")
    channel = await add_channel(case)
    rule = await create(case, draft(channel_ids=[str(channel)], renotify_seconds=900))
    await at(case, 0)
    (alert,) = await rows(case, "SELECT id FROM alerts WHERE rule_id=:r AND state='firing'", r=rule.id)
    written, release = asyncio.Event(), asyncio.Event()
    original = alert_loop.queue_notifications

    async def paused(*args, **kwargs):
        await original(*args, **kwargs)
        written.set()
        await release.wait()

    monkeypatch.setattr(alert_loop, "queue_notifications", paused)
    evaluation = asyncio.create_task(at(case, 900))
    await asyncio.wait_for(written.wait(), 10)
    return rule, channel, alert["id"], evaluation, release


async def test_changing_a_firing_rule_while_the_evaluator_writes_it_resolves_its_alert(evaluator, monkeypatch):  # noqa: F811
    case = evaluator
    rule, channel, alert, evaluation, release = await firing_rule(case, monkeypatch)
    changed = draft(channel_ids=[str(channel)], renotify_seconds=900, threshold=5)
    change = asyncio.create_task(case.rules.update(case.manager, case.scope, rule.id, changed, 1, context=CONTEXT))
    await waiting_for_a_lock(case)
    release.set()
    await evaluation
    view = await change
    assert view.revision == 2 and view.state.state == "ok"
    (resolved,) = await rows(case, "SELECT state,resolved_reason FROM alerts WHERE id=:id", id=alert)
    assert resolved == {"state": "resolved", "resolved_reason": "rule_changed"}
    # The next evaluation reads the changed rule and writes it.
    await at(case, 930)
    assert (await state(case, rule.id))["evaluated_at"] == case.now + timedelta(seconds=930)
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:r AND state='firing'", r=rule.id) == []


async def test_deleting_a_firing_rule_while_the_evaluator_writes_it_resolves_its_alert(evaluator, monkeypatch):  # noqa: F811
    case = evaluator
    rule, _, alert, evaluation, release = await firing_rule(case, monkeypatch)
    delete = asyncio.create_task(case.rules.delete(case.manager, case.scope, rule.id, 1, context=CONTEXT))
    await waiting_for_a_lock(case)
    release.set()
    await evaluation
    await delete
    (resolved,) = await rows(case, "SELECT state,resolved_reason FROM alerts WHERE id=:id", id=alert)
    assert resolved == {"state": "resolved", "resolved_reason": "rule_deleted"}
    assert await rows(case, "SELECT rule_id FROM alert_rule_state WHERE rule_id=:r", r=rule.id) == []


async def test_a_rule_saved_with_a_longer_for_time_while_it_is_read_does_not_fire(evaluator, monkeypatch):  # noqa: F811
    case = evaluator
    run = await case.start("alert-change-after-read")
    await add_incident(case, run.id, "first")
    rule = await create(case, draft())
    planned, release = asyncio.Event(), asyncio.Event()
    original = AlertLoop._plan

    async def paused(self, authority, now):
        decided = await original(self, authority, now)
        planned.set()
        await release.wait()
        return decided

    monkeypatch.setattr(AlertLoop, "_plan", paused)
    # The read decided to fire (For 0, one incident open); the rule is saved with For 300 before anything is written.
    evaluation = asyncio.create_task(at(case, 0))
    await asyncio.wait_for(planned.wait(), 10)
    saved = await case.rules.update(case.manager, case.scope, rule.id, draft(for_seconds=300), 1, context=CONTEXT)
    release.set()
    await evaluation
    assert (saved.revision, saved.for_seconds) == (2, 300)
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:r", r=rule.id) == []
    # The next evaluation reads the saved For time and waits instead of firing.
    monkeypatch.setattr(AlertLoop, "_plan", original)
    await at(case, 30)
    assert (await state(case, rule.id))["state"] == "pending"
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:r", r=rule.id) == []
