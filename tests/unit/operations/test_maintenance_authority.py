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

"""Read permissions and creator selection use current scoped authority."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.compatibility import CompatibilityFinding, CompatibilityReport
from firefly_weave.contracts.maintenance import RetentionPlan
from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.compatibility import CompatibilityService
from firefly_weave.operations.retention import RetentionService


class ReadUnit:
    def __init__(self, scope, session):
        self.scope, self.session = scope, session

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert scope == self.scope and mutation is False
        yield SimpleNamespace(session=self.session)


@pytest.mark.parametrize(
    "role,refresh,allowed", [("viewer", False, True), ("viewer", True, False), ("operator", True, True)]
)
async def test_compatibility_read_does_not_require_rescan_authority(monkeypatch, role, refresh, allowed):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    actor = Principal(id=uuid4(), kind="human", grants=(Grant(role=role, scope=scope),))
    service = CompatibilityService(None, ConnectorRegistry(), ReadUnit(scope, object()), AuthorizationService(), None)
    foreign = scope.model_copy(update={"project_id": uuid4()})
    service.report = CompatibilityReport(
        findings=[CompatibilityFinding(kind="run", code="ir_unsupported", scope=foreign, resource_id=uuid4())]
    )
    scans = []

    async def load(session, identifier):
        assert identifier == actor.id
        return actor

    async def scan():
        scans.append(True)

    monkeypatch.setattr("firefly_weave.operations.compatibility.load_principal", load)
    monkeypatch.setattr(service, "scan", scan)
    if allowed:
        report = await service.scoped(actor, scope, context=AuditContext(), refresh=refresh)
        assert report.findings == [] and report.inspected == 0
    else:
        with pytest.raises(AccessDenied):
            await service.scoped(actor, scope, context=AuditContext(), refresh=refresh)
    assert len(scans) == int(allowed and refresh)


@pytest.mark.parametrize("change", ["revoked", "foreign", "environment", "resource"])
async def test_compatibility_read_uses_current_project_authority(monkeypatch, change):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    actor = Principal(id=uuid4(), kind="human", grants=(Grant(role="viewer", scope=scope),))
    changed_scope = (
        scope.model_copy(update={"project_id": uuid4()})
        if change == "foreign"
        else scope.model_copy(update={"environment_id": uuid4()})
        if change == "environment"
        else scope
    )
    current = actor.model_copy(
        update={
            "active": change != "revoked",
            "grants": (Grant(role="viewer", scope=changed_scope, resources=("one",) if change == "resource" else ()),),
        }
    )

    async def load(session, identifier):
        return current

    monkeypatch.setattr("firefly_weave.operations.compatibility.load_principal", load)
    service = CompatibilityService(None, ConnectorRegistry(), ReadUnit(scope, object()), AuthorizationService(), None)
    with pytest.raises(AccessDenied):
        await service.scoped(actor, scope, context=AuditContext())


def test_compatibility_surface_and_native_openapi_publish_read_check_split():
    assert OPERATIONS["compatibility.read"].capability == "status.read"
    assert OPERATIONS["compatibility.check"].capability == "compatibility.check"
    paths = export_openapi()["paths"]
    assert "status.read" in json.dumps(paths[OPERATIONS["compatibility.read"].canonical_path]["get"])
    assert "compatibility.check" in json.dumps(paths[OPERATIONS["compatibility.check"].canonical_path]["post"])


@pytest.mark.parametrize("selection", ["own", "other_creator", "foreign_scope", "revoked", "environment", "resource"])
async def test_retention_read_filters_creator_and_current_project_authority(monkeypatch, selection):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    actor = Principal(id=uuid4(), kind="human", grants=(Grant(role="operator", scope=scope),))
    current = actor
    if selection == "revoked":
        current = actor.model_copy(update={"active": False})
    elif selection in {"environment", "resource"}:
        grant = Grant(
            role="operator",
            scope=scope.model_copy(update={"environment_id": uuid4()}) if selection == "environment" else scope,
            resources=("one",) if selection == "resource" else (),
        )
        current = actor.model_copy(update={"grants": (grant,)})
    selected_scope = scope.model_copy(update={"project_id": uuid4()}) if selection == "foreign_scope" else scope
    creator = uuid4() if selection == "other_creator" else actor.id
    now = datetime.now(UTC)
    plan = RetentionPlan(
        id=uuid4(),
        scope=selected_scope,
        principal_id=creator,
        created_at=now,
        cutoff=now - timedelta(days=1),
        expires_at=now + timedelta(minutes=15),
        complete=True,
        candidates=[],
    )
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE retention_plans (tenant_id TEXT, project_id TEXT, id TEXT, principal_id TEXT, payload TEXT)"
    )
    db.execute(
        "INSERT INTO retention_plans VALUES (?,?,?,?,?)",
        (
            str(selected_scope.tenant_id),
            str(selected_scope.project_id),
            str(plan.id),
            str(creator),
            plan.model_dump_json(),
        ),
    )
    queries = []

    class Session:
        async def execute(self, statement, parameters):
            queries.append(True)
            row = db.execute(str(statement), {k: str(v) for k, v in parameters.items()}).fetchone()
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(first=lambda: {"payload": json.loads(row[0])} if row else None)
            )

    async def load(session, identifier):
        assert identifier == actor.id
        return current

    monkeypatch.setattr("firefly_weave.operations.retention.load_principal", load)
    service = RetentionService(ReadUnit(scope, Session()), AuthorizationService())
    try:
        if selection == "own":
            assert await service.read(actor, scope, plan.id, context=AuditContext()) == plan
        elif selection in {"other_creator", "foreign_scope"}:
            with pytest.raises(CatalogError) as error:
                await service.read(actor, scope, plan.id, context=AuditContext())
            assert error.value.status == 404
        else:
            with pytest.raises(AccessDenied):
                await service.read(actor, scope, plan.id, context=AuditContext())
            assert not queries
    finally:
        db.close()
