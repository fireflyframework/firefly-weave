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
"""Each environment's test policy on real PostgreSQL: kept per environment, revisioned, and isolated by tenant."""

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.contracts.step_tests import EnvironmentTestPolicy, EnvironmentTestPolicyUpdate
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests import policy as policies

pytestmark = pytest.mark.integration
ENABLED = EnvironmentTestPolicyUpdate(test_calls="enabled")


@pytest.fixture
async def policy(services, access_db, provisioned):
    sessions, owner, access, _ = access_db
    admin, scopes = provisioned
    for scope in scopes:
        await access.grant(admin, admin.id, Grant(role="viewer", scope=scope))
    actor = await access.load_principal(admin.id)
    return services(sessions).resolve(policies.TestPolicyService), actor, scopes, owner


async def test_the_policy_is_kept_per_environment(policy):
    service, actor, (local, other), _ = policy
    context = AuditContext()
    assert await service.save(actor, local, ENABLED, 0, context=context) == EnvironmentTestPolicy(
        test_calls="enabled", revision=1
    )
    assert await service.read(actor, local, context=context) == EnvironmentTestPolicy(test_calls="enabled", revision=1)
    assert await service.read(actor, other, context=context) == EnvironmentTestPolicy(test_calls="disabled", revision=0)
    with pytest.raises(CatalogError) as stale:
        await service.save(actor, local, ENABLED, 0, context=context)
    assert stale.value.code == "WV-REVISION"


async def test_another_tenant_cant_read_it(policy):
    service, actor, (local, other), owner = policy
    await service.save(actor, local, ENABLED, 0, context=AuditContext())
    async with owner() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE weave_app"))
        for name, value in (
            ("weave.tenant_id", other.tenant_id),
            ("weave.project_id", local.project_id),
            ("weave.environment_id", local.environment_id),
        ):
            await session.execute(text("SELECT set_config(:name,:value,true)"), {"name": name, "value": str(value)})
        assert await session.scalar(text("SELECT count(*) FROM environment_test_policies")) == 0


async def test_every_change_is_audited_with_the_old_and_new_values(policy):
    service, actor, (local, _), owner = policy
    await service.save(actor, local, ENABLED, 0, context=AuditContext())
    async with owner() as session:
        details = await session.scalar(
            text("SELECT event->'details' FROM access_audit WHERE action='test_policy.save' AND target=:target"),
            {"target": str(local.environment_id)},
        )
    assert details == {"old": "disabled", "new": "enabled", "revision": 1}
