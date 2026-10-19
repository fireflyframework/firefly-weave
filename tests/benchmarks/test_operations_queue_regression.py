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

"""Queue throughput of a candidate against a baseline: at most 5 % slower in every configuration.

OPS_QUEUE_BASELINE and OPS_QUEUE_CANDIDATE are absolute directories that each hold five rounds of
tests/benchmarks/test_queue_load.py evidence (round-1 ... round-5), measured sequentially on the same machine,
Docker allocation, PostgreSQL version and dataset. Each round is an independent sample; the medians are compared.
"""

import json
import os
import statistics
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("replicas,concurrency", [(1, 1), (1, 4), (2, 1), (2, 4)])
def test_queue_throughput_regression_is_at_most_five_percent(replicas, concurrency):
    roots = [Path(os.environ[name]) for name in ("OPS_QUEUE_BASELINE", "OPS_QUEUE_CANDIDATE")]
    samples = []
    for root in roots:
        rows = [
            json.loads((root / f"round-{n}/queue-{replicas}-{concurrency}/measurement.json").read_text())
            for n in range(1, 6)
        ]
        assert all(row["dataset"] == row["accepted"] == row["succeeded"] == row["effects"] == 100 for row in rows)
        samples.append(rows)
    for field in (
        "dataset",
        "replicas",
        "concurrency",
        "postgres_version",
        "docker_cpus",
        "docker_memory_bytes",
        "host_platform",
    ):
        assert len({row[field] for rows in samples for row in rows}) == 1, field
    baseline = statistics.median(row["accepted_completions_per_second"] for row in samples[0])
    candidate = statistics.median(row["accepted_completions_per_second"] for row in samples[1])
    assert baseline > 0 and candidate >= baseline * 0.95, (baseline, candidate)
