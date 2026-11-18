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

"""The operations loop prunes old log entries a batch per unit, outside the operation lock, within its budget."""

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import execution_logs
from firefly_weave.operations.loop import OperationsLoop, QuantumIncomplete
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://app@localhost:5432/weave"


def loop(monkeypatch, outcomes, *, retention_days=7):
    """A loop whose units record how they were opened and whose prune answers ``outcomes`` in turn."""
    settings = Settings(database_url=DATABASE, execution_log_retention_days=retention_days)
    subject = OperationsLoop(settings, None, LoopHealth())  # type: ignore[arg-type]
    opened, asked = [], []

    @asynccontextmanager
    async def unit(authority, deadline, *, mutation, admission=True):
        opened.append((mutation, admission))
        outcome = outcomes[len(opened) - 1]
        if outcome is QuantumIncomplete:
            raise QuantumIncomplete()
        yield outcome

    async def prune(tx, now, days, *, batch=execution_logs.PRUNE_BATCH):
        asked.append(days)
        batches.append(batch)
        if isinstance(tx, Exception):
            raise tx
        return tx

    batches: list[int] = []
    subject.asked_batches = batches  # type: ignore[attr-defined]

    monkeypatch.setattr(subject, "_unit", unit)
    monkeypatch.setattr(execution_logs, "prune", prune)
    return subject, opened, asked


def authority() -> _SchedulerScope:
    return _SchedulerScope(Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), object())


async def test_a_backlog_is_pruned_a_batch_per_unit_until_less_than_a_batch_is_left(monkeypatch):
    subject, opened, asked = loop(monkeypatch, [5000, 5000, 12], retention_days=3)
    assert await subject.maintain_logs(authority(), 0) is True
    assert opened == [(True, False)] * 3
    assert asked == [3, 3, 3]


@pytest.mark.parametrize("outcomes,healthy", [([5000, QuantumIncomplete], True), ([QuantumIncomplete], False)])
async def test_running_out_of_time_after_a_batch_still_counts_as_progress(monkeypatch, outcomes, healthy):
    subject, _, _ = loop(monkeypatch, outcomes)
    assert await subject.maintain_logs(authority(), 0) is healthy


class Canceled(Exception):
    sqlstate = "57014"


def timed_out() -> DBAPIError:
    return DBAPIError("DELETE FROM execution_logs", None, Canceled())


async def test_a_batch_that_outruns_the_statement_timeout_is_halved_and_grows_back_after_a_full_one(monkeypatch):
    subject, _, _ = loop(monkeypatch, [timed_out(), timed_out(), 1250, 12, 30])
    scope = authority()
    assert await subject.maintain_logs(scope, 0) is True
    assert subject.asked_batches == [5000, 2500, 1250, 2500]
    # The environment keeps its smaller batch for the next visit.
    assert await subject.maintain_logs(scope, 0) is True
    assert subject.asked_batches[-1] == 2500


async def test_a_timeout_at_the_smallest_batch_is_a_failure(monkeypatch, caplog):
    subject, _, _ = loop(monkeypatch, [timed_out()] * 5)
    with caplog.at_level(logging.ERROR):
        assert await subject.maintain_logs(authority(), 0) is False
    assert subject.asked_batches == [5000, 2500, 1250, 625, 500]
    assert "Operations log pruning failed (DBAPIError)" in caplog.text


async def test_a_failed_prune_is_logged_by_its_kind_and_leaves_the_visit_incomplete(monkeypatch, caplog):
    subject, _, _ = loop(monkeypatch, [RuntimeError("relation is locked by a secret-named thing")])
    with caplog.at_level(logging.ERROR):
        assert await subject.maintain_logs(authority(), 0) is False
    assert "Operations log pruning failed (RuntimeError)" in caplog.text
    assert "secret-named" not in caplog.text
