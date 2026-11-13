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
"""Each environment's test policy is off until an administrator turns it on, with revisions and an audit trail."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.step_tests import EnvironmentTestPolicy, EnvironmentTestPolicyUpdate
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests import policy as policies

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
OTHER = SCOPE.model_copy(update={"environment_id": uuid4()})


def person(*roles, scope=SCOPE):
    return Principal(id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=scope) for role in roles))


ADMIN = person("tenant_admin", "viewer")


class Table:
    """The environment_test_policies rows and the audit rows written through one fake session per transaction."""

    def __init__(self):
        self.rows, self.audits, self.opened = {}, [], []

    def session(self, scope):
        table = self
        key = (scope.tenant_id, scope.project_id, scope.environment_id)

        class Session:
            async def execute(self, statement, values=None):
                sql = " ".join(str(statement).split())
                values = values or {}
                if sql.startswith("INSERT INTO access_audit"):
                    table.audits.append(json.loads(values["event"]))
                    return SimpleNamespace(rowcount=1)
                assert (values["tenant"], values["project"], values["environment"]) == key, sql
                if sql.startswith("SELECT test_calls,revision FROM environment_test_policies"):
                    row = table.rows.get(key)
                    return SimpleNamespace(first=lambda: row)
                if sql.startswith("INSERT INTO environment_test_policies"):
                    if key in table.rows:
                        return SimpleNamespace(first=lambda: None)
                    table.rows[key] = (values["calls"], 1)
                    return SimpleNamespace(first=lambda: (1,))
                if sql.startswith("UPDATE environment_test_policies"):
                    current = table.rows.get(key)
                    if current is None or current[1] != values["expected"]:
                        return SimpleNamespace(first=lambda: None)
                    table.rows[key] = (values["calls"], current[1] + 1)
                    return SimpleNamespace(first=lambda: (current[1] + 1,))
                raise AssertionError(sql)

        return Session()

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        self.opened.append(mutation)
        yield SimpleNamespace(session=self.session(scope), scope=scope)


@pytest.fixture
def table():
    return Table()


def service(table):
    return policies.TestPolicyService(table, AuthorizationService())


async def save(table, actor, calls, expected, scope=SCOPE):
    return await service(table).save(
        actor, scope, EnvironmentTestPolicyUpdate(test_calls=calls), expected, context=AuditContext()
    )


async def test_an_environment_without_a_policy_reads_disabled_at_revision_0(table):
    policy = await service(table).read(person("viewer"), SCOPE, context=AuditContext())
    assert policy == EnvironmentTestPolicy(test_calls="disabled", revision=0)
    assert table.opened == [False]


async def test_reading_needs_status_read(table):
    with pytest.raises(AccessDenied):
        await service(table).read(person("developer"), SCOPE, context=AuditContext())
    assert table.opened == []


@pytest.mark.parametrize("roles", [("viewer",), ("operator", "deployer"), ("developer",)])
async def test_saving_needs_environment_management(table, roles):
    with pytest.raises(AccessDenied):
        await save(table, person(*roles), "enabled", 0)
    assert table.rows == {} and table.audits == [] and table.opened == []


async def test_saving_with_the_current_revision_changes_it_and_counts_up(table):
    assert await save(table, ADMIN, "enabled", 0) == EnvironmentTestPolicy(test_calls="enabled", revision=1)
    assert await save(table, ADMIN, "disabled", 1) == EnvironmentTestPolicy(test_calls="disabled", revision=2)
    read = await service(table).read(ADMIN, SCOPE, context=AuditContext())
    assert read == EnvironmentTestPolicy(test_calls="disabled", revision=2)


@pytest.mark.parametrize("stored, expected", [(None, 1), (("enabled", 3), 2), (("enabled", 3), 0)])
async def test_saving_with_another_revision_is_refused_and_changes_nothing(table, stored, expected):
    key = (SCOPE.tenant_id, SCOPE.project_id, SCOPE.environment_id)
    if stored:
        table.rows[key] = stored
    with pytest.raises(CatalogError) as refused:
        await save(table, ADMIN, "disabled", expected)
    assert (refused.value.status, refused.value.code) == (409, "WV-REVISION")
    assert table.rows.get(key) == stored and table.audits == []


async def test_every_change_is_audited_with_the_old_and_new_values_and_nothing_else(table):
    await save(table, ADMIN, "enabled", 0)
    await save(table, ADMIN, "disabled", 1)
    assert [audit["details"] for audit in table.audits] == [
        {"old": "disabled", "new": "enabled", "revision": 1},
        {"old": "enabled", "new": "disabled", "revision": 2},
    ]
    assert {(audit["action"], audit["capability"]) for audit in table.audits} == {
        ("test_policy.save", "environment.manage")
    }


async def test_the_policy_is_kept_per_environment(table):
    await save(table, ADMIN, "enabled", 0)
    other = person("tenant_admin", "viewer", scope=OTHER)
    assert await service(table).read(other, OTHER, context=AuditContext()) == EnvironmentTestPolicy(
        test_calls="disabled", revision=0
    )


@pytest.mark.parametrize("stored, enabled", [(None, False), (("disabled", 2), False), (("enabled", 1), True)])
async def test_the_step_test_service_reads_whether_calls_are_on_inside_its_transaction(table, stored, enabled):
    if stored:
        table.rows[(SCOPE.tenant_id, SCOPE.project_id, SCOPE.environment_id)] = stored
    async with table.open(SCOPE) as tx:
        assert await service(table).enabled(tx) is enabled
