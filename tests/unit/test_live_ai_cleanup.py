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

"""Keep the owned model service recoverable after an ambiguous test command result."""

import json
import runpy
import subprocess
from pathlib import Path

import pytest

JOURNEYS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "live_ai/test_ollama_journeys.py"))


class InterruptedService:
    """Apply a mutation, then lose its answer exactly once; recovery commands still work."""

    model = "qwen2.5:1.5b"

    def __init__(self, error):
        self.error = error
        self.approved = {self.model}
        self.running = True
        self.recovered = False

    def weave(self, *args):
        if args[-1] == "--served":
            self.approved = {self.model}
            self.recovered = True
            return {"approval": "served"}
        if args[2] == "remove":
            self.approved.remove(self.model)
        else:
            self.approved.add(args[-1])
        raise self.error

    def ollama_containers(self):
        return ["owned-ollama"]

    def docker(self, command, *containers):
        assert containers == ("owned-ollama",)
        self.running = command == "start"
        if command == "stop":
            raise self.error

    def wait_healthy(self, containers):
        assert containers == ["owned-ollama"] and self.running
        self.recovered = True


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("test-command", 1), json.JSONDecodeError("lost output", "", 0)]
)
@pytest.mark.parametrize(
    "journey",
    [
        "test_a_model_the_policy_no_longer_approves_fails_with_llm_policy",
        "test_an_approved_model_that_ollama_does_not_serve_fails_with_model_not_found",
        "test_a_stopped_ollama_fails_with_llm_unreachable_and_runs_again_after_a_restart",
    ],
)
async def test_ambiguous_mutation_result_still_restores_the_owned_service(journey, error):
    service = InterruptedService(error)
    with pytest.raises(type(error)):
        await JOURNEYS[journey](service)
    assert service.approved == {service.model}
    assert service.running
    assert service.recovered
