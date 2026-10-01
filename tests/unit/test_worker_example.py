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

"""Production worker finite authentication lifetime and fault separation policy."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def example():
    spec = importlib.util.spec_from_file_location("worker_example", ROOT / "examples/worker/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_drains_before_access_token_expiration():
    worker = example()
    assert worker.token_drain_delay(300, elapsed=5) == 85
    assert worker.token_drain_delay(240, elapsed=10) == 20
    for invalid in (None, True, 210, -1, "300", float("inf")):
        with pytest.raises(ValueError):
            worker.token_drain_delay(invalid, elapsed=1)


def test_production_example_has_no_fault_switch():
    source = (ROOT / "examples/worker/main.py").read_text()
    assert "WEAVE_TEST_CRASH_PROOF" not in source
    assert "os._exit" not in source
    assert "access_token" in source
