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


"""Duration histograms: 64 logarithmic buckets at ratio √2, exact bucket edges and bounded percentiles."""

import math
import random
from bisect import bisect_right

import pytest

from firefly_weave.operations.statistics_histogram import (
    BUCKETS,
    LARGEST_MS,
    THRESHOLDS_US,
    bucket,
    histogram,
    lower_ms,
    merge,
    quantile,
    upper_ms,
)

RATIO = math.sqrt(2)


def test_bucket_edges_are_exact_integer_microseconds_at_ratio_root_two():
    assert BUCKETS == 64 and len(THRESHOLDS_US) == 63
    assert list(THRESHOLDS_US) == sorted(THRESHOLDS_US)
    for index, threshold in enumerate(THRESHOLDS_US, start=1):
        # The smallest whole microsecond that reaches 2^((i-1)/2) ms: t² ≥ 2^(i-1)·10^6 > (t-1)².
        assert threshold**2 >= 2 ** (index - 1) * 10**6 > (threshold - 1) ** 2
    assert THRESHOLDS_US[0] == 1_000 and THRESHOLDS_US[-1] == 2**31 * 1_000
    assert lower_ms(0) == 0 and upper_ms(0) == 1
    assert lower_ms(1) == 1 and upper_ms(1) == pytest.approx(RATIO)
    assert lower_ms(63) == LARGEST_MS == 2**31 and upper_ms(63) is None


@pytest.mark.parametrize(
    ("microseconds", "expected"),
    [
        (0, 0),
        (999, 0),
        (1_000, 1),
        (1_414, 1),
        (1_415, 2),
        (2_000, 3),
        (2_828, 3),
        (2_829, 4),
        (60_000_000, 32),
        (2**31 * 1_000 - 1, 62),
        (2**31 * 1_000, 63),
        (2**40 * 1_000, 63),
    ],
)
def test_a_duration_falls_in_the_bucket_whose_edges_contain_it(microseconds, expected):
    assert bucket(microseconds) == expected
    assert lower_ms(expected) * 1_000 <= microseconds
    if expected < 63:
        assert microseconds < upper_ms(expected) * 1_000


def test_negative_and_non_integer_durations_are_refused():
    for value in (-1, 1.5, True, None):
        with pytest.raises(ValueError):
            bucket(value)  # type: ignore[arg-type]


def test_histograms_count_merge_exactly_and_refuse_the_wrong_length():
    first = histogram([0, 1_000, 1_000, 2_000])
    second = histogram([2_000, 2**40])
    assert len(first) == BUCKETS and sum(first) == 4 and first[1] == 2
    assert merge(first, second) == histogram([0, 1_000, 1_000, 2_000, 2_000, 2**40])
    assert merge() == histogram([])
    with pytest.raises(ValueError):
        merge((0,) * 63)
    with pytest.raises(ValueError):
        merge((-1,) + (0,) * 63)


def test_percentiles_of_nothing_are_unknown_and_q_is_a_fraction():
    assert quantile(histogram([]), 0.5) is None
    for q in (0, -0.1, 1.01, math.nan):
        with pytest.raises(ValueError):
            quantile(histogram([1_000]), q)


def test_percentiles_interpolate_geometrically_inside_a_bucket():
    one = histogram([5_000])
    assert lower_ms(bucket(5_000)) <= quantile(one, 0.5) < upper_ms(bucket(5_000))
    assert quantile(one, 0.5) == pytest.approx(math.sqrt(lower_ms(bucket(5_000)) * upper_ms(bucket(5_000))))
    assert quantile(one, 1.0) == pytest.approx(upper_ms(bucket(5_000)))
    # Under one millisecond the scale is linear from zero; the open top bucket answers its lower edge.
    assert quantile(histogram([400]), 0.5) == pytest.approx(0.5)
    assert quantile(histogram([2**40 * 1_000]), 0.95) == LARGEST_MS


def exact(samples_us, q):
    ordered = sorted(samples_us)
    return ordered[math.ceil(q * len(ordered)) - 1] / 1_000


@pytest.mark.parametrize("seed", range(40))
def test_interpolated_p50_and_p95_are_within_a_factor_of_root_two_of_the_exact_value(seed):
    generator = random.Random(seed)
    size = generator.choice([1, 2, 5, 20, 100, 1_000])
    # Log-uniform from 1 ms to 2^31 ms, the range where the bound holds, with ties now and then.
    samples = [int(1_000 * 2 ** generator.uniform(0, 30.99)) for _ in range(size)]
    samples += samples[: size // 10]
    counts = histogram(samples)
    for q in (0.5, 0.95):
        estimate, truth = quantile(counts, q), exact(samples, q)
        assert truth / RATIO <= estimate <= truth * RATIO, (q, estimate, truth)


def test_the_bucket_of_every_threshold_matches_a_sorted_search():
    for index, threshold in enumerate(THRESHOLDS_US, start=1):
        for value in (threshold - 1, threshold, threshold + 1):
            assert bucket(value) == bisect_right(THRESHOLDS_US, value)
        assert bucket(threshold) >= index
