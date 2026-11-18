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

"""A platform-event trigger moves to a new version of its workflow, keeps what it started, and runs as the mover."""

import json
from uuid import uuid4

import pytest
from test_event_triggers import MOVE, dispatch, held, in_doubt, person, platform, starts  # noqa: F401
from test_webhook_retarget import refused
from transaction_platform import reaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration


async def next_version(platform, document, *, version, expected):  # noqa: F811
    """Publish ``document`` at ``version`` and activate it as the next revision of its activation."""
    from firefly_weave.contracts.catalog import ActivationRequest

    document = json.loads(json.dumps(document))
    document["metadata"]["version"] = version
    published = await platform.definitions.publish(
        platform.actor, platform.scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    request = ActivationRequest(
        scope=platform.scope,
        version_id=published.id,
        artifact_digest=published.digest,
        worker_release_ids={task: platform.release for task in platform.needs(document)},
    )
    return await platform.definitions.activate(
        platform.actor, platform.scope, request, str(uuid4()), expected_revision=expected, context=AuditContext()
    )


async def retarget(platform, trigger, activation, revision, actor):  # noqa: F811
    return await platform.event_triggers.retarget(
        actor, platform.scope, trigger.id, activation.id, revision, context=AuditContext()
    )


async def runs_of(platform, row):  # noqa: F811
    [run] = await platform.query("SELECT activation_id,principal_id FROM runs WHERE id=:run", run=row["run_id"])
    return run["activation_id"], run["principal_id"]


async def test_a_moved_trigger_keeps_its_starts_and_later_events_start_the_new_version(
    platform,  # noqa: F811
    access_db,
    provisioned,
):
    mover = await person(access_db, provisioned, platform, "deployer", "operator", "viewer")
    first = await platform.publish(reaction())
    trigger = await platform.bind(first)
    move = await platform.publish(workflow(*MOVE))
    await in_doubt(platform, "M-1", move)
    second = await next_version(platform, reaction(), version="1.1.0", expected=1)
    moved = await retarget(platform, trigger, second, 1, mover)
    assert (moved.id, moved.revision, moved.activation_id, moved.principal_id) == (trigger.id, 2, second.id, mover.id)
    assert (moved.name, moved.events, moved.workflow_name, moved.may_reconcile) == (
        trigger.name,
        trigger.events,
        trigger.workflow_name,
        False,
    )
    [before] = await starts(platform)
    assert await runs_of(platform, before) == (first.id, platform.actor.id)
    await in_doubt(platform, "M-2", move)
    rows = await starts(platform)
    assert len(rows) == 2 and before in rows
    [after] = [row for row in rows if row["id"] != before["id"]]
    assert await runs_of(platform, after) == (second.id, mover.id)
    [record] = await platform.query("SELECT target,event FROM access_audit WHERE action='event_trigger.retarget'")
    assert record["target"] == str(trigger.id)
    assert record["event"]["details"] == {
        "kind": "event",
        "from_activation_id": str(first.id),
        "to_activation_id": str(second.id),
        "from_principal_id": str(platform.actor.id),
        "to_principal_id": str(mover.id),
    }


async def test_a_start_requested_before_the_move_starts_the_new_version(platform, access_db, provisioned, monkeypatch):  # noqa: F811
    mover = await person(access_db, provisioned, platform, "deployer", "operator", "viewer")
    trigger = await platform.bind(await platform.publish(reaction()))
    with held(monkeypatch):
        await in_doubt(platform)
    second = await next_version(platform, reaction(), version="1.1.0", expected=1)
    await retarget(platform, trigger, second, 1, mover)
    assert await dispatch(platform) == (1, 0)
    [row] = await starts(platform)
    assert row["status"] == "started" and await runs_of(platform, row) == (second.id, mover.id)


async def test_a_version_that_no_longer_declares_the_events_or_sources_is_refused(platform):  # noqa: F811
    first = await platform.publish(reaction())
    trigger = await platform.bind(first)
    # Only another workflow's events may start this one; the binding narrows them to the move workflow's.
    narrowed = await platform.bind(first, name="moves", source_workflows=["move"])
    other_events = await next_version(
        platform, reaction(events=("transaction.compensated",)), version="2.0.0", expected=1
    )
    assert await refused(retarget(platform, trigger, other_events, 1, platform.actor)) == (
        409,
        "WV-TRIGGER-SCHEMA-CHANGED",
    )
    billing_only = await next_version(platform, reaction(sources=["billing"]), version="2.1.0", expected=2)
    assert await refused(retarget(platform, narrowed, billing_only, 1, platform.actor)) == (
        409,
        "WV-TRIGGER-SCHEMA-CHANGED",
    )
    assert (await retarget(platform, trigger, billing_only, 1, platform.actor)).activation_id == billing_only.id


async def test_a_stale_or_missing_revision_a_disabled_trigger_and_another_workflow_are_refused(platform):  # noqa: F811
    first = await platform.publish(reaction())
    trigger = await platform.bind(first)
    second = await next_version(platform, reaction(), version="1.1.0", expected=1)
    assert await refused(retarget(platform, trigger, second, None, platform.actor)) == (422, "WV-ETAG")
    assert await refused(retarget(platform, trigger, second, 2, platform.actor)) == (409, "WV-REVISION")
    other = await platform.publish(reaction("page-team"))
    assert await refused(retarget(platform, trigger, other, 1, platform.actor)) == (409, "WV-TRIGGER-WORKFLOW")
    await platform.event_triggers.disable(platform.actor, platform.scope, trigger.id, context=AuditContext())
    assert await refused(retarget(platform, trigger, second, 1, platform.actor)) == (409, "WV-REVISION")
    assert await refused(retarget(platform, trigger, second, 2, platform.actor)) == (409, "WV-TRIGGER-DISABLED")


async def test_the_mover_needs_what_creating_the_trigger_needs(platform, access_db, provisioned):  # noqa: F811
    trigger = await platform.bind(await platform.publish(reaction()))
    second = await next_version(platform, reaction(), version="1.1.0", expected=1)
    for roles in (("operator", "viewer"), ("deployer", "viewer"), ("deployer", "operator")):
        lacking = await person(access_db, provisioned, platform, *roles)
        with pytest.raises(AccessDenied):
            await retarget(platform, trigger, second, 1, lacking)
    await access_db[2].grant(
        provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope)
    )
    actor = await access_db[2].load_principal(platform.actor.id)
    resolving = await platform.bind(
        await platform.publish(reaction("resolve-moves")), name="resolve", may_reconcile=True
    )
    third = await next_version(platform, reaction("resolve-moves"), version="1.1.0", expected=1)
    mover = await person(access_db, provisioned, platform, "deployer", "operator", "viewer")
    # A trigger whose runs may resolve their transaction also needs a reconciler to move it.
    with pytest.raises(AccessDenied):
        await retarget(platform, resolving, third, 1, mover)
    assert (await retarget(platform, resolving, third, 1, actor)).principal_id == actor.id
