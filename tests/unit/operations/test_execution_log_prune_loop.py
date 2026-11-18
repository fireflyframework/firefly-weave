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

    async def prune(tx, now, days):
        asked.append(days)
        if isinstance(tx, Exception):
            raise tx
        return tx

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


async def test_a_failed_prune_is_logged_by_its_kind_and_leaves_the_visit_incomplete(monkeypatch, caplog):
    subject, _, _ = loop(monkeypatch, [RuntimeError("relation is locked by a secret-named thing")])
    with caplog.at_level(logging.ERROR):
        assert await subject.maintain_logs(authority(), 0) is False
    assert "Operations log pruning failed (RuntimeError)" in caplog.text
    assert "secret-named" not in caplog.text
