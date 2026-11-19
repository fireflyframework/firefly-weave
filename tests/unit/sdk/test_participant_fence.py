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


"""The fence's table name and table text; its behavior is tested on PostgreSQL."""

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from firefly_weave.contracts.transactions import PhaseOperation
from firefly_weave.sdk.participants import FENCE_STATES, FENCE_TABLE, Fence


def test_the_fence_table_is_schema_and_table():
    assert Fence("app.fence").table == "app.fence"
    assert Fence().table == FENCE_TABLE == "weave_fence.operations"
    for name in (
        "fence",
        "a.b.c",
        'app."x"',
        "app.x; drop",
        "App.x",
        "app.",
        ".x",
        "1app.x",
        "app.x-y",
        "a" * 64 + ".x",
    ):
        with pytest.raises(ValueError):
            Fence(name)


def test_the_fence_creates_the_table_its_states_need():
    ddl = Fence("app.fence").ddl()
    assert 'CREATE SCHEMA IF NOT EXISTS "app";' in ddl
    assert 'CREATE TABLE IF NOT EXISTS "app"."fence" (' in ddl
    for column in ("operation_key text PRIMARY KEY", "branch_key text NOT NULL", "phase text NOT NULL"):
        assert column in ddl
    assert "status text NOT NULL" in ddl and "result jsonb" in ddl
    assert set(FENCE_STATES) == {"tried", "confirmed", "cancelled", "tombstone"}
    phases: tuple[PhaseOperation, ...] = ("try", "forward", "confirm", "cancel", "compensate")
    for value in (*FENCE_STATES, *phases):
        assert f"'{value}'" in ddl


def test_importing_the_fence_starts_no_driver():
    probe = "import sys, firefly_weave.sdk.participants; print(sorted({'asyncpg', 'psycopg'} & set(sys.modules)))"
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True, timeout=60)
    assert done.stdout.strip() == "[]"


GUIDE = Path(__file__).resolve().parents[3] / "docs/guides/transaction-participants.md"
EXAMPLE = Path(__file__).resolve().parents[3] / "examples/transaction_participant.py"


def code_blocks(text: str, language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"^ *```{language}\n(.*?)^ *```$", text, re.M | re.S)]


def test_the_guide_shows_the_table_the_fence_creates():
    assert code_blocks(GUIDE.read_text(encoding="utf-8"), "sql") == [Fence().ddl()]


def test_the_guide_quotes_the_tested_example():
    example = EXAMPLE.read_text(encoding="utf-8")
    quoted = code_blocks(GUIDE.read_text(encoding="utf-8"), "python")
    assert len(quoted) == 3
    for block in quoted:
        assert block in example
