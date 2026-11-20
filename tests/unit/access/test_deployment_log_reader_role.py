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

"""Container logs have their own role: it reads them and nothing else, and no other role gains it."""

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
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def person(*roles: str, resources: tuple[str, ...] = ()) -> Principal:
    return Principal(
        id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=SCOPE, resources=resources) for role in roles)
    )


def test_the_deployment_log_reader_reads_container_logs_only():
    assert ROLE_CAPABILITIES["deployment_log_reader"] == frozenset({"deployment_log.read"})
    reader = person("deployment_log_reader")
    assert AuthorizationService().allows(reader, SCOPE, "deployment_log.read")
    for capability in ("deployment.read", "run.read", "status.read", "target.manage", "runner.claim"):
        assert not AuthorizationService().allows(reader, SCOPE, capability), capability


def test_no_other_role_reads_container_logs():
    holders = {role for role, capabilities in ROLE_CAPABILITIES.items() if "deployment_log.read" in capabilities}
    assert holders == {"deployment_log_reader"}


def test_a_grant_for_one_target_reads_that_targets_logs_only():
    target, other = str(uuid4()), str(uuid4())
    reader = person("deployment_log_reader", "deployment_reader", resources=(target,))
    for capability in ("deployment_log.read", "deployment.read"):
        assert AuthorizationService().allows(reader, SCOPE, capability, resource=target)
        assert not AuthorizationService().allows(reader, SCOPE, capability, resource=other)


def test_the_local_owner_reads_container_logs_and_other_people_get_it_by_grant():
    assert "deployment_log_reader" in platform.LOCAL_OWNER_ROLES
    assert "deployment_log_reader" in platform.PERSON_ROLES
    assert "deployment_log_reader" not in platform.DEFAULT_PERSON_ROLES
    owner = person(*platform.LOCAL_OWNER_ROLES)
    assert all(
        AuthorizationService().allows(owner, SCOPE, capability)
        for capability in ("deployment_log.read", "deployment.read")
    )
    assert not AuthorizationService().allows(person(*platform.DEFAULT_PERSON_ROLES), SCOPE, "deployment_log.read")


def test_the_newest_role_check_accepts_the_role():
    for script in ScriptDirectory(str(MIGRATIONS.parent)).walk_revisions("base", SCHEMA_VERSION):
        roles = getattr(script.module, "ROLES", None)
        if roles is not None:
            assert "deployment_log_reader" in roles, script.revision
            return
    raise AssertionError("No migration rebuilds the role check")
