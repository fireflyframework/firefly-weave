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


"""Duration histograms for run statistics: 64 logarithmic buckets at ratio √2, from under 1 ms to about 25 days.

Bucket 0 holds durations under 1 ms, bucket *i* (1–62) holds [2^((i−1)/2), 2^(i/2)) ms and bucket 63 holds 2^31 ms
or more. Edges are whole microseconds, so the database and this module place every duration in the same bucket:
``THRESHOLDS_US`` is the sorted list of lower edges that PostgreSQL's ``width_bucket`` reads. Histograms add, so
five-minute buckets merge into hours and days exactly, and a percentile read from a merged histogram is within one
bucket (a factor of √2) of the exact value.
"""

import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence

BUCKETS = 64
# The open top bucket starts here; its percentile is reported as this lower edge.
LARGEST_MS = 2**31


def _edge(index: int) -> int:
    """The smallest whole microsecond t with t ≥ 2^((index−1)/2) ms, that is t² ≥ 2^(index−1)·10^6."""
    square = 2 ** (index - 1) * 10**6
    root = math.isqrt(square)
    return root if root * root == square else root + 1


# Lower edges of buckets 1–63 in whole microseconds, smallest first.
THRESHOLDS_US: tuple[int, ...] = tuple(_edge(index) for index in range(1, BUCKETS))


def bucket(microseconds: int) -> int:
    """The bucket of a nonnegative whole number of microseconds."""
    if type(microseconds) is not int or microseconds < 0:
        raise ValueError("A duration is a nonnegative whole number of microseconds")
    return bisect_right(THRESHOLDS_US, microseconds)


def lower_ms(index: int) -> float:
    """The lower edge of a bucket in milliseconds (0 for bucket 0)."""
    if not 0 <= index < BUCKETS:
        raise ValueError("Unknown histogram bucket")
    return 0.0 if index == 0 else 2 ** ((index - 1) / 2)


def upper_ms(index: int) -> float | None:
    """The exclusive upper edge of a bucket in milliseconds; the top bucket has none."""
    if not 0 <= index < BUCKETS:
        raise ValueError("Unknown histogram bucket")
    return None if index == BUCKETS - 1 else 2 ** (index / 2)


def histogram(durations_us: Iterable[int]) -> tuple[int, ...]:
    counts = [0] * BUCKETS
    for value in durations_us:
        counts[bucket(value)] += 1
    return tuple(counts)


def _checked(counts: Sequence[int]) -> Sequence[int]:
    if len(counts) != BUCKETS or any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("A histogram is 64 nonnegative counts")
    return counts


def merge(*histograms: Sequence[int]) -> tuple[int, ...]:
    """Add histograms bucket by bucket."""
    total = [0] * BUCKETS
    for counts in histograms:
        for index, value in enumerate(_checked(counts)):
            total[index] += value
    return tuple(total)


def quantile(counts: Sequence[int], q: float) -> float | None:
    """The q-quantile in milliseconds (0 < q ≤ 1), or None for an empty histogram.

    The bucket holding the rank q·n is the bucket of the exact nearest-rank value; inside it the value is
    interpolated geometrically (linearly from 0 in bucket 0), and the open top bucket answers its lower edge.
    """
    if not isinstance(q, float | int) or isinstance(q, bool) or not 0 < q <= 1:
        raise ValueError("A quantile is a fraction greater than 0 and at most 1")
    total = sum(_checked(counts))
    if total == 0:
        return None
    target = q * total
    before = 0
    for index, count in enumerate(counts):
        if count and before + count >= target:
            fraction = (target - before) / count
            if index == 0:
                return fraction
            upper = upper_ms(index)
            if upper is None:
                return float(LARGEST_MS)
            low = lower_ms(index)
            return float(low * (upper / low) ** fraction)
        before += count
    raise AssertionError("unreachable: the last nonempty bucket holds the total")
