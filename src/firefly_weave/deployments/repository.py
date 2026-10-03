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

"""Exact-scope persistence with explicit bounded collections."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction

COLLECTIONS = frozenset({"targets", "deployments", "observations", "plans", "approvals", "runners", "jobs", "reports"})
SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


class DeploymentRepository:
    def __init__(self, tx: Transaction):
        self.tx = tx
        self.bound = dict(tenant=tx.scope.tenant_id, project=tx.scope.project_id, environment=tx.scope.environment_id)

    def table(self, collection: str) -> str:
        if collection not in COLLECTIONS:
            raise ValueError("Unknown deployment collection")
        return "deployment_" + collection

    async def now(self) -> datetime:
        value = await self.tx.session.scalar(text("SELECT clock_timestamp()"))
        assert isinstance(value, datetime)
        return value

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in (await self.tx.session.execute(text(sql), {**self.bound, **values})).mappings()]

    async def row(self, collection: str, identifier: UUID, *, lock: bool = False) -> dict[str, Any]:
        rows = await self.rows(
            f"SELECT * FROM {self.table(collection)} WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Deployment resource not found")
        return rows[0]

    async def get[T: ContractModel](
        self, collection: str, identifier: UUID, model: type[T], *, lock: bool = False
    ) -> T:
        return model.model_validate_json(json.dumps((await self.row(collection, identifier, lock=lock))["payload"]))

    async def insert(
        self,
        collection: str,
        identifier: UUID,
        target_id: UUID,
        payload: ContractModel,
        *,
        deployment_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        table = self.table(collection)
        count = await self.tx.session.scalar(text(f"SELECT count(*) FROM {table} WHERE {SCOPE}"), self.bound)
        maximum = (
            100 if collection in {"targets", "deployments", "runners"} else 10000 if collection == "reports" else 1000
        )
        if count is not None and count >= maximum:
            raise CatalogError(409, "WV-DEPLOYMENT-CAPACITY", "Deployment collection limit reached")
        values = extra or {}
        allowed = {"state", "job_id", "runner_id", "request_hash"}
        if not set(values) <= allowed:
            raise ValueError("Invalid deployment persistence fields")
        encoded = payload.model_dump_json()
        if len(encoded.encode()) > 262144:
            raise CatalogError(422, "WV-DEPLOYMENT-SIZE", "Deployment document exceeds its byte budget")
        cols = "".join("," + key for key in values)
        params = "".join(",:" + key for key in values)
        await self.tx.session.execute(
            text(
                f"INSERT INTO {table}(id,tenant_id,project_id,environment_id,target_id,deployment_id,payload{cols}) "
                f"VALUES(:id,:tenant,:project,:environment,:target,:deployment,cast(:payload AS jsonb){params})"
            ),
            {
                **self.bound,
                "id": identifier,
                "target": target_id,
                "deployment": deployment_id,
                "payload": encoded,
                **values,
            },
        )

    async def update(
        self, collection: str, identifier: UUID, payload: ContractModel, *, extra: dict[str, Any] | None = None
    ) -> None:
        values = extra or {}
        allowed = {"state", "generation", "runner_id", "lease_token", "lease_expires_at"}
        if not set(values) <= allowed:
            raise ValueError("Invalid deployment persistence fields")
        sets = "".join("," + key + "=:" + key for key in values)
        encoded = payload.model_dump_json()
        if len(encoded.encode()) > 262144:
            raise CatalogError(422, "WV-DEPLOYMENT-SIZE", "Deployment document exceeds its byte budget")
        await self.tx.session.execute(
            text(
                f"UPDATE {self.table(collection)} SET payload=cast(:payload AS jsonb),revision=revision+1{sets} "
                f"WHERE {SCOPE} AND id=:id"
            ),
            {**self.bound, "id": identifier, "payload": encoded, **values},
        )
