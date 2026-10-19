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

"""Target permissions are the finite intersection of what the token and the stored principal may read."""

from uuid import UUID, uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
FIRST, SECOND, THIRD = sorted((uuid4(), uuid4(), uuid4()))


def principal(*grants, kind="application", active=True):
    return Principal(id=uuid4(), kind=kind, active=active, grants=grants)


def reader(*targets, role="deployment_reader", scope=SCOPE):
    return Grant(role=role, scope=scope, resources=tuple(str(target) for target in targets))


def permissions(actor, current, capability="deployment.read"):
    from firefly_weave.operations.permissions import target_permissions

    return target_permissions(AuthorizationService(), actor, current, SCOPE, capability, AuditContext())


def test_unrestricted_grants_on_both_principals_read_every_target():
    assert permissions(principal(reader()), principal(reader())) is None


@pytest.mark.parametrize(
    "actor,current,expected",
    [
        (principal(reader()), principal(reader(FIRST, SECOND)), (FIRST, SECOND)),
        (principal(reader(SECOND, FIRST)), principal(reader()), (FIRST, SECOND)),
        (principal(reader(FIRST, SECOND)), principal(reader(SECOND, THIRD)), (SECOND,)),
        (principal(reader(FIRST), reader(THIRD)), principal(reader(THIRD), reader(FIRST)), (FIRST, THIRD)),
        (principal(reader(FIRST)), principal(reader(SECOND)), ()),
    ],
)
def test_restricted_grants_intersect_in_sorted_order(actor, current, expected):
    assert permissions(actor, current) == expected


@pytest.mark.parametrize(
    "grants",
    [
        (),
        (Grant(role="viewer", scope=SCOPE),),
        (reader(scope=Scope(tenant_id=SCOPE.tenant_id, project_id=uuid4(), environment_id=SCOPE.environment_id)),),
        (reader(scope=SCOPE.model_copy(update={"environment_id": uuid4()})),),
    ],
)
def test_no_matching_grant_reads_no_target_and_never_raises(grants):
    assert permissions(principal(*grants), principal(reader())) == ()
    assert permissions(principal(reader()), principal(*grants)) == ()


def test_inactive_principals_and_workers_read_no_target():
    assert permissions(principal(reader(), active=False), principal(reader())) == ()
    assert permissions(principal(reader()), principal(reader(), kind="worker")) == ()


def test_resources_that_are_not_target_ids_are_ignored():
    grant = Grant(
        role="deployment_reader",
        scope=SCOPE,
        resources=("not-a-target", str(FIRST), "../x", str(SECOND).upper(), "{" + str(THIRD) + "}"),
    )
    assert permissions(principal(grant), principal(reader())) == (FIRST,)


def test_an_unrestricted_grant_alongside_restricted_ones_reads_every_target():
    assert permissions(principal(reader(FIRST), reader()), principal(reader())) is None


def test_project_wide_grants_cover_the_environment():
    project = Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id)
    assert permissions(principal(reader(scope=project)), principal(reader(FIRST, scope=project))) == (FIRST,)


def test_only_the_named_capability_counts():
    planner = Grant(role="deployment_planner", scope=SCOPE, resources=(str(FIRST),))
    approver = Grant(role="deployment_approver", scope=SCOPE, resources=(str(SECOND),))
    both = principal(planner, approver)
    assert permissions(both, both) == (FIRST, SECOND)
    assert permissions(both, both, "deployment.approve") == (SECOND,)
    assert permissions(both, both, "deployment.plan") == (FIRST,)


def test_every_resource_is_checked_through_the_authorization_service():
    from firefly_weave.operations.permissions import target_permissions

    checks: list[tuple[UUID, str | None]] = []

    class Recording(AuthorizationService):
        def require(self, principal, scope, capability, *, resource=None, context=None):
            checks.append((principal.id, resource))
            super().require(principal, scope, capability, resource=resource, context=context)

    actor, current = principal(reader(FIRST, SECOND)), principal(reader())
    assert target_permissions(Recording(), actor, current, SCOPE, "deployment.read", AuditContext()) == (
        FIRST,
        SECOND,
    )
    assert sorted(checks, key=str) == sorted(
        [(actor.id, str(FIRST)), (actor.id, str(SECOND)), (current.id, None)], key=str
    )
