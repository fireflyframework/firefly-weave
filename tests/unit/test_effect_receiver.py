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

"""Independent durable receiver retains attempts across response loss and retry."""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "weave_effect_receiver_tests", Path(__file__).parents[1] / "e2e/support/effect_receiver.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def test_lost_response_keeps_one_effect_and_repeated_attempts(tmp_path):
    receiver = await module.EffectReceiver(tmp_path / "effects.sqlite3").open()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.post(receiver.url + "/effect-loss", json={}, headers={"Idempotency-Key": "CaseSensitive"})
            assert (await client.get(receiver.url + "/accepted", params={"key": "CaseSensitive"})).json() == {
                "accepted": True
            }
            assert (
                await client.post(receiver.url + "/effect", json={}, headers={"Idempotency-Key": "CaseSensitive"})
            ).status_code == 200
        snapshot = receiver.snapshot()
        assert snapshot["effects"] == ["CaseSensitive"]
        assert len(snapshot["attempts"]) == 2
        assert all(row["accepted"] for row in snapshot["attempts"])
    finally:
        await receiver.close()
