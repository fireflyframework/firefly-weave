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

"""Who may see the incidents linked to an incident: readers of every incident of the environment, nobody else."""

from uuid import uuid4

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import incidents

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def principal(*grants: Grant, active: bool = True) -> Principal:
    return Principal(id=uuid4(), kind="human", active=active, grants=grants)


def test_links_need_incident_read_across_the_environment_on_the_token_and_the_stored_principal():
    reads_every_incident = incidents.reads_every_incident
    authorization = AuthorizationService()
    everywhere = principal(Grant(role="operator", scope=SCOPE))
    project = principal(Grant(role="operator", scope=SCOPE.model_copy(update={"environment_id": None})))
    named = principal(Grant(role="operator", scope=SCOPE, resources=(str(uuid4()),)))
    elsewhere = principal(Grant(role="operator", scope=SCOPE.model_copy(update={"environment_id": uuid4()})))
    without = principal(Grant(role="deployer", scope=SCOPE))
    assert reads_every_incident(authorization, (everywhere, everywhere), SCOPE)
    assert reads_every_incident(authorization, (project, everywhere), SCOPE)
    # A grant that names incidents, another environment, a role without incident.read or a disabled principal: none.
    for reader in (named, elsewhere, without, principal(Grant(role="operator", scope=SCOPE), active=False)):
        assert not reads_every_incident(authorization, (reader, everywhere), SCOPE)
        assert not reads_every_incident(authorization, (everywhere, reader), SCOPE)
