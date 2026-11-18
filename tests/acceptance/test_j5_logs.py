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

"""J5.5: run logs on the real platform, through its public API.

Runs only through scripts/acceptance.py (the run stage), on the fixture worker J0.10 started on local-docker. One run
of this step's own one-step workflow makes the fixture worker write two entries: how long it slept, and a warning
with a note that carries secret-shaped text. The run's log then holds the engine's and the worker's step-scoped
entries in time order, within the run's own times; the level and step filters narrow it; the log across runs lists
the worker's entries newest first; and no answer holds the planted text, a canary value or a password.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from test_operations_overview import Platform, fixture_worker, get, lite_activation, post

pytestmark = pytest.mark.acceptance

SUFFIX = uuid4().hex[:6]
WORKFLOW = "order-review-logs-" + SUFFIX
ACTION = "order-review-logs-sleep-" + SUFFIX
SLEEP_MS = 200
# The run's own times bound its entries; a second covers the clocks of the worker and the database.
SLACK = timedelta(seconds=1)
WAIT_SECONDS = 120


def planted() -> list[str]:
    """Secret-shaped values made for this run only: a key and a JWT-shaped bearer token (never in the source)."""

    def part(data: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    token = ".".join([part({"alg": "HS256", "typ": "JWT"}), part({"sub": "j5-" + SUFFIX}), secrets.token_urlsafe(24)])
    return ["sk-" + secrets.token_urlsafe(24), token]


def note_of(values: list[str]) -> str:
    key, token = values
    return f"Lookup key {key} was refused; retrying with Authorization: Bearer {token}"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def logged_run(
    platform: Platform, release: str, note: str, *, name: str = WORKFLOW, action: str = ACTION
) -> dict:
    """One run whose step the fixture worker logs, with its summary and every log read the checks make.

    The fixture worker must be online: J5.3 stops its container, so this step runs before it (its file sorts first).
    """
    activation = await lite_activation(platform, release, name=name, action=action)
    async with platform.client() as client:
        workers = await get(client, platform.environment + "/workers", release_id=release)
        online = [item for item in workers["items"] if item["presence"] == "recent" and not item["draining"]]
        assert online, "no fixture worker is online to run the logged step (J5.3 stops its container)"
        body = {"activation_id": activation["id"], "input": {"ms": SLEEP_MS, "note": note}}
        started = await post(client, platform.environment + "/runs", body)
        run_id = started["id"]
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            listed = await get(client, platform.environment + "/run-summaries", workflow=name, limit=100)
            summary = next((item for item in listed["items"] if item["id"] == run_id), None)
            if summary and summary.get("ended_at"):
                break
            assert time.monotonic() < deadline, summary
            await asyncio.sleep(1)
        assert summary["status"] == "succeeded", summary
        path = platform.environment + f"/runs/{run_id}/logs"
        # The worker sends its entries before it completes the task; the wait only covers a slow database.
        while True:
            everything = await get(client, path)
            if {"FIXTURE.SLEPT", "FIXTURE.NOTE"} <= {item["code"] for item in everything["items"]}:
                break
            assert time.monotonic() < deadline, [item["code"] for item in everything["items"]]
            await asyncio.sleep(1)
        warnings = await get(client, path, level="warning")
        step = await get(client, path, node_id="sleep")
        since = (instant(summary["started_at"]) - timedelta(minutes=1)).isoformat()
        across = await get(
            client, platform.environment + "/execution-logs", workflow=name, level="info", **{"from": since}
        )
    return {
        "run": run_id,
        "summary": summary,
        "everything": everything,
        "warnings": warnings,
        "step": step,
        "across": across,
    }


def run_secrets(run: Any) -> list[str]:
    """The run's canary values and passwords, from the harness module that made ``run``, whatever its name."""
    return list(importlib.import_module(type(run).__module__).known_secrets(run.root))


def leaked(text: str, values: list[str]) -> int:
    """How many of the values appear in the text; never the values themselves, so a failure prints none."""
    return sum(1 for value in values if value in text)


@pytest.mark.journey_step("J5.5")
def test_j5_05_a_run_logs_its_steps_with_times_and_filters_and_no_secret(run):
    platform = Platform(run)
    worker, release = fixture_worker(run)
    values = planted()
    journey = asyncio.run(logged_run(platform, release, note_of(values)))
    summary, items = journey["summary"], journey["everything"]["items"]
    # Every entry is the run's, in time order, within the run's own times.
    times = [instant(item["at"]) for item in items]
    assert times == sorted(times)
    begun, ended = instant(summary["started_at"]) - SLACK, instant(summary["ended_at"]) + SLACK
    assert all(begun <= at <= ended for at in times), (summary["started_at"], summary["ended_at"])
    codes = [item["code"] for item in items]
    assert {"RUN.STARTED", "STEP.SUCCEEDED", "FIXTURE.SLEPT", "FIXTURE.NOTE"} <= set(codes), codes
    # The worker's entries name its step, attempt and worker, which the platform took from the lease.
    written = [item for item in items if item["source"] == "worker"]
    assert [item["code"] for item in written] == ["FIXTURE.SLEPT", "FIXTURE.NOTE"], written
    assert all(item["node_id"] == "sleep" and item["attempt"] == 1 for item in written)
    assert all(item["worker_id"] == worker for item in written)
    slept, note = written
    assert (slept["level"], slept["message"], slept["fields"]) == ("info", f"Slept for {SLEEP_MS} ms", {"ms": SLEEP_MS})
    assert note["level"] == "warning" and note["redactions"] >= 1 and "[redacted]" in note["message"]
    # The step's engine entry names it too.
    finished = next(item for item in items if item["code"] == "STEP.SUCCEEDED")
    assert finished["source"] == "engine" and finished["node_id"] == "sleep", finished
    # Warning or higher keeps the note; the step filter keeps the step's entries only.
    expected = [item["id"] for item in items if item["level"] in ("warning", "error")]
    assert [item["id"] for item in journey["warnings"]["items"]] == expected and expected, expected
    stepped = journey["step"]["items"]
    assert stepped and all(item["node_id"] == "sleep" for item in stepped)
    assert {item["id"] for item in written} <= {item["id"] for item in stepped}
    # Across runs: the worker's entries of this run, newest first, with the run they belong to.
    own = [item for item in journey["across"]["items"] if item.get("run_id") == journey["run"]]
    assert [item["id"] for item in own] == [item["id"] for item in reversed(written)], own
    assert all(item["workflow_name"] == WORKFLOW and item["test"] is False for item in own)
    # No answer holds the planted values, a canary value or a password.
    answers = json.dumps([journey[key] for key in ("everything", "warnings", "step", "across")])
    assert leaked(answers, values) == 0, "a planted secret reached a log answer"
    assert leaked(answers, run_secrets(run)) == 0, "a canary value or a password reached a log answer"
    (run.evidence / "operations-logs.json").write_text(
        json.dumps(
            {
                "run": journey["run"],
                "codes": codes,
                "levels": [item["level"] for item in items],
                "redactions": note["redactions"],
                "across": len(own),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
