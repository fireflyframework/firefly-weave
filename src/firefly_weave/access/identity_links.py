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

"""Only explicit provider/issuer/subject links resolve a stable local principal."""

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from firefly_weave.access.models import CredentialIdentity, Principal, VerifiedIdentity
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.providers.base import PrincipalResolver
from firefly_weave.access.repository import load_principal


@service
class IdentityResolver(PrincipalResolver):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def resolve(self, identity: VerifiedIdentity) -> Principal:
        async with self.sessions.begin() as session:
            principal_id = await session.scalar(
                text(
                    "SELECT principal_id FROM identity_links WHERE provider_id=:provider AND "
                    "issuer=:issuer AND subject=:subject"
                ),
                {"provider": identity.provider_id, "issuer": identity.issuer, "subject": identity.subject},
            )
            if principal_id is None:
                raise AuthenticationFailed()
            principal = await load_principal(session, principal_id)
            if (principal.kind == "human") != (identity.actor_kind == "human"):
                raise AuthenticationFailed()
            return principal.model_copy(
                update={
                    "credential_identity": CredentialIdentity(
                        provider_id=identity.provider_id,
                        issuer=identity.issuer,
                        subject=identity.subject,
                        client_id=identity.client_id,
                    )
                }
            )
