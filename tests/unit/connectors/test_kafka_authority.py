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

"""Authority snapshots must not request PostgreSQL row locks."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.triggers.kafka import ConsumerAuthority, KafkaTrigger


class QueryBoundary(Exception):
    """Stop at the real authority query before unrelated fixture lookups."""


@pytest.mark.parametrize("entry", ["authorize", "credentials", "checked"])
async def test_authority_reads_do_not_request_row_locks(monkeypatch, entry):
    authority = ConsumerAuthority(
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), uuid4(), uuid4(), 1
    )
    queries = []

    class Definitions:
        @asynccontextmanager
        async def transaction(self, scope, supplied, *, mutation=True):
            assert mutation is False
            yield SimpleNamespace(scope=scope)

    class Repository:
        def __init__(self, tx):
            self.tx = tx

        async def rows(self, sql, **params):
            queries.append((sql, params))
            raise QueryBoundary

    monkeypatch.setattr("firefly_weave.triggers.kafka.RuntimeRepository", Repository)
    service = KafkaTrigger(SimpleNamespace(definitions=Definitions()), None, None)
    with pytest.raises(QueryBoundary):
        if entry == "checked":
            await service.checked(SimpleNamespace(scope=authority.scope), authority)
        else:
            await getattr(service, entry)(authority)
    assert len(queries) == 1
    sql, params = queries[0]
    assert "FOR UPDATE" not in sql and "FOR SHARE" not in sql
    assert params["id"] == authority.trigger_id


async def test_mutating_authority_check_retains_explicit_row_lock(monkeypatch):
    authority = ConsumerAuthority(
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), uuid4(), uuid4(), 1
    )
    queries = []

    class Repository:
        def __init__(self, tx):
            pass

        async def rows(self, sql, **params):
            queries.append(sql)
            raise QueryBoundary

    monkeypatch.setattr("firefly_weave.triggers.kafka.RuntimeRepository", Repository)
    service = KafkaTrigger(None, None, None)
    with pytest.raises(QueryBoundary):
        await service.checked(SimpleNamespace(scope=authority.scope), authority, for_update=True)
    assert len(queries) == 1 and "FOR UPDATE" in queries[0]
