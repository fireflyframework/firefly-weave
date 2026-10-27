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


"""Run statistics on a real local Docker platform: 25 runs of the fixture worker, five of them planted failures.

Run it with ``pytest -m real_operations`` and WEAVE_ACC_RUN_DIR set to an acceptance run that
``scripts/acceptance.py --stages prepare,up --keep`` left in place, as the other operations journeys. Without it the
test is skipped. It publishes its own workflow, so it never depends on another journey's runs: the counts are exact,
the failure rate is 0.2, and p95 is within a factor of √2 of the p95 measured from the runs themselves. Studio's
Metrics page is checked against the same platform by the operate-real browser suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.real_operations

ROOT = Path(__file__).resolve().parents[2]
OWNED_CONTEXT = "colima-weave-tests"
SUFFIX = uuid4().hex[:6]
WORKFLOW = "metrics-o-j5-" + SUFFIX
ACTION = "metrics-o-j5-sleep-" + SUFFIX
RUN_TIMEOUT = 10
SUCCEEDING = [{"ms": 100 + 40 * index} for index in range(20)]
FAILING = [{"ms": RUN_TIMEOUT * 3000}] * 5
# complete_until passes a run's end about ten minutes later: the five-minute margin plus the rollup's visits.
WAIT_SECONDS = 900


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _journeys() -> Any:
    """The J5 helpers: the platform's API and token, and order-review-lite's publication."""
    _load("operations_acceptance_harness", ROOT / "tests/acceptance/conftest.py")
    return _load("operations_overview_journeys", ROOT / "tests/acceptance/test_operations_overview.py")


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


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(q * len(ordered)) - 1]


async def measured(run: Any) -> dict:
    journeys = _journeys()
    platform = journeys.Platform(run)
    _, release = journeys.fixture_worker(run)
    activation = await journeys.lite_activation(
        platform, release, name=WORKFLOW, action=ACTION, timeout_seconds=RUN_TIMEOUT
    )
    began = datetime.now(UTC) - timedelta(minutes=1)
    async with platform.client() as client:

        async def start(inputs: list[dict]) -> list[str]:
            started = []
            for item in inputs:
                body = {"activation_id": activation["id"], "input": item}
                started.append((await journeys.post(client, platform.environment + "/runs", body))["id"])
            return started

        async def until_ended(runs: list[str]) -> dict[str, dict]:
            deadline = time.monotonic() + WAIT_SECONDS
            while True:
                listed = await journeys.get(
                    client, platform.environment + "/run-summaries", workflow=WORKFLOW, limit=100
                )
                found = {item["id"]: item for item in listed["items"] if item["id"] in runs}
                if len(found) == len(runs) and all(item.get("ended_at") for item in found.values()):
                    return found
                assert time.monotonic() < deadline, {key: item.get("status") for key, item in found.items()}
                await asyncio.sleep(1)

        # The successful runs first, so none waits for a slot long enough to time out.
        succeeded = await until_ended(await start(SUCCEEDING))
        failed = await until_ended(await start(FAILING))
        last_end = max(datetime.fromisoformat(item["ended_at"]) for item in [*succeeded.values(), *failed.values()])
    window = {"from": began.isoformat().replace("+00:00", "Z")}
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        # The wait can outlive an access token: each read signs in again.
        fresh = journeys.Platform(run)
        async with fresh.client() as reader:
            window["to"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            table = await journeys.statistics(reader, fresh, "workflows", **window)
            complete = table["complete_until"]
            # Every bucket holding these runs is final once complete_until passes the last run's end.
            if complete and datetime.fromisoformat(complete) > last_end:
                series = await journeys.statistics(
                    reader,
                    fresh,
                    "series",
                    **window,
                    workflow=WORKFLOW,
                    signals=["runs_started", "runs_succeeded", "runs_failed", "failure_rate"],
                )
                break
        assert time.monotonic() < deadline, (complete, last_end.isoformat())
        await asyncio.sleep(10)
    row = next(item for item in table["workflows"] if item["workflow"] == WORKFLOW)
    return {
        "row": row,
        "series": series["series"],
        "statuses": sorted(item["status"] for item in [*succeeded.values(), *failed.values()]),
        "durations_ms": sorted(float(item["duration_ms"]) for item in [*succeeded.values(), *failed.values()]),
        "complete_until": table["complete_until"],
    }


def test_o_j5_metrics_count_runs_and_failures_exactly_and_bound_p95(run):
    figures = asyncio.run(measured(run))
    row = figures["row"]
    assert figures["statuses"] == ["succeeded"] * 20 + ["timed_out"] * 5
    assert (row["runs"], row["succeeded"], row["failed"], row["cancelled"]) == (25, 20, 5, 0), row
    assert row["failure_rate"] == pytest.approx(0.2), row
    totals = {item["signal"]: item["points"] for item in figures["series"] if not item["labels"]}
    assert sum(value or 0 for _, value in totals["runs_started"]) == 25
    assert sum(value or 0 for _, value in totals["runs_failed"]) == 5
    truth = nearest_rank(figures["durations_ms"], 0.95)
    assert truth / math.sqrt(2) <= row["duration_p95_ms"] <= truth * math.sqrt(2), (row, truth)
    evidence = run.evidence / "operations-metrics-o-j5.json"
    evidence.write_text(json.dumps({**figures, "measured_p95_ms": truth}, indent=2) + "\n", encoding="utf-8")
