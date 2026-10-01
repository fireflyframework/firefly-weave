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

"""Authorized bounded maintenance; PostgreSQL owns immutable plans and atomic manifests."""

import json
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.maintenance import RetentionApplication, RetentionPlan, RetentionRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import UnitOfWork


@service
class RetentionService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    @staticmethod
    def project(scope: Scope) -> Scope:
        if scope.project_id is None or scope.environment_id is not None:
            raise CatalogError(422, "WV-SCOPE", "Project maintenance scope required")
        return scope

    async def plan(
        self, actor: Principal, scope: Scope, request: RetentionRequest, *, context: AuditContext
    ) -> RetentionPlan:
        self.project(scope)
        async with self.uow.open(scope) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "retention.plan", context=context)
            result = await tx.session.scalar(
                text("SELECT weave_retention_plan(:actor,:limit,:after)"),
                {"actor": current.id, "limit": request.limit, "after": request.after},
            )
            plan = RetentionPlan.model_validate_json(json.dumps(result))
            await audit(
                tx.session,
                current,
                "retention.plan",
                str(plan.id),
                scope=scope,
                capability="retention.plan",
                context=context,
                details={"candidates": len(plan.candidates)},
            )
            tx.record("retention", "retention", "ok")
            return plan

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> RetentionPlan:
        self.project(scope)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "retention.plan", context=context)
            row = (
                (
                    await tx.session.execute(
                        text(
                            "SELECT payload FROM retention_plans WHERE tenant_id=:t AND project_id=:p "
                            "AND id=:id AND principal_id=:actor"
                        ),
                        {"t": scope.tenant_id, "p": scope.project_id, "id": identifier, "actor": current.id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Retention plan not found")
            return RetentionPlan.model_validate_json(json.dumps(row["payload"]))

    async def apply(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext
    ) -> RetentionApplication:
        self.project(scope)
        async with self.uow.open(scope) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "retention.apply", context=context)
            prior = await tx.session.scalar(
                text(
                    "SELECT manifest FROM retention_applications WHERE tenant_id=:t AND project_id=:p AND "
                    "plan_id=:id AND principal_id=:actor"
                ),
                {"t": scope.tenant_id, "p": scope.project_id, "id": identifier, "actor": current.id},
            )
            if prior is not None:
                return RetentionApplication.model_validate_json(json.dumps(prior))
            result = await tx.session.scalar(
                text("SELECT weave_retention_apply(:id,:actor)"), {"id": identifier, "actor": current.id}
            )
            applied = RetentionApplication.model_validate_json(json.dumps(result))
            await audit(
                tx.session,
                current,
                "retention.apply",
                str(identifier),
                scope=scope,
                capability="retention.apply",
                context=context,
                details={"deleted": len(applied.deleted), "blocked": len(applied.blocked)},
            )
            tx.record("retention", "retention", "ok")
            return applied
