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

"""A message search says nothing about the entries of runs whose details are withheld: no row, and no cursor that
could lead to one."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import ExecutionLogQuery
from firefly_weave.operations.execution_logs import AcrossPosition, ExecutionLogReader

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def withheld(minutes: int) -> dict:
    return {
        "id": uuid4(),
        "at": NOW - timedelta(minutes=minutes),
        "source": "worker",
        "level": "info",
        "code": None,
        "message": "token-prefix-ABC",
        "node_id": "send",
        "instance_key": "",
        "generation": 1,
        "worker_id": uuid4(),
        "fields": {},
        "redactions": 0,
        "truncated": False,
        "run_id": uuid4(),
        "workflow_name": "orders",
        "test": False,
        "workflow_version": "1.0.0",
        "classification_state": "unavailable",
        "classification_policy": None,
        "pinned_ir_version": None,
        "pinned_features": None,
        "policy_blocked": False,
    }


class Rows:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def mappings(self) -> "Rows":
        return self

    def all(self) -> list[dict]:
        return self.rows


def answering(matches: list[dict]) -> SimpleNamespace:
    """A database that returns ``matches`` for the stored stream's search, as if its filter had let them through."""

    async def execute(statement, params=None):
        if "position(lower" in str(statement):
            return Rows(matches[: params["limit"]])
        return Rows([])

    return SimpleNamespace(scope=SCOPE, session=SimpleNamespace(execute=execute))


async def test_a_search_that_reaches_only_withheld_entries_ends_with_no_row_and_no_cursor():
    query = ExecutionLogQuery(contains="token-prefix", limit=1)
    items, after = await ExecutionLogReader(answering([withheld(5), withheld(10)])).page(
        query, AcrossPosition(NOW - timedelta(hours=1), NOW)
    )
    assert (items, after) == ([], None)
