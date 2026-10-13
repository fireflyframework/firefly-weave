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

"""The integration fixtures' teardown drops everything a test created, even when one drop fails."""


class Connection:
    def __init__(self, failing: set[str]):
        self.failing = failing
        self.statements: list[str] = []

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if any(name in sql for name in self.failing):
            raise RuntimeError("dependent objects still exist")


async def test_every_drop_is_attempted_and_every_failure_reported(resource_dropper):
    owned = [("role", "role_a"), ("database", "db_a"), ("database", "db_b"), ("role", "role_b")]
    connection = Connection({"db_a", "role_a"})

    failures = await resource_dropper(connection, owned)

    assert connection.statements == [
        'DROP DATABASE IF EXISTS "db_a" WITH (FORCE)',
        'DROP DATABASE IF EXISTS "db_b" WITH (FORCE)',
        'DROP ROLE IF EXISTS "role_a"',
        'DROP ROLE IF EXISTS "role_b"',
    ]
    assert failures == ["database db_a: RuntimeError", "role role_a: RuntimeError"]


async def test_no_failure_reports_nothing(resource_dropper):
    connection = Connection(set())

    assert await resource_dropper(connection, [("database", "db_a"), ("role", "role_a")]) == []
    assert len(connection.statements) == 2
