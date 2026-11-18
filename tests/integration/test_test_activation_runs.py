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

"""Running a draft test activation: only its creator, only while it lasts, within the step test limits."""

import json
from uuid import uuid4

import pytest
from test_test_activation_create import CONTEXT, MOVE, NOTIFY, SUBMIT, drafts, refused  # noqa: F401
from transaction_platform import workflow

from firefly_weave.contracts.step_tests import DraftTestRunRequest, StepTestRequest

pytestmark = pytest.mark.integration


async def run(env, activation, actor=None, value=None, key=None):
    return await env.service.start(
        actor or env.tester,
        env.scope,
        activation.id,
        DraftTestRunRequest(input=value or MOVE),
        key,
        context=CONTEXT,
    )


async def test_the_creator_starts_test_runs_without_run_start(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    first = await run(env, created)
    second = await run(env, created)
    assert first.id != second.id and first.activation.id == second.activation.id == created.id
    facts = await env.query(
        "SELECT run_id,test,origin FROM run_facts WHERE activation_id=:a ORDER BY run_id", a=created.id
    )
    assert sorted(row["run_id"] for row in facts) == sorted([first.id, second.id])
    assert all(row["test"] and row["origin"] == "test" for row in facts)
    records = await env.query("SELECT target,event FROM access_audit WHERE action='test_activation.run' ORDER BY id")
    assert [record["event"]["details"] for record in records] == [
        {"run_id": str(first.id)},
        {"run_id": str(second.id)},
    ]
    assert "moveId" not in json.dumps([record["event"] for record in records])


async def test_a_key_sent_again_answers_the_same_run(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    assert (await run(env, created, key="click-1")).id == (await run(env, created, key="click-1")).id


async def test_anyone_but_the_creator_is_told_it_doesnt_exist(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    other = await env.person("step_tester", "developer", "deployer", "viewer")
    error = await refused(run(env, created, actor=other))
    assert (error.status, error.code) == (404, "WV-NOT-FOUND")
    error = await refused(run(env, created.model_copy(update={"id": uuid4()})))
    assert (error.status, error.code) == (404, "WV-NOT-FOUND")


async def test_an_expired_test_activation_refuses_runs(drafts):  # noqa: F811
    env = drafts
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    await env.execute(
        "UPDATE test_activation_details SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' "
        "WHERE activation_id=:a",
        a=created.id,
    )
    error = await refused(run(env, created))
    assert (error.status, error.code) == (410, "WV-TESTACTIVATION-EXPIRED")


async def test_step_tests_and_draft_test_runs_share_the_running_limit(drafts, worker_runtime_fixture):  # noqa: F811
    env = drafts
    from firefly_weave.step_tests.service import StepTestService

    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    for _ in range(3):
        await run(env, created)
    step_draft = uuid4()
    await env.execute(
        "INSERT INTO draft_revisions(id,tenant_id,project_id,revision,document) "
        "VALUES(:id,:tenant,:project,1,cast(:document AS jsonb))",
        id=step_draft,
        tenant=env.scope.tenant_id,
        project=env.scope.project_id,
        document=worker_runtime_fixture["source"],
    )
    step = StepTestRequest.model_validate_json(
        json.dumps(
            {
                "kind": "action",
                "step_id": "work",
                "uses": "echo-action@2.0.0",
                "input": 3,
                "draft_id": str(step_draft),
                "draft_revision": 1,
                "timeout_seconds": 60,
            }
        )
    )
    await env.graph.resolve(StepTestService).start(env.tester, env.scope, step, "step-1", context=CONTEXT)
    # Four test runs are running: three of the draft and one step test.
    error = await refused(run(env, created))
    assert (error.status, error.code) == (429, "WV-STEPTEST-BUSY")
    assert error.retry_after is not None and error.retry_after >= 1
    # An expired activation says so, whatever the limits.
    await env.execute(
        "UPDATE test_activation_details SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' "
        "WHERE activation_id=:a",
        a=created.id,
    )
    error = await refused(run(env, created))
    assert (error.status, error.code) == (410, "WV-TESTACTIVATION-EXPIRED")


async def test_runs_of_called_workflows_are_test_runs_too(drafts):  # noqa: F811
    env = drafts
    callee = await env.platform.publish(workflow("notifier", [NOTIFY], callable_=True))
    step = {"id": "call", "kind": "callWorkflow", "uses": "notifier@1.0.0", "with": {"ref": "/input"}}
    document = workflow("call-notifier", [step])
    # Starting the called run is a run of a real activation, which needs run.start.
    caller = await env.person("step_tester", "developer", "deployer", "viewer", "operator")
    draft = await env.save(document)
    bindings = {
        "worker_release_ids": {task: str(env.platform.release) for task in env.platform.needs(document)},
        "workflow_activation_ids": {"notifier@1.0.0": str(callee.id)},
    }
    created = await env.service.create(
        caller, env.scope, env.request(document, draft, bindings=bindings), context=CONTEXT
    )
    parent = await run(env, created, actor=caller)
    await env.platform.scan()
    [child] = await env.query("SELECT activation_id,test,origin FROM run_facts WHERE caller_run_id=:run", run=parent.id)
    assert (child["activation_id"], child["test"], child["origin"]) == (callee.id, True, "call")


async def test_a_draft_run_that_outlives_its_activation_counts_until_its_own_timeout(drafts):  # noqa: F811
    env = drafts
    first = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    for _ in range(4):
        await run(env, first)
    await env.execute(
        "UPDATE test_activation_details SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' "
        "WHERE activation_id=:a",
        a=first.id,
    )
    # The four runs go on after their activation expired: they keep counting until their own timeout, an hour.
    second = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    error = await refused(run(env, second))
    assert (error.status, error.code) == (429, "WV-STEPTEST-BUSY")
    assert 3500 <= error.retry_after <= 3600
