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

"""UTC schedule arithmetic, occurrence identity, and bounded calendar parsing."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


def test_public_calendar_strict_next_previous_and_leap_century():
    from firefly_weave.triggers.schedules import calendar

    cron = calendar("0 0 29 2 *", "UTC")
    assert cron.next_fire_time(datetime(2096, 2, 29, tzinfo=UTC)) == datetime(2104, 2, 29, tzinfo=UTC)
    assert cron.previous_fire_time(datetime(2104, 2, 29, tzinfo=UTC)) == datetime(2096, 2, 29, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression",
    ["* * * * * *", "@daily", "0 0 1 * 1", "0 0 ? * *", "0 0 * JAN *", "*/0 * * * *", "0 0 * * 7", "0 0 31 2 *"],
)
def test_rejects_unsupported_or_impossible_cron(expression):
    from firefly_weave.triggers.schedules import calendar

    with pytest.raises(ValueError):
        calendar(expression, "UTC")


def test_occurrence_identity_requires_aware_utc_minute():
    from firefly_weave.triggers.schedules import occurrence_key

    identifier = uuid4()
    now = datetime(2026, 9, 30, tzinfo=UTC)
    assert occurrence_key(identifier, 1, now) == f"{identifier}:1:{now.isoformat()}"
    for invalid in [now.replace(tzinfo=None), now + timedelta(seconds=1)]:
        with pytest.raises(ValueError):
            occurrence_key(identifier, 1, invalid)


def test_skip_grace_exact_boundary_and_backward_clock():
    from firefly_weave.triggers.schedules import calendar, occurrence_window

    cron = calendar("0 * * * *")
    due = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert occurrence_window(cron, due, due + timedelta(seconds=59, microseconds=999999)) == (
        due + timedelta(hours=1),
        due,
        None,
    )
    assert occurrence_window(cron, due, due + timedelta(minutes=1)) == (due + timedelta(hours=1), None, due)
    assert occurrence_window(cron, due, due - timedelta(seconds=1)) == (due, None, None)


def test_calendar_bounds_overflow_month_end_and_host_timezone(monkeypatch):
    import os
    import time

    from firefly_weave.triggers.schedules import calendar

    prior = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Pacific/Honolulu")
        time.tzset()
        assert calendar("0 0 31 * *").next_fire_time(datetime(2026, 4, 1, tzinfo=UTC)) == datetime(
            2026, 5, 31, tzinfo=UTC
        )
        with pytest.raises((ValueError, OverflowError)):
            calendar("* * * * *").next_fire_time(datetime.max.replace(tzinfo=UTC))
        for spec in ["1," * 33 + "2 * * * *", " " * 257, "0 0 * * FRI", "0 0 * * 5-1"]:
            with pytest.raises(ValueError):
                calendar(spec)
        with pytest.raises(ValueError):
            calendar("* * * * *", "America/Los_Angeles")
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


@pytest.mark.parametrize(
    "expression",
    [
        "* * * * * *",
        "@daily",
        "0 0 1 * 1",
        "0 0 ? * *",
        "0 0 * JAN *",
        "*/0 * * * *",
        "0 0 * * 7",
        "0 0 31 2 *",
        "0 0 31 4 *",
        "3/2 * * * *",
        "0 0 * * 5-1",
    ],
)
def test_pure_and_runtime_calendar_reject_with_same_domain_reason(expression):
    from firefly_weave.contracts.schedules import ScheduleRequest, validate_calendar
    from firefly_weave.triggers.schedules import calendar

    with pytest.raises(ValueError) as pure:
        validate_calendar(expression)
    with pytest.raises(ValueError) as runtime:
        calendar(expression)
    assert str(runtime.value) == str(pure.value)
    with pytest.raises(ValueError):
        ScheduleRequest(cron=expression, activation_id=uuid4())


@pytest.mark.parametrize("expression", ["*/15 0-23/2 * * 0,6", "0 0 29 2 *", "0 0 31 * *"])
def test_pure_schedule_and_native_calendar_accept_same_bounded_grammar(expression):
    from firefly_weave.contracts.schedules import ScheduleRequest, validate_calendar
    from firefly_weave.triggers.schedules import calendar

    validate_calendar(expression)
    request = ScheduleRequest(cron=expression, activation_id=uuid4())
    future = calendar(request.cron).next_fire_time(datetime(2026, 1, 1, tzinfo=UTC))
    assert future > datetime(2026, 1, 1, tzinfo=UTC)
