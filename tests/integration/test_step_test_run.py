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
"""Starting, reading and canceling a step test on real PostgreSQL: one test run, one transaction, creator only."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.step_tests import StepTestRequest
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()
TABLES = ("test_activation_details", "activation_revisions WHERE test", "definition_versions WHERE test")


@dataclass
class StepTests:
    """A saved draft of the worker fixture workflow, a step tester, and an environment whose test calls are on."""

    service: Any
    access: Any
    admin: Principal
    tester: Principal
    scope: Scope
    draft: UUID
    owner: Any

    async def person(self, *roles: str, resources: tuple[str, ...] = ()) -> Principal:
        identifier = await self.access.create_principal(self.admin, "application")
        for role in roles:
            scope = self.scope.model_copy(update={"environment_id": None}) if role == "developer" else self.scope
            grant_resources = resources if role == "deployer" else ()
            await self.access.grant(self.admin, identifier, Grant(role=role, scope=scope, resources=grant_resources))
        return await self.access.load_principal(identifier)

    def request(self, **changes: Any) -> StepTestRequest:
        values: dict[str, Any] = {
            "kind": "action",
            "step_id": "work",
            "uses": "echo-action@2.0.0",
            "input": 3,
            "connections": {},
            "draft_id": str(self.draft),
            "draft_revision": 1,
            "timeout_seconds": 60,
        }
        values.update(changes)
        return StepTestRequest.model_validate_json(json.dumps(values))

    async def scalar(self, sql: str, **values: Any) -> Any:
        async with self.owner() as session:
            return await session.scalar(text(sql), values)

    async def nothing_written(self) -> bool:
        return all([await self.scalar(f"SELECT count(*) FROM {table}") == 0 for table in TABLES])

    async def policy(self, calls: str) -> None:
        async with self.owner() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO environment_test_policies VALUES(:tenant,:project,:environment,:calls,1,now(),"
                    ":admin) ON CONFLICT(tenant_id,project_id,environment_id) DO UPDATE SET test_calls=:calls"
                ),
                {
                    "tenant": self.scope.tenant_id,
                    "project": self.scope.project_id,
                    "environment": self.scope.environment_id,
                    "calls": calls,
                    "admin": self.admin.id,
                },
            )

    async def start(self, actor: Principal, request: StepTestRequest | None = None, key: str = "click-1") -> Any:
        return await self.service.start(actor, self.scope, request or self.request(), key, context=CONTEXT)


async def make_step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture) -> StepTests:
    from firefly_weave.step_tests.service import StepTestService

    sessions, owner, access, _ = access_db
    admin, _ = provisioned
    scope = worker_setup[4]
    draft = uuid4()
    async with owner() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO draft_revisions(id,tenant_id,project_id,revision,document) "
                "VALUES(:id,:tenant,:project,1,cast(:document AS jsonb))"
            ),
            {
                "id": draft,
                "tenant": scope.tenant_id,
                "project": scope.project_id,
                "document": worker_runtime_fixture["source"],
            },
        )
    helper = StepTests(services(sessions).resolve(StepTestService), access, admin, admin, scope, draft, owner)
    helper.tester = await helper.person("step_tester", "developer")
    await helper.policy("enabled")
    return helper


@pytest.fixture
async def step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture):
    return await make_step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture)


async def test_a_step_test_starts_one_test_run_flagged_as_a_test(step_tests):
    accepted = await step_tests.start(step_tests.tester)
    assert (accepted.status, accepted.side_effect) == ("running", "read_only")
    facts = await step_tests.scalar("SELECT to_jsonb(f) FROM run_facts f WHERE run_id=:run", run=accepted.run_id)
    assert (facts["test"], facts["origin"], facts["workflow_name"]) == (True, "test", "worker-flow")
    assert facts["workflow_version"] == "1.0.0+test." + accepted.id.hex[:12]
    assert facts["activation_id"] == str(accepted.id)
    details = await step_tests.scalar(
        "SELECT to_jsonb(d) FROM test_activation_details d WHERE activation_id=:id", id=accepted.id
    )
    assert (details["run_id"], details["step_id"], details["source"]) == (str(accepted.run_id), "work", "step")
    assert details["principal_id"] == str(step_tests.tester.id)


async def test_a_replay_with_the_same_key_starts_no_second_run(step_tests):
    first = await step_tests.start(step_tests.tester)
    again = await step_tests.start(step_tests.tester)
    assert again == first
    assert await step_tests.scalar("SELECT count(*) FROM test_activation_details") == 1
    with pytest.raises(CatalogError) as conflict:
        await step_tests.start(step_tests.tester, step_tests.request(input=4))
    assert (conflict.value.status, conflict.value.code) == (409, "WV-IDEMPOTENCY-CONFLICT")


async def test_another_person_cant_read_or_cancel_it(step_tests):
    accepted = await step_tests.start(step_tests.tester)
    other = await step_tests.person("step_tester", "developer", "tenant_admin")
    for action in (step_tests.service.read, step_tests.service.cancel):
        with pytest.raises(CatalogError) as hidden:
            await action(other, step_tests.scope, accepted.id, context=CONTEXT)
        assert (hidden.value.status, hidden.value.code) == (404, "WV-NOT-FOUND")


async def test_cancel_stops_a_running_test_and_answers_an_ended_one_as_it_ended(step_tests):
    accepted = await step_tests.start(step_tests.tester)
    running = await step_tests.service.read(step_tests.tester, step_tests.scope, accepted.id, context=CONTEXT)
    assert (running.id, running.run_id, running.status) == (accepted.id, accepted.run_id, "running")
    cancelled = await step_tests.service.cancel(step_tests.tester, step_tests.scope, accepted.id, context=CONTEXT)
    assert cancelled.status == "cancelled"
    again = await step_tests.service.cancel(step_tests.tester, step_tests.scope, accepted.id, context=CONTEXT)
    assert again.status == "cancelled" and again.ended_at == cancelled.ended_at


async def test_a_person_without_run_start_can_still_run_a_step_test(step_tests):
    held = {capability for grant in step_tests.tester.grants for capability in ROLE_CAPABILITIES[grant.role]}
    assert "run.start" not in held and "step.test" in held
    accepted = await step_tests.start(step_tests.tester)
    assert accepted.status == "running"


async def test_everything_is_written_in_one_transaction(step_tests, monkeypatch):
    from firefly_weave.runtime.service import RuntimeService

    async def broken(*args, **kwargs):
        raise CatalogError(503, "WV-COMPATIBILITY", "The run couldn't start")

    monkeypatch.setattr(RuntimeService, "start", broken)
    with pytest.raises(CatalogError):
        await step_tests.start(step_tests.tester)
    assert await step_tests.nothing_written()
