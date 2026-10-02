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

"""Read-only discovery of the caller's own grants and the scopes they cover."""

from pyfly.container.stereotypes import service
from sqlalchemy import text

from firefly_weave.access.authorization import contains
from firefly_weave.access.models import Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import AccessService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.identity import (
    IdentityEnvironment,
    IdentityGrant,
    IdentityProject,
    IdentityView,
    IdentityWorkspace,
)
from firefly_weave.persistence.uow import UnitOfWork


def project_grants(actor: Principal) -> list[IdentityGrant]:
    result = []
    for grant in actor.grants:
        capabilities = ROLE_CAPABILITIES[grant.role]
        if actor.kind == "worker":
            capabilities = capabilities & ROLE_CAPABILITIES["worker"]
        result.append(
            IdentityGrant(
                role=grant.role, scope=grant.scope, resources=list(grant.resources), capabilities=sorted(capabilities)
            )
        )
    return result


@service
class IdentityDiscoveryService:
    def __init__(self, access: AccessService, uow: UnitOfWork) -> None:
        self.access, self.uow = access, uow

    async def read(self, actor: Principal) -> IdentityView:
        actor = await self.access.load_principal(actor.id)
        grants = project_grants(actor)
        tenants = sorted({g.scope.tenant_id for g in grants if g.scope is not None and g.capabilities}, key=str)
        workspaces = []
        truncated = len(tenants) > 100
        for tenant in tenants[:100]:
            async with self.uow.open(Scope(tenant_id=tenant), mutation=False) as tx:
                # Recheck current membership in the same snapshot as the names we expose.
                current = await self.access.load_principal(actor.id, tx=tx)
                scoped = [
                    g for g in project_grants(current) if g.scope and g.scope.tenant_id == tenant and g.capabilities
                ]
                if not scoped:
                    continue
                name = await tx.session.scalar(text("SELECT name FROM tenants WHERE id=:id"), {"id": tenant})
                if name is None:
                    continue
                visible = []
                project_ids = [g.scope.project_id for g in scoped if g.scope and g.scope.project_id]
                all_projects = any(g.scope and g.scope.project_id is None for g in scoped)
                projects = (
                    (
                        await tx.session.execute(
                            text(
                                "SELECT id,name FROM projects WHERE tenant_id=:t "
                                "AND (:all OR id=ANY(CAST(:ids AS uuid[]))) ORDER BY id LIMIT 101"
                            ),
                            {"t": tenant, "all": all_projects, "ids": project_ids},
                        )
                    )
                    .mappings()
                    .all()
                )
                truncated |= len(projects) > 100
                for project in projects[:100]:
                    pid = project["id"]
                    applicable = [
                        g for g in scoped if g.scope and (g.scope.project_id is None or g.scope.project_id == pid)
                    ]
                    if not applicable:
                        continue
                    environment_ids = [g.scope.environment_id for g in applicable if g.scope and g.scope.environment_id]
                    all_environments = any(g.scope and g.scope.environment_id is None for g in applicable)
                    environments = (
                        (
                            await tx.session.execute(
                                text(
                                    "SELECT id,name FROM environments WHERE tenant_id=:t AND project_id=:p "
                                    "AND (:all OR id=ANY(CAST(:ids AS uuid[]))) ORDER BY id LIMIT 101"
                                ),
                                {"t": tenant, "p": pid, "all": all_environments, "ids": environment_ids},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    truncated |= len(environments) > 100
                    allowed = [
                        IdentityEnvironment(id=e["id"], name=e["name"])
                        for e in environments[:100]
                        if any(
                            contains(g.scope, Scope(tenant_id=tenant, project_id=pid, environment_id=e["id"]))
                            for g in applicable
                        )
                    ]
                    visible.append(IdentityProject(id=pid, name=project["name"], environments=allowed))
                workspaces.append(IdentityWorkspace(id=tenant, name=name, projects=visible))
        return IdentityView(
            principal_id=actor.id, kind=actor.kind, grants=grants, workspaces=workspaces, truncated=truncated
        )
