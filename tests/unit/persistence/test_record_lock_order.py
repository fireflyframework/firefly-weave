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

"""A record's lock is taken only before the project's operation lock, and never silently skipped."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.uow import UnitOfWork


def no_session():
    raise AssertionError("a refused record lock opens no session")


@pytest.mark.parametrize(
    ("project", "mutation", "admission"),
    [(True, False, True), (True, True, False), (False, True, True)],
    ids=["read", "writer-without-admission", "tenant-change"],
)
async def test_a_record_lock_is_refused_outside_an_admitted_project_change(project, mutation, admission):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4() if project else None)
    with pytest.raises(ValueError, match="record's lock"):
        async with UnitOfWork(no_session).open(scope, mutation=mutation, admission=admission, resource="incident:a"):
            pass


async def test_an_enlisted_transaction_cannot_take_a_record_lock_after_the_project_lock():
    service = DefinitionService.__new__(DefinitionService)
    with pytest.raises(ValueError, match="record's lock"):
        async with service.transaction(Scope(tenant_id=uuid4(), project_id=uuid4()), object(), resource="incident:a"):
            pass


async def test_only_a_change_that_names_a_record_hands_it_to_the_unit_of_work():
    """Every other caller opens its unit of work as before record locks existed, so one without them still works."""
    opened = []

    class RecordingUnitOfWork:
        @asynccontextmanager
        async def open(self, scope, *, mutation=True, **record):
            opened.append((mutation, record))
            yield "owned"

    service = DefinitionService.__new__(DefinitionService)
    service.uow = RecordingUnitOfWork()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    async with service.transaction(scope, None, mutation=False) as plain:
        pass
    async with service.transaction(scope, None, resource="incident:a") as decided:
        pass
    assert (plain, decided) == ("owned", "owned")
    assert opened == [(False, {}), (True, {"resource": "incident:a"})]
