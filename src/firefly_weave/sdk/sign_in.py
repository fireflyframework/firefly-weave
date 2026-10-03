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

"""Sign-in orchestration shared by the CLI and the Studio host.

A saved profile chooses the credential store, the server answers who the
signed-in person is without needing a workspace, and the authorized tenants,
projects and environments become a flat, labeled list to pick from. Platform
authorization stays on the server; nothing here grants or widens access.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from firefly_weave.contracts.identity import IdentityView
from firefly_weave.sdk.auth import OAuthSession
from firefly_weave.sdk.client import AsyncTokenProvider, WeaveClient
from firefly_weave.sdk.credentials import CredentialStore, FileCredentialStore, NativeCredentialStore
from firefly_weave.sdk.errors import WeaveError
from firefly_weave.sdk.profiles import AccountHint, PlatformProfile, WorkspaceSelection

if TYPE_CHECKING:
    import httpx


class SignInError(Exception):
    """A sign-in outcome a person must act on, such as `WV-AUTH-NOT-LINKED`; never carries tokens."""

    def __init__(self, code: str, account: AccountHint | None = None) -> None:
        self.code, self.account = code, account
        self.exit_code = 1
        super().__init__(code)


class WorkspaceOption(BaseModel):
    """One authorized tenant > project > environment path, labeled for pickers."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: UUID
    tenant_name: str
    project_id: UUID
    project_name: str
    environment_id: UUID
    environment_name: str
    label: str

    def selection(self) -> WorkspaceSelection:
        """The profile selection; names the profile cannot store safely are left out."""
        return WorkspaceSelection(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            environment_id=self.environment_id,
            tenant_name=_storable(self.tenant_name),
            project_name=_storable(self.project_name),
            environment_name=_storable(self.environment_name),
        )


class IdentityCheck(BaseModel):
    """Identity plus workspace options; `code` is `WV-AUTH-NO-ACCESS` when there is nothing to pick."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    identity: IdentityView
    workspaces: list[WorkspaceOption]
    code: Literal["WV-AUTH-NO-ACCESS"] | None = None
    truncated: bool = False


def _storable(name: str) -> str | None:
    if not name or len(name) > 200 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
        return None
    return name


def session_for(
    profile: PlatformProfile, *, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
) -> OAuthSession:
    """The OAuth session for a profile: its pinned login and its native or explicit file credential store."""
    store: CredentialStore
    if profile.credential_store == "file" and profile.credential_file is not None:
        store = FileCredentialStore(profile.credential_file)
    else:
        store = NativeCredentialStore()
    return OAuthSession(profile.login, store, transport_factory=transport_factory)


def _locally_valid(provider: Any) -> bool:
    status = getattr(provider, "status", None)
    if not callable(status):
        return False
    try:
        value = status()
    except Exception:
        return False
    return isinstance(value, dict) and value.get("authenticated") is True


def identity_failure(error: BaseException, provider: Any, account: AccountHint | None) -> SignInError | None:
    """`WV-AUTH-NOT-LINKED` for a 401 on identity while the local credential is valid, else None.

    The provider accepted the person, but the platform does not know them yet:
    an administrator must link the account (shown through the hint).
    """
    if isinstance(error, WeaveError) and error.status == 401 and _locally_valid(provider):
        return SignInError("WV-AUTH-NOT-LINKED", account)
    return None


async def read_identity(
    profile: PlatformProfile,
    provider: AsyncTokenProvider | Callable[[], str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> IdentityView:
    """`GET /api/v1/identity` against the profile server; no workspace scope is needed."""
    try:
        async with WeaveClient(profile.server, provider, None, transport=transport) as client:
            return cast(IdentityView, await client.invoke("identity.read"))
    except WeaveError as error:
        mapped = identity_failure(error, provider, profile.account)
        if mapped is not None:
            raise mapped from None
        raise


def workspace_options(identity: IdentityView) -> list[WorkspaceOption]:
    """Flatten tenant > project > environment into unique options sorted by their label."""
    options: dict[tuple[UUID, UUID, UUID], WorkspaceOption] = {}
    for tenant in identity.workspaces:
        for project in tenant.projects:
            for environment in project.environments:
                options.setdefault(
                    (tenant.id, project.id, environment.id),
                    WorkspaceOption(
                        tenant_id=tenant.id,
                        tenant_name=tenant.name,
                        project_id=project.id,
                        project_name=project.name,
                        environment_id=environment.id,
                        environment_name=environment.name,
                        label=f"{tenant.name} / {project.name} / {environment.name}",
                    ),
                )
    return sorted(
        options.values(),
        key=lambda option: (
            option.label.casefold(),
            option.label,
            str(option.tenant_id),
            str(option.project_id),
            str(option.environment_id),
        ),
    )


def check_identity(identity: IdentityView) -> IdentityCheck:
    """Workspace options for an identity; an empty list is reported, not raised, so the identity can be shown."""
    options = workspace_options(identity)
    return IdentityCheck(
        identity=identity,
        workspaces=options,
        code=None if options else "WV-AUTH-NO-ACCESS",
        truncated=identity.truncated,
    )


async def verify_sign_in(
    profile: PlatformProfile,
    provider: AsyncTokenProvider | Callable[[], str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> IdentityCheck:
    """Read the identity and its workspace options in one step (raises `SignInError` when not linked)."""
    return check_identity(await read_identity(profile, provider, transport=transport))
