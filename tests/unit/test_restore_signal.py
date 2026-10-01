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

"""Negative controls for the restored-signal continuation oracle, without backend I/O."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def restore_trial(tmp_path, *, status="succeeded", output=None, consumed=True, transition=True):
    spec = importlib.util.spec_from_file_location(
        "restore_signal_oracle", Path(__file__).parents[1] / "e2e/support/restore_matrix.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Response:
        def __init__(self, code, value):
            self.status_code, self.value = code, value

        def json(self):
            return self.value

    class Client:
        async def post(self, path, **kwargs):
            return Response(409 if path.endswith("/tasks/complete") else 202, {"id": "receipt"})

        async def get(self, path):
            if "/foreign/" in path:
                return Response(403, {})
            if path.endswith("/occurrences"):
                return Response(200, [{"id": "occurrence"}])
            if path.endswith("/signal"):
                return Response(200, {"state": {"status": status, "output": {} if output is None else output}})
            return Response(200, {"state": {"status": "succeeded"}})

    class Trial:
        host = SimpleNamespace(environment_url="/tenants/tenant/projects/project/environments/env")
        client, tokens, api_url, directory, base_env, other_tenant = (
            Client(),
            ["host", "worker"],
            "unused",
            tmp_path,
            {},
            "foreign",
        )
        scope = SimpleNamespace(tenant_id="tenant", model_dump=lambda **kwargs: {})

        async def spawn(self, *args):
            return object()

        async def observe(self, name):
            if name == "restore-signal":
                return {
                    "runs": [{"id": "signal", "status": status, "sequence": "2"}],
                    "signals": [{"id": "receipt", "external_event_id": "restore-approved", "consumed": consumed}],
                    "events": [{"id": "receipt", "run_id": "signal", "type": "signal_received", "sequence": 2}]
                    if transition
                    else [],
                }
            return {
                "tasks": [{"status": "completed" if name == "restore-safe" else "incident", "operation_key": "safe"}]
            }

    async def joined(*args):
        return 0

    trial = Trial()
    trial.owner = SimpleNamespace(joined=joined)
    seeded = {
        "signal": {"id": "signal"},
        "timer": {"id": "timer"},
        "schedule": {"id": "schedule"},
        "tasks": [
            {"worker": {"id": "worker"}, "lease": {"operation_key": "safe", "proof": {}}},
            {"lease": {"operation_key": "unsafe"}},
        ],
    }
    receiver = SimpleNamespace(
        url="unused", snapshot=lambda: {"effects": ["safe", "unsafe"], "attempts": [{"operation_key": "unsafe"}]}
    )
    current = {"wheel": "unused", "sha": "unused", "worker_python": "unused"}
    return module, trial, seeded, receiver, current


@pytest.mark.asyncio
async def test_permanently_waiting_signal_cannot_pass_restore_continuation(tmp_path):
    module, *arguments = restore_trial(tmp_path, status="waiting")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await module.resume_core(*arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [{"output": {"wrong": True}}, {"consumed": False}, {"transition": False}])
async def test_signal_success_requires_output_and_durable_consumption(tmp_path, corruption):
    module, *arguments = restore_trial(tmp_path, **corruption)
    with pytest.raises(AssertionError):
        await module.resume_core(*arguments)


@pytest.mark.asyncio
async def test_completed_signal_preserves_observed_continuation_proof(tmp_path):
    module, *arguments = restore_trial(tmp_path)
    result = await module.resume_core(*arguments)
    assert result["signal_status"] == "succeeded"
    assert result["signal_output"] == {}
    assert result["signal_final"]["signals"][0]["consumed"] is True
    assert result["signal_final"]["events"][0]["id"] == "receipt"
