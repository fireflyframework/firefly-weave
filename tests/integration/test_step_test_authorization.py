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
"""Each check before a step test runs refuses before anything is written, in the order the server applies them."""

from uuid import uuid4

import pytest
from test_step_test_run import CONTEXT, make_step_tests

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.step_tests import SynthesizedTest
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


@pytest.fixture
async def step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture):
    return await make_step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture)


@pytest.mark.parametrize(
    "roles, resources, connections",
    [
        (("developer",), (), 0),
        (("step_tester",), (), 0),
        (("step_tester", "developer", "deployer"), ("first",), 2),
    ],
    ids=["no step.test", "no catalog.read", "connection.bind on one of two revisions"],
)
async def test_each_missing_grant_is_refused_before_anything_is_created(step_tests, roles, resources, connections):
    first, second = uuid4(), uuid4()
    actor = await step_tests.person(*roles, resources=tuple(str(first) for _ in resources))
    chosen = {"a": str(first), "b": str(second)} if connections else {}
    with pytest.raises(AccessDenied):
        await step_tests.start(actor, step_tests.request(connections=chosen))
    assert await step_tests.nothing_written()


async def test_a_replay_after_the_step_tester_role_is_revoked_is_refused(step_tests):
    identifier = await step_tests.access.create_principal(step_tests.admin, "application")
    project = step_tests.scope.model_copy(update={"environment_id": None})
    await step_tests.access.grant(step_tests.admin, identifier, Grant(role="developer", scope=project))
    binding = await step_tests.access.grant(
        step_tests.admin, identifier, Grant(role="step_tester", scope=step_tests.scope)
    )
    await step_tests.start(await step_tests.access.load_principal(identifier))
    await step_tests.access.revoke_grant(step_tests.admin, step_tests.scope, binding)
    with pytest.raises(AccessDenied):
        await step_tests.start(await step_tests.access.load_principal(identifier))


async def test_step_test_authority_cant_cancel_a_run_that_isnt_a_test_run(step_tests, worker_setup):
    _, runtime, _, actor, scope, activation, _ = worker_setup
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=3), "run", context=CONTEXT
    )
    with pytest.raises(AccessDenied):
        async with step_tests.service.uow.open(scope) as tx:
            await runtime.cancel(
                tx,
                run.id,
                "Canceled from step details",
                actor=step_tests.tester,
                scope=scope,
                context=CONTEXT,
                capability="step.test",
            )
    assert await step_tests.scalar("SELECT state->>'status' FROM runs WHERE id=:id", id=run.id) != "cancelled"


async def test_another_tenants_draft_is_not_found(step_tests):
    with pytest.raises(CatalogError) as hidden:
        await step_tests.start(step_tests.tester, step_tests.request(draft_id=str(uuid4())))
    assert (hidden.value.status, hidden.value.code) == (404, "WV-NOT-FOUND")
    assert await step_tests.nothing_written()


async def test_a_disabled_environment_refuses_before_reading_the_draft(step_tests):
    await step_tests.policy("disabled")
    with pytest.raises(CatalogError) as refused:
        await step_tests.start(step_tests.tester, step_tests.request(draft_id=str(uuid4())))
    assert (refused.value.status, refused.value.code) == (403, "WV-STEPTEST-DISABLED")
    assert await step_tests.nothing_written()


async def test_a_step_that_changes_data_asks_first_and_names_the_action_and_environment(step_tests, monkeypatch):
    real = step_tests.service.synthesizers.for_kind("action")

    class Changing:
        kinds = real.kinds

        def synthesize(self, request, draft, catalog):
            made = real.synthesize(request, draft, catalog)
            return SynthesizedTest.model_validate({**made.model_dump(), "side_effect": "non_idempotent"})

    monkeypatch.setattr(step_tests.service.synthesizers, "for_kind", lambda kind: Changing())
    for acknowledged in (None, "idempotency_key"):
        with pytest.raises(CatalogError) as asked:
            await step_tests.start(step_tests.tester, step_tests.request(acknowledged_side_effect=acknowledged))
        assert (asked.value.status, asked.value.code) == (409, "WV-STEPTEST-CONFIRM")
        assert asked.value.result == {
            "side_effect": "non_idempotent",
            "action": "echo-action@2.0.0",
            "environment": await step_tests.scalar(
                "SELECT name FROM environments WHERE id=:id", id=step_tests.scope.environment_id
            ),
        }
    assert await step_tests.nothing_written()
    accepted = await step_tests.start(step_tests.tester, step_tests.request(acknowledged_side_effect="non_idempotent"))
    assert accepted.side_effect == "non_idempotent"


async def test_a_step_that_doesnt_match_the_saved_draft_is_refused(step_tests):
    for changes in ({"step_id": "missing"}, {"uses": "echo-action@9.0.0"}, {"kind": "llm"}):
        with pytest.raises(CatalogError) as refused:
            await step_tests.start(step_tests.tester, step_tests.request(**changes))
        assert refused.value.status in {404, 422}
    assert await step_tests.nothing_written()
