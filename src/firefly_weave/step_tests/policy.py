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
"""Whether an environment allows test calls: off unless an administrator turns it on, revisioned and audited."""

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.step_tests import EnvironmentTestPolicy, EnvironmentTestPolicyUpdate
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction, UnitOfWork

SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


def _keys(scope: Scope) -> dict[str, object]:
    if scope.environment_id is None:
        raise AccessDenied()
    return {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id}


def _changed() -> CatalogError:
    return CatalogError(409, "WV-REVISION", "The test policy changed; read it again before saving")


async def _stored(tx: Transaction, *, lock: str = "") -> EnvironmentTestPolicy:
    row = (
        await tx.session.execute(
            text(f"SELECT test_calls,revision FROM environment_test_policies WHERE {SCOPED}{lock}"), _keys(tx.scope)
        )
    ).first()
    if row is None:
        return EnvironmentTestPolicy(test_calls="disabled", revision=0)
    return EnvironmentTestPolicy(test_calls=row[0], revision=row[1])


@service
class TestPolicyService:
    __test__ = False

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def read(self, actor: Principal, scope: Scope, *, context: AuditContext) -> EnvironmentTestPolicy:
        _keys(scope)
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            return await _stored(tx)

    async def save(
        self,
        actor: Principal,
        scope: Scope,
        update: EnvironmentTestPolicyUpdate,
        expected_revision: int,
        *,
        context: AuditContext,
    ) -> EnvironmentTestPolicy:
        keys = _keys(scope)
        self.authorization.require(actor, scope, "environment.manage", context=context)
        async with self.uow.open(scope) as tx:
            before = await _stored(tx, lock=" FOR UPDATE")
            if before.revision != expected_revision:
                raise _changed()
            values = {**keys, "calls": update.test_calls, "actor": actor.id, "expected": expected_revision}
            if expected_revision == 0:
                statement = (
                    "INSERT INTO environment_test_policies(tenant_id,project_id,environment_id,test_calls,revision,"
                    "updated_at,updated_by) VALUES(:tenant,:project,:environment,:calls,1,clock_timestamp(),:actor) "
                    "ON CONFLICT DO NOTHING RETURNING revision"
                )
            else:
                statement = (
                    "UPDATE environment_test_policies SET test_calls=:calls,revision=revision+1,"
                    f"updated_at=clock_timestamp(),updated_by=:actor WHERE {SCOPED} AND revision=:expected "
                    "RETURNING revision"
                )
            saved = (await tx.session.execute(text(statement), values)).first()
            if saved is None:
                # Another administrator saved first, at the same revision.
                raise _changed()
            await audit(
                tx.session,
                actor,
                "test_policy.save",
                str(scope.environment_id),
                scope=scope,
                capability="environment.manage",
                context=context,
                details={"old": before.test_calls, "new": update.test_calls, "revision": saved[0]},
            )
            return EnvironmentTestPolicy(test_calls=update.test_calls, revision=saved[0])

    async def enabled(self, tx: Transaction) -> bool:
        """Whether test calls are on, read inside the caller's transaction; a concurrent change waits for it."""
        return (await _stored(tx, lock=" FOR SHARE")).test_calls == "enabled"
