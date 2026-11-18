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

"""Alerts have their own role: it reads and manages alerts, grants nothing else, and no other role gains it."""

from pathlib import Path
from uuid import uuid4

from alembic.script import ScriptDirectory

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope
from firefly_weave.persistence.migrations import SCHEMA_VERSION
from firefly_weave.sdk import platform

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations/versions"
ALERT_CAPABILITIES = frozenset({"alert.read", "alert.manage"})


def newest_role_check() -> tuple[str, tuple[str, ...]]:
    """The newest migration on the line to the schema version that rebuilds the role check, and its roles.

    A migration that adds no role doesn't rebuild the check, so the walk passes it by.
    """
    for script in ScriptDirectory(str(MIGRATIONS.parent)).walk_revisions("base", SCHEMA_VERSION):
        roles = getattr(script.module, "ROLES", None)
        if roles is not None:
            return script.revision, tuple(roles)
    raise AssertionError("No migration rebuilds the role check")


def test_the_alert_manager_role_reads_and_manages_alerts_only():
    assert ROLE_CAPABILITIES["alert_manager"] == ALERT_CAPABILITIES
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    manager = Principal(id=uuid4(), kind="human", grants=(Grant(role="alert_manager", scope=scope),))
    assert AuthorizationService().allows(manager, scope, "alert.read")
    assert AuthorizationService().allows(manager, scope, "alert.manage")
    for capability in ("status.read", "run.read", "environment.manage", "deployment.read", "email.send"):
        assert not AuthorizationService().allows(manager, scope, capability), capability


def test_no_other_role_holds_an_alert_capability():
    holders = {role for role, capabilities in ROLE_CAPABILITIES.items() if capabilities & ALERT_CAPABILITIES}
    assert holders == {"alert_manager"}
    assert "alert.read" not in ROLE_CAPABILITIES["viewer"] and "alert.read" not in ROLE_CAPABILITIES["operator"]


def test_the_local_owner_manages_alerts():
    scope = {"tenant_id": "t", "project_id": "p", "environment_id": "e"}
    assert "alert_manager" in platform.LOCAL_OWNER_ROLES
    assert platform._person_grants(("alert_manager",), scope) == [{"role": "alert_manager", "scope": scope}]


def test_the_local_owner_can_send_alert_email_as_themselves():
    # An email channel sends as the person who saved it, so the owner needs email.send to set one up.
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    owner = Principal(
        id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=scope) for role in platform.LOCAL_OWNER_ROLES)
    )
    assert all(AuthorizationService().allows(owner, scope, capability) for capability in ("alert.manage", "email.send"))
    assert "email_manager" not in platform.LOCAL_OWNER_ROLES


def test_default_accounts_cant_see_alerts_and_administrators_can_grant_it():
    assert "alert_manager" not in platform.DEFAULT_PERSON_ROLES
    assert "alert_manager" in platform.PERSON_ROLES
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    grants = tuple(Grant(role=role, scope=scope) for role in platform.DEFAULT_PERSON_ROLES)
    person = Principal(id=uuid4(), kind="human", grants=grants)
    assert not AuthorizationService().allows(person, scope, "alert.read")


def test_the_newest_role_check_accepts_every_role_and_nothing_new():
    _, roles = newest_role_check()
    assert set(roles) == set(ROLE_CAPABILITIES)
    assert len(set(roles)) == len(roles)
