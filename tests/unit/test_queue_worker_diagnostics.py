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

"""Worker failure diagnostics classify known rejection without printing request secrets."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "queue_worker_diagnostics", Path(__file__).parents[1] / "e2e/support/queue_worker.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
def test_explicit_rejection_is_distinguishable_from_ambiguous_wire_failure(code):
    request = httpx.Request(
        "POST",
        "https://private:password@local/private/tasks/complete?token=secret",
        headers={"Authorization": "Bearer secret"},
        content=b"secret-payload",
    )
    response = httpx.Response(429, json={"code": code, "detail": "secret-detail"}, request=request)
    error = httpx.HTTPStatusError("secret-message", request=request, response=response)
    assert module.failure_summary(error) == {
        "failed": True,
        "error_type": "HTTPStatusError",
        "status": 429,
        "operation": "complete",
        "code": code,
    }
    assert module.failure_summary(httpx.ReadTimeout("secret", request=request)) == {
        "failed": True,
        "error_type": "ReadTimeout",
    }


@pytest.mark.parametrize("body", [{"code": "private-secret"}, {"code": []}, {"code": {}}, ["secret"], "not-json"])
def test_unknown_response_details_never_escape(body):
    request = httpx.Request("POST", "https://private.local/private/secret")
    response = httpx.Response(500, json=body, request=request)
    result = module.failure_summary(httpx.HTTPStatusError("secret", request=request, response=response))
    assert result == {
        "failed": True,
        "error_type": "HTTPStatusError",
        "status": 500,
        "operation": "other",
        "code": None,
    }
    assert "secret" not in json.dumps(result)
