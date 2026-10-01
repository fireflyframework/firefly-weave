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

"""Service-level capability checks and explicit administrative delegation ceilings."""

from pyfly.container import service

from firefly_weave.access.audit import AuditContext, emit, event
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope


class AccessDenied(Exception):
    def __init__(self) -> None:
        super().__init__("Access denied")


def contains(parent: Scope | None, child: Scope | None) -> bool:
    if parent is None or child is None:
        return parent is child
    return parent.tenant_id == child.tenant_id and all(
        getattr(parent, key) is None or getattr(parent, key) == getattr(child, key)
        for key in ("project_id", "environment_id")
    )


@service
class AuthorizationService:
    def require(
        self,
        principal: Principal,
        scope: Scope | None,
        capability: str,
        *,
        resource: str | None = None,
        context: AuditContext | None = None,
    ) -> None:
        allowed = principal.active and any(
            capability in ROLE_CAPABILITIES[grant.role]
            and contains(grant.scope, scope)
            and (not grant.resources or resource in grant.resources)
            for grant in principal.grants
        )
        if principal.kind == "worker" and capability not in ROLE_CAPABILITIES["worker"]:
            allowed = False
        emit(event(principal, scope, capability, "allow" if allowed else "deny", context or AuditContext()))
        if not allowed:
            raise AccessDenied()

    def require_delegation(self, principal: Principal, grant: Grant, *, context: AuditContext | None = None) -> None:
        platform = any(g.role == "platform_admin" and not g.resources for g in principal.grants)
        administered = grant.scope is not None and any(
            g.role == "tenant_admin"
            and contains(g.scope, grant.scope)
            and (not g.resources or (grant.resources and set(grant.resources) <= set(g.resources)))
            for g in principal.grants
        )
        allowed = principal.active and principal.kind != "worker" and (platform or administered)
        emit(
            event(
                principal,
                grant.scope,
                "grant.admin" if platform else "grant.manage",
                "allow" if allowed else "deny",
                context or AuditContext(),
                action="delegation",
                details={"grant": grant.model_dump(mode="json")},
            )
        )
        if not allowed:
            raise AccessDenied()


def authorize(
    principal: Principal, scope: Scope | None, capability: str, *, context: AuditContext | None = None
) -> None:
    AuthorizationService().require(principal, scope, capability, context=context)
