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
"""Typed member administration over the shared bounded, authenticated SDK."""

from typing import cast
from uuid import UUID

from firefly_weave.contracts.members import (
    MemberBinding,
    MemberGrantRequest,
    PrincipalCreateRequest,
    PrincipalIdentityRequest,
    PrincipalIdentityResult,
    PrincipalRecord,
    PrincipalStatusRequest,
)
from firefly_weave.contracts.public import Page, Revoked
from firefly_weave.sdk.client import WeaveClient


class MembersClient:
    def __init__(self, client: WeaveClient) -> None:
        self.client = client

    async def principals(self, *, limit: int = 50, cursor: str | None = None) -> Page[PrincipalRecord]:
        query: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return cast(Page[PrincipalRecord], await self.client.invoke("principals.list", query=query))

    async def create_principal(self, request: PrincipalCreateRequest) -> PrincipalRecord:
        return cast(PrincipalRecord, await self.client.invoke("principals.create", body=request))

    async def link_identity(self, identifier: UUID, request: PrincipalIdentityRequest) -> PrincipalIdentityResult:
        return cast(
            PrincipalIdentityResult, await self.client.invoke("principals.link", identifier=identifier, body=request)
        )

    async def set_active(self, identifier: UUID, active: bool) -> PrincipalRecord:
        return cast(
            PrincipalRecord,
            await self.client.invoke(
                "principals.status", identifier=identifier, body=PrincipalStatusRequest(active=active)
            ),
        )

    async def bindings(self, *, limit: int = 50, cursor: str | None = None) -> Page[MemberBinding]:
        query: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return cast(Page[MemberBinding], await self.client.invoke("members.list", query=query))

    async def grant(self, request: MemberGrantRequest) -> MemberBinding:
        return cast(MemberBinding, await self.client.invoke("members.grant", body=request))

    async def revoke(self, identifier: UUID) -> Revoked:
        return cast(Revoked, await self.client.invoke("members.revoke", identifier=identifier))
