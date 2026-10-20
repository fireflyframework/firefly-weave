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

"""What makes a test-data document run data, and from which environments."""

from uuid import UUID

from firefly_weave.definitions.studio_documents import run_data

RUN = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11"
ENVIRONMENT = "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718"


def test_entries_from_runs_and_test_calls_are_run_data():
    pin = {"output": {}, "source": "test-call", "runId": RUN, "runEnvironmentId": ENVIRONMENT}
    assert run_data({"pins": {"a": pin}}) == (True, [UUID(ENVIRONMENT)])
    turns = {"turns": [], "source": "run", "runId": RUN, "runEnvironmentId": ENVIRONMENT}
    assert run_data({"aiTurns": {"support": turns}}) == (True, [UUID(ENVIRONMENT)])


def test_values_named_like_provenance_are_just_data():
    manual = {"value": {"source": "run", "runId": RUN, "runEnvironmentId": ENVIRONMENT}, "source": "manual"}
    assert run_data({"input": manual}) == (False, [])


def test_manual_and_generated_data_are_not():
    assert run_data({"input": {"value": 1, "source": "schema"}, "pins": {}}) == (False, [])
