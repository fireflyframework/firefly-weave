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

"""Transaction-local authorization data loading, with principal-only grant visibility."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.contracts.access import Scope


async def load_principal(session: AsyncSession, principal_id: UUID) -> Principal:
    await session.execute(text("SELECT set_config('weave.principal_id',:id,true)"), {"id": str(principal_id)})
    row = (
        (await session.execute(text("SELECT id,kind,active FROM principals WHERE id=:id"), {"id": principal_id}))
        .mappings()
        .first()
    )
    if row is None or not row["active"]:
        raise AuthenticationFailed()
    grants = []
    for binding in (
        await session.execute(text("SELECT * FROM role_bindings WHERE principal_id=:id"), {"id": principal_id})
    ).mappings():
        grants.append(
            Grant(
                role=binding["role"],
                scope=Scope(
                    tenant_id=binding["tenant_id"],
                    project_id=binding["project_id"],
                    environment_id=binding["environment_id"],
                ),
                resources=tuple(binding["resources"]),
            )
        )
    if await session.scalar(
        text("SELECT principal_id FROM platform_administrators WHERE principal_id=:id"), {"id": principal_id}
    ):
        grants.append(Grant(role="platform_admin", scope=None))
    return Principal(**row, grants=tuple(grants))
