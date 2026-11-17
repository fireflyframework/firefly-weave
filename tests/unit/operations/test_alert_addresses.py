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

"""Each person's own alert address: only the caller, only a person with a grant in the tenant."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import MyAlertAddressUpdate
from firefly_weave.operations.alert_channels import AlertAddressService, is_person

TENANT = uuid4()
SCOPE = Scope(tenant_id=TENANT, project_id=uuid4(), environment_id=uuid4())
ELSEWHERE = Scope(tenant_id=uuid4(), project_id=uuid4())


def principal(*roles: str, kind: str = "human", active: bool = True, scope: Scope = SCOPE) -> Principal:
    return Principal(
        id=uuid4(), kind=kind, active=active, grants=tuple(Grant(role=role, scope=scope) for role in roles)
    )


def test_a_person_is_someone_with_a_grant_in_the_tenant_who_isnt_a_machine():
    assert is_person(principal("viewer"), TENANT)
    assert is_person(principal("email_reader", kind="application"), TENANT)
    assert not is_person(principal("viewer", scope=ELSEWHERE), TENANT)
    assert not is_person(principal("viewer", active=False), TENANT)
    assert not is_person(principal("worker", kind="worker"), TENANT)
    assert not is_person(principal("deployment_runner", kind="application"), TENANT)
    assert not is_person(principal("viewer", kind="worker"), TENANT)


def test_the_address_update_names_no_one():
    with pytest.raises(ValidationError):
        MyAlertAddressUpdate.model_validate_json(
            json.dumps({"address": "ana@example.org", "principal_id": str(uuid4())})
        )
    assert MyAlertAddressUpdate.model_validate_json("{}").address is None


class Unopened:
    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        raise AssertionError("a refused call opened a transaction")
        yield


SERVICE = AlertAddressService(Unopened(), AuthorizationService())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "actor",
    [
        principal("viewer", scope=ELSEWHERE),
        principal("worker", kind="worker"),
        principal("deployment_runner", kind="application"),
        principal("viewer", active=False),
    ],
)
async def test_someone_who_isnt_a_person_of_the_tenant_can_neither_read_nor_set_an_address(actor):
    with pytest.raises(AccessDenied):
        await SERVICE.read_mine(actor, TENANT, context=AuditContext())
    with pytest.raises(AccessDenied):
        await SERVICE.save_mine(actor, TENANT, MyAlertAddressUpdate(address="x@example.org"), context=AuditContext())
