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


"""Saved test data of a workflow that platform events start: a sample event is checked against the platform's view."""

import json
from typing import Any

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.test_data import check_test_data
from firefly_weave.contracts.studio_documents import StudioTestData
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA

WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "notify-team", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"$ref": PLATFORM_EVENT_SCHEMA},
        "outputSchema": {},
        "eventTrigger": {"events": ["transaction.in_doubt"]},
        "steps": [],
        "output": {"literal": None},
    },
}
SAMPLE = {
    "version": "weave/platform-event-v1",
    "event_id": "6f1d2c3b-0a9e-4f8d-8c7b-6a5e4d3c2b1a",
    "type": "transaction.in_doubt",
    "emitted_at": "2026-11-19T10:00:00Z",
    "transaction": {
        "workflow": {"name": "move-between-systems", "version": "1.0.0"},
        "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
        "instance_key": "move-1",
        "business_key": "REF-1029",
        "state": "in_doubt",
        "steps": {"done": [{"step": "hold", "instance_key": "hold-1", "how": "reserve"}]},
        "test": False,
    },
}


def data(**fields: Any) -> StudioTestData:
    document = {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-11-19T10:00:00Z", **fields}
    return StudioTestData.model_validate_json(json.dumps(document), by_alias=True, by_name=False)


def test_a_sample_platform_event_is_kept_as_test_input():
    saved = data(input={"value": SAMPLE, "source": "manual"})
    assert check_test_data(WORKFLOW, saved, CatalogSnapshot.empty()) == [("/input", None)]


def test_test_input_that_isnt_a_platform_event_is_refused():
    saved = data(input={"value": {"version": "weave/platform-event-v1"}, "source": "manual"})
    assert check_test_data(WORKFLOW, saved, CatalogSnapshot.empty()) == [("/input", "WV-STUDIO-TESTDATA-SCHEMA")]
