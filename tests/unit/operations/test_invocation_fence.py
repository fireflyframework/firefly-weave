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

"""The domain invocation port rejects read-only outer transactions before row locks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.leases import TaskService


async def test_invocation_requires_admitted_outer_mutation_before_authority_check():
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    session = SimpleNamespace(
        get_transaction=lambda: SimpleNamespace(is_active=True),
        scalar=AsyncMock(return_value=str(scope.tenant_id)),
        info={},
    )
    tasks = object.__new__(TaskService)
    tasks.workers = SimpleNamespace(definitions=object.__new__(DefinitionService))
    tasks._operation = Mock(side_effect=AssertionError("check reached without outer admission"))
    with pytest.raises(CatalogError) as error:
        await tasks.invocation(
            Transaction(session=session, scope=scope), None, actor=None, scope=scope, context=AuditContext()
        )
    assert error.value.code == "WV-TRANSACTION"
    tasks._operation.assert_not_called()
