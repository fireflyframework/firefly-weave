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


"""The rollup's pure rules: its window, catch-up, completeness, closing order and the series caps."""

from datetime import UTC, datetime, timedelta

from firefly_weave.operations.statistics import (
    CATCH_UP,
    HOURS_PER_CYCLE,
    RETENTION,
    WINDOW,
    BucketRow,
    cap_series,
    complete_until,
    days_to_close,
    fold_task_types,
    hours_to_close,
    window,
)
from firefly_weave.operations.statistics_histogram import histogram, merge

NOW = datetime(2026, 10, 26, 14, 32, 10, tzinfo=UTC)
BUCKET = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)


def test_the_window_starts_ten_minutes_back_on_whole_buckets():
    assert timedelta(minutes=10) == WINDOW
    lo, hi = window(NOW, None)
    assert lo == datetime(2026, 10, 26, 14, 20, tzinfo=UTC)
    # The bucket holding now is recomputed too: the window ends on the next whole bucket.
    assert hi == datetime(2026, 10, 26, 14, 35, tzinfo=UTC)
    # A recent cursor never shortens the ten minutes.
    assert window(NOW, datetime(2026, 10, 26, 14, 35, tzinfo=UTC)) == (lo, hi)


def test_after_downtime_the_window_catches_up_one_hour_per_cycle():
    assert timedelta(hours=1) == CATCH_UP
    rolled = datetime(2026, 10, 26, 11, 7, tzinfo=UTC)
    lo, hi = window(NOW, rolled)
    assert (lo, hi) == (datetime(2026, 10, 26, 11, 5, tzinfo=UTC), datetime(2026, 10, 26, 12, 5, tzinfo=UTC))
    lo, hi = window(NOW, hi)
    assert (lo, hi) == (datetime(2026, 10, 26, 12, 5, tzinfo=UTC), datetime(2026, 10, 26, 13, 5, tzinfo=UTC))


def test_the_window_never_reaches_past_the_five_minute_retention():
    lo, hi = window(NOW, NOW - timedelta(days=9))
    assert lo == datetime(2026, 10, 24, 14, 35, tzinfo=UTC)
    assert lo >= NOW - RETENTION["5m"] and hi - lo == CATCH_UP


def test_complete_until_is_the_bucket_ending_five_minutes_before_the_rollup():
    assert complete_until(NOW, None) is None
    assert complete_until(NOW, datetime(2026, 10, 26, 14, 35, tzinfo=UTC)) == datetime(2026, 10, 26, 14, 25, tzinfo=UTC)
    # While catching up, completeness follows the cursor, not the clock.
    assert complete_until(NOW, datetime(2026, 10, 26, 12, 5, tzinfo=UTC)) == datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def test_hours_close_only_after_their_last_bucket_left_the_window():
    rolled = datetime(2026, 10, 26, 14, 35, tzinfo=UTC)
    # The window reaches back to 14:25, so the 13:00 hour is final and the 14:00 hour is not.
    assert hours_to_close(datetime(2026, 10, 26, 12, 0, tzinfo=UTC), rolled) == [
        datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 26, 13, 0, tzinfo=UTC),
    ]
    assert hours_to_close(datetime(2026, 10, 26, 14, 0, tzinfo=UTC), rolled) == []
    # At 14:06 the cursor sits on 14:10, and the next window still recomputes 13:55: the 13:00 hour stays open.
    assert hours_to_close(datetime(2026, 10, 26, 13, 0, tzinfo=UTC), datetime(2026, 10, 26, 14, 10, tzinfo=UTC)) == []
    assert hours_to_close(datetime(2026, 10, 26, 13, 0, tzinfo=UTC), datetime(2026, 10, 26, 14, 15, tzinfo=UTC)) == [
        datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
    ]
    assert hours_to_close(None, rolled) == []
    many = hours_to_close(datetime(2026, 10, 25, 0, 0, tzinfo=UTC), rolled)
    assert len(many) == HOURS_PER_CYCLE and many[0] == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    # A day closes once its last hour has closed.
    assert days_to_close(datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 10, 26, 13, 0, tzinfo=UTC)) == [
        datetime(2026, 10, 25, tzinfo=UTC)
    ]
    assert days_to_close(datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 10, 26, 14, 0, tzinfo=UTC)) == []


def row(workflow: str, started: int, ended: int, at: datetime = BUCKET) -> BucketRow:
    return BucketRow(
        bucket_start=at,
        workflow_name=workflow,
        workflow_version="1.0.0" if workflow != "(other)" else "",
        started=started,
        succeeded=ended,
        failed=0,
        cancelled=0,
        timed_out=0,
        duration_hist=histogram([1_000 * (started + 1)] * ended),
        duration_sum_ms=ended,
        last_ended_at=at if ended else None,
    )


def test_series_past_the_cap_fold_into_other_with_merged_histograms():
    rows = [row(f"flow-{index:04d}", index + 3, index + 3) for index in range(2_003)]
    rows.append(row("other-bucket", 1, 1, BUCKET - timedelta(minutes=5)))
    kept, folded = cap_series(rows, cap=2_000)
    in_bucket = [item for item in kept if item.bucket_start == BUCKET]
    assert len(in_bucket) == 2_001
    other = next(item for item in in_bucket if item.workflow_name == "(other)")
    smallest = rows[:3]
    assert other.workflow_version == ""
    assert other.started == sum(item.started for item in smallest)
    assert other.succeeded == sum(item.succeeded for item in smallest)
    assert tuple(other.duration_hist) == merge(*(item.duration_hist for item in smallest))
    assert folded == {(BUCKET, f"flow-{index:04d}", "1.0.0") for index in range(3)}
    # Another bucket keeps its own series.
    assert any(item.workflow_name == "other-bucket" for item in kept)


def test_folding_adds_to_the_withheld_runs_already_under_other():
    rows = [row("(other)", 2, 2), row("a", 5, 5), row("b", 1, 1)]
    kept, folded = cap_series(rows, cap=1)
    assert sorted(item.workflow_name for item in kept) == ["(other)", "a"]
    other = next(item for item in kept if item.workflow_name == "(other)")
    assert (other.started, other.succeeded) == (3, 3) and folded == {(BUCKET, "b", "1.0.0")}


def test_task_types_past_fifty_fold_into_other():
    rows = [(f"type-{index:02d}", 100 - index, float(index)) for index in range(53)]
    kept = fold_task_types(rows, cap=50)
    assert len(kept) == 51
    assert kept[-1] == ("(other)", sum(100 - index for index in range(50, 53)), 52.0)
    assert fold_task_types([("a", 1, None)], cap=50) == [("a", 1, None)]
