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


"""The statistics migration freezes the histogram edges the rollup and the reads share; they must match the module."""

import importlib.util
from pathlib import Path

from firefly_weave.operations.statistics_histogram import BUCKETS, THRESHOLDS_US
from firefly_weave.persistence.migrations import SCHEMA_VERSION

PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0039_operations_statistics.py"


def migration():
    spec = importlib.util.spec_from_file_location("statistics_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_frozen_bucket_edges_equal_the_histogram_module():
    frozen = migration()
    assert tuple(int(value) for value in frozen.THRESHOLDS.split(",")) == THRESHOLDS_US
    assert "{" + ",".join(["0"] * BUCKETS) + "}" == frozen.EMPTY


def test_the_migration_is_the_schema_head_and_counts_nothing_toward_usage():
    frozen = migration()
    assert frozen.revision == SCHEMA_VERSION
    source = PATH.read_text()
    assert "weave_usage_charge" not in source and "operation_usage" not in source
    assert source.count("FOREIGN KEY") == 3 and source.count("ON DELETE CASCADE") == 3


def test_the_app_may_count_add_and_bucket_histograms_and_nothing_else_is_granted():
    source = PATH.read_text()
    for signature in (
        "weave_duration_bucket(bigint)",
        "weave_histogram_add(int4[],int4[])",
        "weave_histogram_sum(int4[])",
        "weave_histogram_count(int4[],bigint)",
        "weave_histogram(bigint)",
    ):
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO weave_app" in source, signature
