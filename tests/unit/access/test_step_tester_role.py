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
"""Running a step for real needs its own role, which grants nothing else and stays in the demo environment."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope
from firefly_weave.sdk import platform

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations/versions"


def test_the_step_tester_role_holds_only_step_test():
    assert ROLE_CAPABILITIES["step_tester"] == frozenset({"step.test"})


def test_no_other_role_can_test_steps():
    holders = {role for role, capabilities in ROLE_CAPABILITIES.items() if "step.test" in capabilities}
    assert holders == {"step_tester"}


def test_the_local_owner_tests_steps_on_the_demo_environment_only():
    scope = {"tenant_id": "t", "project_id": "p", "environment_id": "e"}
    assert "step_tester" in platform.LOCAL_OWNER_ROLES
    assert platform._person_grants(("step_tester",), scope) == [{"role": "step_tester", "scope": scope}]


def test_default_accounts_cant_test_steps_and_administrators_can_grant_it():
    assert "step_tester" not in platform.DEFAULT_PERSON_ROLES
    assert "step_tester" in platform.PERSON_ROLES
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    tester = Principal(id=uuid4(), kind="human", grants=(Grant(role="step_tester", scope=scope),))
    assert AuthorizationService().allows(tester, scope, "step.test")
    assert not AuthorizationService().allows(tester, scope, "run.start")


def test_the_role_check_accepts_every_role_and_nothing_new():
    path = MIGRATIONS / "0040_editor_step_tests.py"
    spec = importlib.util.spec_from_file_location("step_tests_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert set(migration.ROLES) == set(ROLE_CAPABILITIES)
    assert len(set(migration.ROLES)) == len(migration.ROLES)
