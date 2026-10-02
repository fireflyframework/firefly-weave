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

"""Identity projection exposes only the caller's applicable capability grants."""

from uuid import uuid4

from firefly_weave.access.discovery import project_grants
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope


def test_worker_projection_cannot_advertise_human_authority():
    actor = Principal(
        id=uuid4(), kind="worker", grants=(Grant(role="task_participant", scope=Scope(tenant_id=uuid4())),)
    )
    result = project_grants(actor)
    assert not result[0].capabilities


def test_projection_preserves_scope_and_resource_limit():
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    actor = Principal(
        id=uuid4(), kind="human", grants=(Grant(role="task_participant", scope=scope, resources=("task-1",)),)
    )
    result = project_grants(actor)
    assert result[0].scope == scope
    assert result[0].resources == ["task-1"]
    assert "human_task.complete" in result[0].capabilities
