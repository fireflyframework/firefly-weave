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

"""Restore real current/predecessor active work into fresh retained databases."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest.mark.parametrize("predecessor", [False, True], ids=["current", "retained-predecessor"])
async def test_owner_preserving_restore_and_public_continuation(predecessor, tmp_path):
    assert os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") == "colima-weave-tests", "Explicit owned backend required"
    path = Path(__file__).parent / "support/restore_matrix.py"
    spec = importlib.util.spec_from_file_location("weave_restore_matrix", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    result = await module.exercise(tmp_path, predecessor=predecessor)
    assert result["catalog_data_equal"]
    assert result["source_fenced"]
    assert result["safe_operation_key_preserved"]
    assert result["unsafe_effect_attempts"] == 1
    assert result["signal_replay_same_receipt"]
    assert result["signal_status"] == "succeeded"
    assert result["signal_output"] == {}
    assert any(receipt["consumed"] for receipt in result["signal_final"]["signals"])
    assert any(event["type"] == "signal_received" for event in result["signal_final"]["events"])
    assert result["timer_status"] == "succeeded"
    assert result["replicas"] == 2
    assert result["separate_worker"]
