# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""The operations loop retires expired test activations and purges retired ones once no run uses them."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_step_test_run import CONTEXT, make_step_tests

from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests.sweep import sweep_test_activations

pytestmark = pytest.mark.integration


@pytest.fixture
async def step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture):
    return await make_step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture)


async def sweep(step_tests, now):
    async with step_tests.service.uow.open(step_tests.scope) as tx:
        return await sweep_test_activations(tx, now)


async def test_an_expired_test_activation_is_retired_and_refuses_new_runs(step_tests):
    accepted = await step_tests.start(step_tests.tester)
    assert (await sweep(step_tests, accepted.expires_at - timedelta(seconds=1))).retired == 0
    result = await sweep(step_tests, accepted.expires_at + timedelta(seconds=1))
    # Its run still names it, so it stays until retention purges the run.
    assert (result.retired, result.purged) == (1, 0)
    definitions = step_tests.service.definitions
    async with definitions.transaction(step_tests.scope, None, mutation=False) as tx:
        with pytest.raises(CatalogError) as expired:
            await definitions.runtime_snapshot(
                step_tests.tester, step_tests.scope, accepted.id, context=CONTEXT, tx=tx, test=True
            )
    assert expired.value.code == "WV-TESTACTIVATION-EXPIRED"


async def test_a_retired_one_no_run_uses_is_purged_and_normal_activations_stay(step_tests):
    accepted = await step_tests.start(step_tests.tester)
    normal = await step_tests.scalar("SELECT count(*) FROM activation_revisions WHERE NOT test")
    # A second test activation of the same version that never started a run, already expired.
    unused = uuid4()
    async with step_tests.owner() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,"
                "payload,test) SELECT :unused,tenant_id,project_id,environment_id,'test-unused',1,version_id,payload,"
                "true FROM activation_revisions WHERE id=:id"
            ),
            {"unused": unused, "id": accepted.id},
        )
        await session.execute(
            text(
                "INSERT INTO test_activation_details(tenant_id,project_id,environment_id,activation_id,principal_id,"
                "source,draft_id,draft_revision,step_id,side_effect,created_at,expires_at) SELECT tenant_id,"
                "project_id,environment_id,:unused,principal_id,source,draft_id,draft_revision,step_id,side_effect,"
                "created_at,created_at+interval '1 second' FROM test_activation_details WHERE activation_id=:id"
            ),
            {"unused": unused, "id": accepted.id},
        )
    result = await sweep(step_tests, accepted.expires_at - timedelta(seconds=1))
    assert (result.retired, result.purged) == (1, 1)
    assert await step_tests.scalar("SELECT count(*) FROM activation_revisions WHERE id=:id", id=unused) == 0
    # The started one still runs and keeps its rows; normal activations are untouched.
    assert await step_tests.scalar("SELECT count(*) FROM activation_revisions WHERE id=:id", id=accepted.id) == 1
    assert await step_tests.scalar("SELECT count(*) FROM activation_revisions WHERE NOT test") == normal
