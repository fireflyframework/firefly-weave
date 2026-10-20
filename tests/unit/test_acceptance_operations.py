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

"""J5.1 reads the failing run's incident only once recovery has settled it."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "acceptance_operations_overview", ROOT / "tests/acceptance/test_operations_overview.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


overview = load()


def test_the_failing_runs_incident_counts_once_recovery_has_settled_it():
    # Seen in CI for J9.3, and the same here: the worker's failure opens WV-TASK-FAILED, and recovery turns it into
    # WV-TASK-RETRIES-EXHAUSTED when no retry is left. The run is already suspended at the first code.
    first = {"items": [{"run_id": "r1", "code": "WV-TASK-FAILED", "status": "active"}]}
    settled = {"items": [{"run_id": "r1", "code": "WV-TASK-RETRIES-EXHAUSTED", "status": "active"}]}
    other = {"items": [{"run_id": "r2", "code": "WV-TASK-RETRIES-EXHAUSTED", "status": "active"}]}
    assert not overview.incident_settled(first, "r1")
    assert overview.incident_settled(settled, "r1")
    assert not overview.incident_settled(other, "r1")
