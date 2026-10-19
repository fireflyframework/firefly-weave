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

"""Deployment targets a reader may see, from both the token and the principal stored now."""

from uuid import UUID

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.access import Scope


def target_permissions(
    authorization: AuthorizationService,
    actor: Principal,
    current: Principal,
    scope: Scope,
    capability: str,
    context: AuditContext,
) -> tuple[UUID, ...] | None:
    """`None` when both principals hold `capability` for every target; otherwise the sorted finite intersection.

    The result is empty when either principal holds it for no target. Each granted target is checked through the
    authorization service, so callers never collect every target and filter afterwards.
    """

    def allowed(principal: Principal) -> set[UUID] | None:
        if not principal.active or principal.kind == "worker":
            return set()
        candidates: set[UUID] = set()
        for grant in principal.grants:
            if capability not in ROLE_CAPABILITIES[grant.role] or not contains(grant.scope, scope):
                continue
            if not grant.resources:
                authorization.require(principal, scope, capability, context=context)
                return None
            for resource in grant.resources:
                try:
                    identifier = UUID(resource)
                except ValueError:
                    continue
                # Target grants name the canonical ID, as single target reads check it.
                if str(identifier) != resource:
                    continue
                authorization.require(principal, scope, capability, resource=str(identifier), context=context)
                candidates.add(identifier)
        return candidates

    left, right = allowed(actor), allowed(current)
    result = right if left is None else left if right is None else left & right
    return None if result is None else tuple(sorted(result))
