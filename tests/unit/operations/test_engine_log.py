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

"""The engine's entries are worded from a fixed table and read only the step and an error code."""

from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest

from firefly_weave.operations.engine_log import (
    ENGINE_ENTRIES,
    engine_entry,
    fact_entry_id,
    rank_sql,
    run_fact_entry,
    step_fact_entry,
)
from firefly_weave.runtime.models import RuntimeEvent

AT = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def test_every_engine_event_type_has_words():
    # A new event type fails here until someone decides how the log words it.
    assert set(ENGINE_ENTRIES) == set(get_args(RuntimeEvent.model_fields["type"].annotation))


@pytest.mark.parametrize(
    "event_type,key,code,expected",
    [
        ("started", None, None, ("info", "RUN.STARTED", "Run started", None, "")),
        ("task_completed", "send", None, ("info", "STEP.SUCCEEDED", "send finished", "send", "")),
        (
            "task_failed",
            "send[2]",
            "HANDLER_FAILED",
            ("warning", "HANDLER_FAILED", "send[2] attempt failed: HANDLER_FAILED", "send", "send[2]"),
        ),
        ("task_failed", "send", None, ("warning", None, "send attempt failed", "send", "")),
        ("recovery_scheduled", "send", "WV-TASK-FAILED", ("warning", "STEP.RETRY", "send will be retried", "send", "")),
        (
            "step_failed",
            "send",
            "WV-TASK-RETRIES-EXHAUSTED",
            ("error", "WV-TASK-RETRIES-EXHAUSTED", "send failed: WV-TASK-RETRIES-EXHAUSTED", "send", ""),
        ),
        (
            "incident_opened",
            "charge",
            "WV-TASK-AMBIGUOUS",
            ("error", "WV-TASK-AMBIGUOUS", "Incident opened at charge: WV-TASK-AMBIGUOUS", "charge", ""),
        ),
        ("cancelled", None, None, ("info", "RUN.CANCELED", "Run canceled", None, "")),
        ("wait_elapsed", "approval", None, ("info", "WAIT.ELAPSED", "approval reached its deadline", "approval", "")),
        ("memory_loaded", "support", None, ("info", None, "support loaded its conversation", "support", "")),
        (
            "memory_loaded",
            "support",
            "AGENT.MEMORY_UNREADABLE",
            (
                "info",
                "AGENT.MEMORY_UNREADABLE",
                "support couldn't read its conversation and started without it: AGENT.MEMORY_UNREADABLE",
                "support",
                "",
            ),
        ),
    ],
)
def test_an_event_is_worded_by_its_template(event_type, key, code, expected):
    entry = engine_entry(uuid4(), event_type, AT, key, code, handled=False)
    assert (entry.level, entry.code, entry.message, entry.node_id, entry.instance_key) == expected
    assert (entry.source, entry.attempt, entry.worker_id, entry.fields) == ("engine", None, None, {})


def test_a_handled_step_failure_is_a_warning_that_says_so():
    entry = engine_entry(uuid4(), "step_failed", AT, "send", "HANDLER_FAILED", handled=True)
    assert (entry.level, entry.message) == ("warning", "send failed: HANDLER_FAILED; the failure was handled")


def test_engine_entries_hold_no_event_data_beyond_step_and_code():
    # A code that isn't one (free text from an event) is never shown; a key only the engine uses names no step.
    entry = engine_entry(uuid4(), "incident_opened", AT, "@txn:pay", "card 4111 1111 1111 1111 declined", handled=False)
    assert (entry.code, entry.message, entry.node_id) == (None, "Incident opened at A step", None)
    assert "4111" not in entry.model_dump_json()


def test_a_long_step_key_keeps_the_message_within_512_characters():
    key = "s" * 512
    entry = engine_entry(uuid4(), "step_failed", AT, key, "HANDLER_FAILED", handled=True)
    assert len(entry.message) == 512 and entry.node_id == key


def test_the_sql_rank_follows_the_table():
    sql = rank_sql("e.type", "handled")
    assert "WHEN 'started' THEN 1" in sql and "WHEN 'timed_out' THEN 3" in sql
    assert "WHEN 'step_failed' THEN CASE WHEN handled THEN 2 ELSE 3 END" in sql
    assert sql.count("WHEN '") == len(ENGINE_ENTRIES)


def test_runs_and_steps_that_ended_badly_read_as_entries_with_stable_ids():
    run = uuid4()
    failed = run_fact_entry(run, "failed", AT, "WV-TASK-FAILED", workflow="orders", version="1.2.0", test=False)
    assert (failed.level, failed.code, failed.message, failed.node_id) == (
        "error",
        "WV-TASK-FAILED",
        "Run failed: WV-TASK-FAILED",
        None,
    )
    canceled = run_fact_entry(run, "cancelled", AT, None, workflow="orders", version="1.2.0", test=True)
    assert (canceled.level, canceled.code, canceled.test) == ("info", "RUN.CANCELED", True)
    step = step_fact_entry(
        run, "send", "send[1]", "failed", AT, None, handled=True, workflow="orders", version="1.2.0", test=False
    )
    assert (step.level, step.code, step.message) == (
        "warning",
        "STEP.FAILED",
        "send[1] failed: STEP.FAILED; the failure was handled",
    )
    assert step.id == fact_entry_id(run, "send[1]", "failed") != failed.id
    late = step_fact_entry(
        run, "send", "", "timed_out", AT, "X", handled=False, workflow="orders", version="1.2.0", test=False
    )
    assert (late.level, late.code, late.message) == ("error", "STEP.TIMED_OUT", "send timed out")
