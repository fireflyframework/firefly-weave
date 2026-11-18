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

"""A caller who can hold task.log nowhere is refused before a lease is read; a worker bound to its release is not."""

from uuid import uuid4

import pytest

from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.sdk import platform

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
RELEASE = str(uuid4())
# A worker's grant is bound to its release, so only the check that names that release allows it.
WORKER = Principal(id=uuid4(), kind="worker", grants=(Grant(role="worker", scope=SCOPE, resources=(RELEASE,)),))


def test_a_worker_bound_to_its_release_passes_before_the_release_is_known():
    authorization = AuthorizationService()
    authorization.require_somewhere(WORKER, SCOPE, "task.log")
    assert authorization.allows(WORKER, SCOPE, "task.log", resource=RELEASE)
    assert not authorization.allows(WORKER, SCOPE, "task.log")


@pytest.mark.parametrize(
    ("caller", "where"),
    [
        (
            Principal(
                id=uuid4(),
                kind="human",
                grants=tuple(Grant(role=role, scope=SCOPE) for role in platform.LOCAL_OWNER_ROLES),
            ),
            SCOPE,
        ),
        (WORKER, Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id, environment_id=uuid4())),
        (WORKER.model_copy(update={"active": False}), SCOPE),
    ],
    ids=["a person with every owner role", "a worker in another environment", "a deactivated worker"],
)
def test_a_caller_who_holds_task_log_for_no_release_here_is_refused(caller, where):
    with pytest.raises(AccessDenied):
        AuthorizationService().require_somewhere(caller, where, "task.log")
