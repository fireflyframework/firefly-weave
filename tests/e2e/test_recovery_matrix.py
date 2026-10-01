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

"""Crash recovery uses exact installed children and real guarded backends.

Every selected boundary is repeated in independent namespaces. A further case
kills twice on one logical request before replaying that same request identity.
"""

import importlib.util
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

spec = importlib.util.spec_from_file_location(
    "weave_recovery_matrix", Path(__file__).with_name("support") / "crash_matrix.py"
)
matrix = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = matrix
spec.loader.exec_module(matrix)

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
COMMIT_BOUNDARIES = [
    f"{side}_{kind}_commit" for side in ("before", "after") for kind in ("run", "completion", "signal")
]


@pytest_asyncio.fixture
async def recovery_slice(tmp_path):
    evidence = os.environ.get("WEAVE_D5_EVIDENCE")
    directory = Path(evidence) / ("crash-" + uuid4().hex) if evidence else tmp_path / ("crash-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    trial = matrix.InstalledSlice(directory)
    try:
        await trial.bootstrap()
        yield trial
    finally:
        await trial.close()


@pytest.mark.parametrize("repetition", [1, 2])
@pytest.mark.parametrize("boundary", COMMIT_BOUNDARIES)
async def test_kill_and_restart_preserves_contract(recovery_slice, boundary, repetition):
    evidence = await recovery_slice.commit_case(boundary)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]
    assert len(evidence["crashes"]) == 1


@pytest.mark.parametrize("boundary", COMMIT_BOUNDARIES)
async def test_consecutive_kills_preserve_one_logical_request(recovery_slice, boundary):
    evidence = await recovery_slice.commit_case(boundary, consecutive=2)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]
    assert len(evidence["crashes"]) == 2


EXTERNAL_BOUNDARIES = ["external_request_in_flight", "after_external_accept", "after_external_response_loss"]


@pytest.mark.parametrize("repetition", [1, 2])
@pytest.mark.parametrize("safe", [True, False])
@pytest.mark.parametrize("boundary", EXTERNAL_BOUNDARIES)
async def test_external_request_acceptance_and_lost_response(recovery_slice, boundary, safe, repetition):
    evidence = await recovery_slice.external_case(boundary, safe=safe)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]


@pytest.mark.parametrize("boundary", EXTERNAL_BOUNDARIES)
async def test_consecutive_external_failures_keep_operation_identity(recovery_slice, boundary):
    evidence = await recovery_slice.external_case(boundary, consecutive=2)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]


PROVIDER_BOUNDARIES = [
    f"{side}_provider_{stage}_commit" for side in ("before", "after") for stage in ("admission", "dispatch")
]


@pytest.mark.parametrize("repetition", [1, 2])
@pytest.mark.parametrize("provider", ["teams", "whatsapp"])
@pytest.mark.parametrize("boundary", PROVIDER_BOUNDARIES)
async def test_provider_admission_ack_dispatch_and_protocol_fences(recovery_slice, provider, boundary, repetition):
    providers = matrix.load("weave_process_provider_matrix", matrix.SUPPORT / "provider_matrix.py")
    evidence = await providers.exercise(recovery_slice, provider, boundary, matrix)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]


@pytest.mark.parametrize("consecutive", [1, 1, 2], ids=["repeat-1", "repeat-2", "consecutive"])
async def test_outbox_ack_before_settlement_survives_kill(recovery_slice, consecutive):
    outbox = matrix.load("weave_process_outbox_matrix", matrix.SUPPORT / "outbox_matrix.py")
    evidence = await outbox.exercise(recovery_slice, matrix, consecutive=consecutive)
    assert all(value == 0 for value in evidence["invariants"].values()), evidence["invariants"]
