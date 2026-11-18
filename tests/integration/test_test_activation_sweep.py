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

"""Retiring draft test activations early, and the sweep that retires expired ones and purges what no run keeps."""

from datetime import UTC, datetime

import pytest
from test_test_activation_create import CONTEXT, NOTIFY, drafts, refused  # noqa: F401
from test_test_activation_listen import listen, route_of
from test_test_activation_runs import run
from transaction_platform import workflow

pytestmark = pytest.mark.integration


async def sweep(env, *, limit=100):
    from firefly_weave.step_tests.sweep import sweep_test_activations

    async with env.platform.runtime.definitions.transaction(env.scope, None) as tx:
        return await sweep_test_activations(tx, datetime.now(UTC), limit=limit)


async def expire(env, activation) -> None:
    await env.execute(
        "UPDATE test_activation_details SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' "
        "WHERE activation_id=:a",
        a=activation.id,
    )


async def exists(env, activation) -> bool:
    return bool(await env.query("SELECT 1 FROM activation_revisions WHERE id=:a", a=activation.id))


async def test_the_creator_retires_it_at_once_and_its_listen_route_stops(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    listened = await listen(env, created)
    other = await env.person("step_tester", "developer", "deployer", "viewer")
    error = await refused(env.service.retire(other, env.scope, created.id, context=CONTEXT))
    assert (error.status, error.code) == (404, "WV-NOT-FOUND")
    await env.service.retire(env.tester, env.scope, created.id, context=CONTEXT)
    await env.service.retire(env.tester, env.scope, created.id, context=CONTEXT)
    error = await refused(run(env, created))
    assert (error.status, error.code) == (410, "WV-TESTACTIVATION-EXPIRED")
    [route] = await env.query("SELECT disabled,revision FROM trigger_routes WHERE id=:id", id=route_of(listened))
    assert (route["disabled"], route["revision"]) == (True, 2)
    records = await env.query("SELECT target FROM access_audit WHERE action='test_activation.retire'")
    assert records == [{"target": str(created.id)}]


async def test_the_sweep_retires_expired_ones_stops_their_routes_and_purges_them_with_their_version(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    listened = await listen(env, created)
    [version] = await env.query("SELECT version_id FROM activation_revisions WHERE id=:a", a=created.id)
    await expire(env, created)
    result = await sweep(env)
    assert (result.retired, result.purged) == (1, 0)
    [route] = await env.query("SELECT disabled FROM trigger_routes WHERE id=:id", id=route_of(listened))
    assert route["disabled"] is True
    # A stopped route's reserve is released, so the next pass purges the activation without leaking it.
    [reserve] = await env.query(
        "SELECT amount FROM operation_reservations WHERE kind='webhook' AND resource_id=:id", id=route_of(listened)
    )
    assert reserve["amount"] == 0
    result = await sweep(env)
    assert (result.retired, result.purged) == (0, 1)
    assert not await exists(env, created)
    assert await env.query("SELECT id FROM trigger_routes WHERE id=:id", id=route_of(listened)) == []
    assert await env.query("SELECT id FROM definition_versions WHERE id=:v", v=version["version_id"]) == []
    assert await env.query("SELECT 1 FROM test_activation_details") == []


async def test_a_test_activation_whose_runs_are_kept_stays_until_retention_purges_them(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    await run(env, created)
    await env.service.retire(env.tester, env.scope, created.id, context=CONTEXT)
    result = await sweep(env)
    assert (result.retired, result.purged) == (0, 0)
    assert await exists(env, created)


async def test_kept_ones_never_hold_back_a_newer_purgeable_one(drafts):  # noqa: F811
    env = drafts
    kept = []
    for _ in range(3):
        created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
        await run(env, created)
        kept.append(created)
    newest = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    for activation in kept:
        await env.service.retire(env.tester, env.scope, activation.id, context=CONTEXT)
    await env.service.retire(env.tester, env.scope, newest.id, context=CONTEXT)
    # More retired activations that runs keep than one sweep reads, all retired before the purgeable one.
    result = await sweep(env, limit=2)
    assert result.purged == 1
    assert not await exists(env, newest) and all([await exists(env, activation) for activation in kept])


async def test_a_test_run_never_runs_longer_than_an_hour(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    [version] = await env.query(
        "SELECT v.document->'spec'->'timeoutSeconds' AS timeout FROM activation_revisions a JOIN definition_versions v "
        "ON v.id=a.version_id WHERE a.id=:a",
        a=created.id,
    )
    assert version["timeout"] == 3600
