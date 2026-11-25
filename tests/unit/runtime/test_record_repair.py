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

"""Checking one event of a run's history, apart from the database it comes from. What a repair reads and writes is
tested on PostgreSQL."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.operations import exports
from firefly_weave.runtime import kernel
from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures
from firefly_weave.runtime.models import RunState
from firefly_weave.runtime.record_repair import History, read_moment

HISTORY = History(uuid4(), None, [{2: ["move[0]"]}], {}, False)


def stored():
    """A database whose run stored one event after its first."""

    async def rows(sql, **values):
        return [
            {
                "id": uuid4(),
                "type": "task_completed",
                "data": "{}",
                "created_at": datetime(2026, 11, 1, tzinfo=UTC),
                "digest": "sha256:" + "0" * 64,
                "recorded": "{}",
                "preceded": True,
                "before": json.dumps(RunState(input={}).model_dump(mode="json")),
            }
        ]

    return SimpleNamespace(rows=rows)


async def test_an_event_past_todays_limits_is_left_and_not_counted_against_the_callers_transaction(monkeypatch):
    def reducing(state, event, artifact):
        raise RuntimeCapacityError()

    monkeypatch.setattr(kernel, "transition", reducing)
    failures: list[bool] = []
    token = capacity_failures.set(failures)
    try:
        found = await read_moment(stored(), HISTORY, HISTORY.moments[0])
    finally:
        capacity_failures.reset(token)
    assert (found.records, found.left) == ({}, {"move[0]": "too_large"})
    assert failures == []


async def test_a_check_whose_read_ended_stops_at_its_next_step(monkeypatch):
    started, go_on, digests = threading.Event(), threading.Event(), []

    def reducing(state, event, artifact):
        started.set()
        go_on.wait(5)
        return SimpleNamespace(model_dump=lambda mode: {})

    monkeypatch.setattr(kernel, "transition", reducing)
    monkeypatch.setattr(exports, "transition_digest", digests.append)
    reading = asyncio.ensure_future(read_moment(stored(), HISTORY, HISTORY.moments[0]))
    assert await asyncio.to_thread(started.wait, 5)
    [check] = [thread for thread in threading.enumerate() if thread.name == "weave-pure"]
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    go_on.set()
    await asyncio.to_thread(check.join, 5)
    assert not check.is_alive()
    assert digests == []
