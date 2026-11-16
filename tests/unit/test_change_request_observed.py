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

"""A change request read with the time its state was computed at."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ChangeRequest
from firefly_weave.deployments import change_service
from firefly_weave.deployments.change_service import ChangeRequestService

SCOPE = Scope(
    tenant_id=UUID("00000000-0000-4000-8000-0000000000f1"),
    project_id=UUID("00000000-0000-4000-8000-0000000000f2"),
    environment_id=UUID("00000000-0000-4000-8000-0000000000f3"),
)
CHANGE_ID = UUID("00000000-0000-4000-8000-0000000000f4")
TARGET = UUID("00000000-0000-4000-8000-0000000000f5")
ACTOR = Principal(id=UUID("00000000-0000-4000-8000-0000000000f6"), kind="human")
CREATED = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

RECORD = ChangeRequest.model_validate_json(
    json.dumps(
        {
            "id": str(CHANGE_ID),
            "scope": SCOPE.model_dump(mode="json"),
            "target_id": str(TARGET),
            "deployment_id": str(UUID("00000000-0000-4000-8000-0000000000f7")),
            "expected_deployment_revision": 1,
            "intent": "scale_workers",
            "components": ["fixture-worker"],
            "title": "Scale the workers",
            "revision": 1,
            "requested_by": str(ACTOR.id),
            "created_at": CREATED.isoformat(),
            "deployment_revision": 1,
            "approval_policy": {"distinct_approver": True},
            "state": "awaiting_approval",
            "plan_expires_at": (CREATED + timedelta(seconds=600)).isoformat(),
        }
    )
)


class Units:
    def __init__(self):
        self.opened = 0

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert mutation is False
        self.opened += 1
        yield SimpleNamespace(scope=scope, session=None)


def reader(monkeypatch, now, *, refuse=False):
    class Repository:
        def __init__(self, tx):
            self.tx = tx

        async def get(self, collection, identifier, model):
            assert (collection, identifier, model) == ("change_requests", CHANGE_ID, ChangeRequest)
            return RECORD

        async def now(self):
            return now

    authorized = []

    async def authorize(tx, actor, capability, context, target):
        authorized.append((actor, capability, target))
        if refuse:
            raise AccessDenied()

    async def able(tx, actor, capability):
        assert capability == "deployment.read"

    monkeypatch.setattr(change_service, "DeploymentRepository", Repository)
    units = Units()
    service = ChangeRequestService(units, SimpleNamespace(authorize=authorize), None)  # type: ignore[arg-type]
    service.able = able  # type: ignore[method-assign]
    return service, units, authorized


async def test_read_observed_returns_the_time_it_computed_the_record_with(monkeypatch):
    expired_at = CREATED + timedelta(seconds=601)
    service, units, authorized = reader(monkeypatch, expired_at)
    record, observed_at = await service.read_observed(CHANGE_ID, actor=ACTOR, scope=SCOPE, context=AuditContext())
    assert observed_at == expired_at
    assert record.state == "expired"
    assert units.opened == 1
    assert authorized == [(ACTOR, "deployment.read", TARGET)]


async def test_read_and_read_observed_return_the_same_record(monkeypatch):
    service, _, _ = reader(monkeypatch, CREATED + timedelta(seconds=60))
    record, _ = await service.read_observed(CHANGE_ID, actor=ACTOR, scope=SCOPE, context=AuditContext())
    assert await service.read(CHANGE_ID, actor=ACTOR, scope=SCOPE, context=AuditContext()) == record
    assert record.state == "awaiting_approval"


async def test_read_observed_refuses_a_caller_without_the_targets_read(monkeypatch):
    service, _, _ = reader(monkeypatch, CREATED, refuse=True)
    with pytest.raises(AccessDenied):
        await service.read_observed(CHANGE_ID, actor=ACTOR, scope=SCOPE, context=AuditContext())
