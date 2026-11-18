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

"""An app event source moves to a new version when its mapping still fits, under a new binding for the mover."""

from types import SimpleNamespace

import pytest
from providers.test_inbox import deliver, inbox, message, rows  # noqa: F401 (inbox is a fixture)
from sqlalchemy import text
from test_webhook_retarget import activate_version, person, publish_version, refused

pytestmark = pytest.mark.integration


@pytest.fixture
async def events(inbox, worker_setup, worker_runtime_fixture, access_db, provisioned):  # noqa: F811
    from firefly_weave.access.models import Grant
    from firefly_weave.definitions.service import DefinitionService

    _, _, _, creator, scope, first, _ = worker_setup
    _, owner, access, _ = access_db
    admin, _ = provisioned
    definitions = inbox["apps"][0].state.pyfly.context.get_bean(DefinitionService)
    source = worker_runtime_fixture["source"]
    creator = inbox["actor"]
    second = await activate_version(
        definitions,
        creator,
        scope,
        first,
        await publish_version(definitions, creator, scope, source, version="1.1.0"),
        expected=1,
    )
    mover = await person(access, admin, scope, "deployer", "operator", "viewer", "tenant_admin")
    return SimpleNamespace(
        inbox=inbox,
        service=inbox["service"],
        definitions=definitions,
        source=source,
        events=inbox["source"],
        scope=scope,
        creator=creator,
        mover=mover,
        first=first,
        second=second,
        owner=owner,
        access=access,
        admin=admin,
        grant=Grant,
    )


async def retarget(env, activation_id, revision, *, actor=None):
    from firefly_weave.access.audit import AuditContext

    return await env.service.retarget(
        actor or env.mover, env.scope, env.events.id, activation_id, revision, context=AuditContext()
    )


async def stored(env, sql, **values):
    async with env.owner() as session:
        return (await session.execute(text(sql), {"id": env.events.id, **values})).mappings().all()


async def dispatched(env, access_db, event):
    """Deliver ``event`` and dispatch its receipt; answer the run it started."""
    assert (await deliver(env.inbox, message(event))).status_code == 200
    [receipt] = [row for row in await rows(env.inbox, access_db) if row["event_id"] == event]
    result = await env.inbox["dispatchers"][0].dispatch_one(env.scope, receipt["id"])
    assert result.state == "dispatched"
    [run] = await stored(env, "SELECT activation_id,principal_id FROM runs WHERE id=:run", run=result.run_id)
    return run


async def test_a_retarget_binds_the_mover_and_the_next_event_starts_the_new_version(events, access_db):
    env = events
    moved = await retarget(env, env.second.id, 1)
    assert (moved.id, moved.revision, moved.activation_id, moved.principal_id) == (
        env.events.id,
        2,
        env.second.id,
        env.mover.id,
    )
    bindings = {
        row["id"]: row
        for row in await stored(
            env, "SELECT id,revoked,principal_id FROM connection_source_bindings WHERE source_id=:id"
        )
    }
    assert bindings[env.events.binding_id]["revoked"] is True
    assert (bindings[moved.binding_id]["revoked"], bindings[moved.binding_id]["principal_id"]) == (False, env.mover.id)
    run = await dispatched(env, access_db, "after-move")
    assert (run["activation_id"], run["principal_id"]) == (env.second.id, env.mover.id)


async def test_an_event_received_before_the_move_dispatches_on_the_new_version(events, access_db):
    env = events
    assert (await deliver(env.inbox, message("before-move"))).status_code == 200
    await retarget(env, env.second.id, 1)
    [receipt] = await rows(env.inbox, access_db)
    result = await env.inbox["dispatchers"][0].dispatch_one(env.scope, receipt["id"])
    [run] = await stored(env, "SELECT activation_id,principal_id FROM runs WHERE id=:run", run=result.run_id)
    assert (run["activation_id"], run["principal_id"]) == (env.second.id, env.mover.id)


async def test_an_input_the_mapping_doesnt_fit_is_refused_and_one_it_fits_is_accepted(events):
    env = events
    # The mapping makes an integer of every event the source dispatches.
    text_input = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(
            env.definitions, env.creator, env.scope, env.source, version="2.0.0", input_schema={"type": "string"}
        ),
        expected=2,
    )
    assert await refused(retarget(env, text_input.id, 1)) == (409, "WV-TRIGGER-SCHEMA-CHANGED")
    number_input = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(
            env.definitions, env.creator, env.scope, env.source, version="2.1.0", input_schema={"type": "integer"}
        ),
        expected=3,
    )
    assert (await retarget(env, number_input.id, 1)).activation_id == number_input.id


async def test_the_audit_names_activations_principals_and_bindings_only(events):
    env = events
    moved = await retarget(env, env.second.id, 1)
    [audit] = await stored(env, "SELECT target,event FROM access_audit WHERE action='provider.source.retarget'")
    assert audit["target"] == str(env.events.id)
    assert audit["event"]["details"] == {
        "kind": "provider",
        "from_activation_id": str(env.first.id),
        "to_activation_id": str(env.second.id),
        "from_principal_id": str(env.creator.id),
        "to_principal_id": str(env.mover.id),
        "from_binding_id": str(env.events.binding_id),
        "to_binding_id": str(moved.binding_id),
    }


async def test_a_stale_or_missing_revision_and_a_stopped_source_are_refused(events):
    env = events
    from firefly_weave.access.audit import AuditContext

    assert await refused(retarget(env, env.second.id, None)) == (422, "WV-ETAG")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-REVISION")
    stopped = await env.service.disable(env.creator, env.scope, env.events.id, context=AuditContext())
    assert (stopped.disabled, stopped.revision) == (True, 2)
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-REVISION")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-TRIGGER-DISABLED")


async def test_another_workflow_is_refused(events):
    env = events
    other = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="1.0.0", name="other-flow"),
    )
    assert await refused(retarget(env, other.id, 1)) == (409, "WV-TRIGGER-WORKFLOW")


async def test_the_mover_needs_what_creating_an_app_event_source_needs(events):
    env = events
    from firefly_weave.access.authorization import AccessDenied

    for roles in (
        ("deployer", "operator", "viewer"),
        ("operator", "viewer", "tenant_admin"),
        ("deployer", "viewer", "tenant_admin"),
    ):
        lacking = await person(env.access, env.admin, env.scope, *roles)
        with pytest.raises(AccessDenied):
            await retarget(env, env.second.id, 1, actor=lacking)
    [row] = await stored(env, "SELECT revision,binding_id FROM provider_sources WHERE id=:id")
    assert (row["revision"], row["binding_id"]) == (1, env.events.binding_id)


async def test_a_source_whose_binding_was_revoked_stays_stopped(events):
    env = events
    from firefly_weave.access.audit import AuditContext

    await env.service.bindings.revoke(env.creator, env.scope, env.events.binding_id, context=AuditContext())
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-TRIGGER-DISABLED")
    [row] = await stored(env, "SELECT revision,binding_id FROM provider_sources WHERE id=:id")
    assert (row["revision"], row["binding_id"]) == (1, env.events.binding_id)
    assert await stored(env, "SELECT id FROM connection_source_bindings WHERE source_id=:id AND NOT revoked") == []


async def test_a_mover_without_connection_management_is_refused_before_the_source_is_read(events):
    env = events
    from firefly_weave.access.authorization import AccessDenied

    lacking = await person(env.access, env.admin, env.scope, "deployer", "operator", "viewer")
    # A stale revision would answer 409; the missing capability is refused first.
    with pytest.raises(AccessDenied):
        await retarget(env, env.second.id, 5, actor=lacking)
