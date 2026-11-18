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

"""Writing log entries is a worker's capability alone: no person's role holds it."""

from uuid import uuid4

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope
from firefly_weave.sdk import platform


def test_the_worker_may_write_log_entries_and_nobody_else_may():
    holders = {role for role, capabilities in ROLE_CAPABILITIES.items() if "task.log" in capabilities}
    assert holders == {"worker"}
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    worker = Principal(id=uuid4(), kind="worker", grants=(Grant(role="worker", scope=scope),))
    assert AuthorizationService().allows(worker, scope, "task.log")
    owner = Principal(
        id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=scope) for role in platform.LOCAL_OWNER_ROLES)
    )
    assert not AuthorizationService().allows(owner, scope, "task.log")
