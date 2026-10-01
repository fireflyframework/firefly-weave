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

"""Observed invariants must expose missing identities and duplicate effects."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "weave_matrix_observations", Path(__file__).parents[1] / "e2e/support/crash_matrix.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_lost_committed_run_is_not_hidden_by_empty_event_sets():
    before = {"runs": [{"id": "one"}], "events": []}
    final = {"runs": [], "events": []}
    result = module.invariant_counts(before, final, expected_event_types={})
    assert result["lost_committed_work"] == 1


def test_duplicate_semantics_and_unsafe_delivery_attempts_are_counted():
    events = [{"run_id": "run", "id": str(index), "sequence": index, "type": "started"} for index in (1, 2)]
    result = module.invariant_counts(
        {"events": []},
        {"events": events},
        expected_event_types={"started": 1},
        deliveries=[{"operation_key": "same"}, {"operation_key": "same"}],
        safe=False,
    )
    assert result == {"duplicate_accepted_transitions": 1, "lost_committed_work": 0, "unsafe_blind_retries": 1}


@pytest.mark.parametrize("failure", ["owner", "client"])
def test_close_attempts_all_independent_resources_after_failure(failure):
    calls = []

    def close(name):
        calls.append(name)
        if name == failure:
            raise RuntimeError(name)

    async def aclose(name):
        close(name)

    trial = object.__new__(module.InstalledSlice)
    trial.closed = False
    trial.owner = SimpleNamespace(close=lambda: aclose("owner"))
    trial.barrier_socket = SimpleNamespace(close=lambda: close("socket"))
    trial.client = SimpleNamespace(aclose=lambda: aclose("client"))
    trial.engine = SimpleNamespace(dispose=lambda: aclose("engine"))
    trial.observer = SimpleNamespace(dispose=lambda: aclose("observer"))
    trial.target = SimpleNamespace(close=lambda: close("target"), wait_closed=lambda: aclose("listener"))
    trial.containers = []
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(trial.close())
    assert [str(error) for error in caught.value.exceptions] == [failure]
    assert calls == ["owner", "socket", "client", "engine", "observer", "target", "listener"]
    assert trial.closed
    asyncio.run(trial.close())
    assert len(calls) == 7


def test_external_preparation_failure_closes_acquired_receiver(tmp_path, monkeypatch):
    calls = []

    class Receiver:
        def __init__(self, path):
            assert path == tmp_path / "receiver.sqlite3"

        async def open(self):
            calls.append("open")
            return self

        async def close(self):
            calls.append("close")

    async def prepare(**kwargs):
        raise RuntimeError("preparation failed")

    monkeypatch.setattr(module, "load", lambda *_: SimpleNamespace(EffectReceiver=Receiver))
    trial = object.__new__(module.InstalledSlice)
    trial.directory = tmp_path
    trial.prepare_workflow = prepare
    with pytest.raises(RuntimeError, match="preparation failed"):
        asyncio.run(trial.external_case("external_request_in_flight"))
    assert calls == ["open", "close"]
