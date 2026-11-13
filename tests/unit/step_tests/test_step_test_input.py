# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""A step test's input is checked against its action's input schema; refusals name paths and codes, never values."""

import json

from firefly_weave.step_tests.admission import input_refusal

SCHEMA = {
    "type": "object",
    "properties": {"customerId": {"type": "string", "minLength": 3}, "apiKey": {"type": "string", "x-secret": True}},
    "required": ["customerId"],
    "additionalProperties": False,
}


def test_a_valid_input_passes():
    assert input_refusal(SCHEMA, {"customerId": "C-100"}, {}) is None


def test_an_input_that_breaks_the_schema_names_paths_and_codes_only():
    refused = input_refusal(SCHEMA, {"customerId": "9f2c-marker"[:2], "extra": "9f2c-marker"}, {})
    assert refused is not None
    assert (refused.status, refused.code) == (422, "WV-STEPTEST-INPUT")
    diagnostics = refused.result["diagnostics"]
    assert diagnostics and all(set(item) == {"path", "code"} for item in diagnostics)
    assert "9f2c" not in json.dumps(refused.result) + refused.message


def test_an_input_with_a_secret_value_is_refused_with_its_path():
    refused = input_refusal(SCHEMA, {"customerId": "C-100", "apiKey": "9f2c-marker-secret"}, {})
    assert refused is not None
    assert (refused.status, refused.code) == (422, "WV-SCHEMA-SECRET_VALUE")
    assert set(refused.result) == {"paths"}
    assert "9f2c" not in json.dumps(refused.result) + refused.message
