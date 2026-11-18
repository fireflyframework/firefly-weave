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


"""Run logs on a real local Docker platform: a worker's entries reach the run, and a planted secret never does.

Run it with ``pytest -m real_operations`` and WEAVE_ACC_RUN_DIR set to an acceptance run that
``scripts/acceptance.py --stages prepare,up --keep`` left in place, as the other operations journeys. Without it the
test is skipped. Its own workflow runs once on the fixture worker, whose note plants a key and a JWT-shaped bearer
token made for this test: the run's log holds the worker's entries with the values hidden, and neither value is in
the runs.logs or execution_logs.list answers or in any row of the platform's execution_logs table. Studio renders
entries as text, which its browser suites check.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.real_operations

ROOT = Path(__file__).resolve().parents[2]
OWNED_CONTEXT = "colima-weave-tests"
SUFFIX = uuid4().hex[:6]
WORKFLOW = "logs-o-j6-" + SUFFIX
ACTION = "logs-o-j6-sleep-" + SUFFIX


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _journeys() -> tuple[Any, Any]:
    """The J5 helpers, and J5.5's logged run on the fixture worker (which imports the helpers by their name)."""
    overview = _load("test_operations_overview", ROOT / "tests/acceptance/test_operations_overview.py")
    logs = _load("test_j5_logs", ROOT / "tests/acceptance/test_j5_logs.py")
    return overview, logs


@pytest.fixture(scope="module")
def run() -> Any:
    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if not root:
        pytest.skip("Set WEAVE_ACC_RUN_DIR to a kept acceptance run to run the operations journeys")
    path = Path(root).resolve()
    assert (ROOT / "build").resolve() in path.parents, "the run must live under this checkout's build/"
    kept = _load("operations_acceptance_harness", ROOT / "tests/acceptance/conftest.py").Run(path)
    assert kept.context == OWNED_CONTEXT, f"the journeys use only {OWNED_CONTEXT}"
    return kept


def platform_sql(run: Any, sql: str) -> str:
    """The rows of one query on the platform's own database, read inside its PostgreSQL container."""
    container = f"weave-local-{run.platform_state()['id']}-postgres-1"
    databases = run.require(
        run.docker(
            "exec", container, "sh", "-c", 'psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database"'
        )
    ).stdout.split()
    for database in databases:
        found = run.docker("exec", container, "sh", "-c", f'psql -U "$POSTGRES_USER" -d {database} -Atc "{sql}"')
        if found.returncode == 0:
            return str(found.stdout)
    raise AssertionError("No database of the platform answered")


def test_o_j6_a_workers_entries_reach_the_run_and_a_planted_secret_never_does(run):
    overview, logs = _journeys()
    platform = overview.Platform(run)
    _, release = overview.fixture_worker(run)
    values = logs.planted()
    journey = asyncio.run(logs.logged_run(platform, release, logs.note_of(values), name=WORKFLOW, action=ACTION))
    items = journey["everything"]["items"]
    written = [item for item in items if item["source"] == "worker"]
    assert [item["code"] for item in written] == ["FIXTURE.SLEPT", "FIXTURE.NOTE"], written
    note = written[1]
    assert note["redactions"] >= 1 and "[redacted]" in note["message"]
    answers = json.dumps([journey[key] for key in ("everything", "warnings", "step", "across")])
    assert logs.leaked(answers, values) == 0, "a planted secret reached a log answer"
    # The stored rows: the note as the platform keeps it, and no planted value anywhere in the table.
    run_id = str(UUID(journey["run"]))
    stored = platform_sql(
        run, f"SELECT redactions, message FROM execution_logs WHERE run_id = '{run_id}' AND code = 'FIXTURE.NOTE'"
    ).strip()
    redactions, _, message = stored.partition("|")
    assert int(redactions) >= 1 and "[redacted]" in message
    table = platform_sql(run, "SELECT message || ' ' || fields::text FROM execution_logs")
    assert table.strip(), "the execution_logs table holds no row"
    assert logs.leaked(table, values) == 0, "a planted secret reached the execution_logs table"
