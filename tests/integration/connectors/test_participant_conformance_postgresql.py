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

"""The participant check against real participants on PostgreSQL: the fenced example worker, a worker without
tombstones, the built-in PostgreSQL connector, and the command."""

import asyncio
import json
import runpy
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner
from participant_fixtures import PROCESSED, catalog, lock, unfenced_handlers, workflow
from test_postgresql import external_database as external_database
from test_postgresql import postgres_connector as postgres_connector

from firefly_weave.cli.main import cli
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.sdk.participant_conformance import (
    ConnectorParticipant,
    HandlerParticipant,
    check_step,
    participant_step,
)
from firefly_weave.sdk.participants import Fence

pytestmark = pytest.mark.integration

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
INPUT = {"moveId": "M-1", "quantity": 2}


@pytest.fixture
async def participant_database(external_database, monkeypatch):
    """The test database with the fence, the example's tables and the unfenced fixture's table; the example's
    handlers reach it through PARTICIPANT_DATABASE_URL."""
    db = external_database["db"]
    await db.execute(Fence().ddl())
    await db.execute(runpy.run_path(str(EXAMPLES / "transaction_participant.py"))["TABLES"])
    await db.execute(PROCESSED)
    settings = external_database["kwargs"]
    monkeypatch.setenv(
        "PARTICIPANT_DATABASE_URL",
        f"postgresql://{quote(settings['user'])}:{quote(settings['password'])}@127.0.0.1:{settings['port']}/"
        f"{settings['database']}?sslmode=disable",
    )
    return db


def example() -> dict:
    return runpy.run_path(str(EXAMPLES / "transaction_participant.py"))


def failures(report) -> dict[str, str]:
    return {item.check: item.reason for item in report.checks if not item.passed}


async def test_a_fenced_worker_passes_every_check(participant_database):
    step = participant_step(workflow(), "hold", catalog())
    report = await check_step(step, HandlerParticipant(example()["handlers"]()), INPUT)
    assert report.passed, failures(report)
    rows = await participant_database.fetch("SELECT status, count(*) AS count FROM holds GROUP BY status")
    # try-then-confirm and confirm-twice leave a confirmed hold, try-twice one held hold; every other check's hold
    # was released or never made.
    assert {row["status"]: row["count"] for row in rows} == {"confirmed": 2, "held": 1}


async def test_a_fenced_worker_that_undoes_by_compensation_passes_every_check(participant_database):
    step = participant_step(workflow(), "add", catalog())
    report = await check_step(step, HandlerParticipant(example()["handlers"]()), INPUT)
    assert report.mode == "compensate" and report.passed, failures(report)
    # forward-twice leaves one addition; every other check's was removed or never made.
    assert await participant_database.fetchval("SELECT count(*) FROM additions") == 1


async def test_a_worker_without_tombstones_fails_only_the_late_try(participant_database):
    step = participant_step(workflow(), "hold", catalog())
    report = await check_step(step, HandlerParticipant(unfenced_handlers()), INPUT)
    assert failures(report) == {"try-after-cancel": "applied-late"}
    late = next(item for item in report.checks if item.check == "try-after-cancel").deliveries[-1].operation_key
    assert await participant_database.fetchval("SELECT status FROM holds WHERE hold_id = $1", late) == "held"


async def test_the_postgresql_connector_passes_every_check(postgres_connector, external_database):
    settings = external_database["kwargs"]
    connection = ConnectionRequest(
        name="postgres",
        connector_version_id=uuid4(),
        config={
            "dialect": "postgresql",
            "host": "127.0.0.1",
            "port": settings["port"],
            "database": external_database["name"],
            "user": external_database["roles"]["write"],
            "role": "command",
            "tls": "disable",
        },
        secretRef={"password": "external-password"},
        allowed_destinations=(f"postgresql://127.0.0.1:{settings['port']}",),
    )
    participant = ConnectorParticipant(
        {"weave-postgresql@1.0.0": postgres_connector},
        {"sql": connection},
        lambda slot, name: external_database["password"],
        {},
    )
    step = participant_step(workflow(), "reserve", catalog())
    report = await check_step(step, participant, INPUT, vary=["/moveId"])
    assert report.passed, failures(report)
    late = next(item for item in report.checks if item.check == "try-after-cancel").deliveries[-1]
    assert (late.code, late.effect) == ("SQL_ALREADY_UNDONE", "none")
    rows = await external_database["db"].fetch(
        "SELECT status, count(*) AS count FROM business.customers WHERE customer_id LIKE 'M-1-%' GROUP BY status"
    )
    assert {row["status"]: row["count"] for row in rows} == {"kept": 2, "held": 1}


async def test_the_command_checks_a_fenced_worker(participant_database, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    (tmp_path / "move.workflow.yaml").write_text(yaml.safe_dump(workflow()), encoding="utf-8")
    (tmp_path / "catalog.lock.json").write_text(json.dumps(lock()), encoding="utf-8")
    (tmp_path / "input.json").write_text(json.dumps(INPUT), encoding="utf-8")
    # The command runs its own event loop, as it does in a process of its own.
    result = await asyncio.to_thread(
        CliRunner().invoke,
        cli,
        [
            "transactions",
            "conformance",
            str(tmp_path / "move.workflow.yaml"),
            "--step",
            "hold",
            "--catalog",
            str(tmp_path / "catalog.lock.json"),
            "--input",
            str(tmp_path / "input.json"),
            "--handlers",
            "transaction_participant:handlers",
            "--output",
            "json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["passed"] is True and len(report["checks"]) == 7
