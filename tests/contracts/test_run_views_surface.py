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

"""Run summaries, steps and logs are registered, documented and authorized like every operation."""

from functools import cache

import pytest

from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.run_views import RunLogPage, RunStepQuery, RunSummaryPage, StepFactPage
from firefly_weave.contracts.surface import OPERATIONS

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
EXPECTED = {
    "run_summaries.list": (ENVIRONMENT + "/run-summaries", RunSummaryPage),
    "runs.steps": (ENVIRONMENT + "/runs/{identifier}/steps", StepFactPage),
    "runs.logs": (ENVIRONMENT + "/runs/{identifier}/logs", RunLogPage),
}


spec = cache(export_openapi)


def native(identifier: str) -> dict:
    operation = OPERATIONS[identifier]
    return spec()["paths"][operation.canonical_path][operation.method.lower()]


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_operation_availability_matches_served_reads(identifier):
    operation = OPERATIONS[identifier]
    path, response = EXPECTED[identifier]
    assert (operation.method, operation.canonical_path, operation.response) == ("GET", path, response)
    assert operation.capability == "run.read"
    assert operation.served is (identifier != "runs.logs")
    assert operation.page is False and operation.request is None
    assert all(other.served for key, other in OPERATIONS.items() if key not in EXPECTED)


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_openapi_documents_query_501_and_bearer_security(identifier):
    document = native(identifier)
    query = [parameter for parameter in document["parameters"] if parameter["in"] == "query"]
    assert [parameter["name"] for parameter in query] == list(OPERATIONS[identifier].query.model_fields)
    assert all(parameter["required"] is False for parameter in query)
    assert ("501" in document["responses"]) is (identifier == "runs.logs")
    assert "422" in document["responses"]
    if identifier == "runs.logs":
        assert document["responses"]["501"]["content"]["application/problem+json"]["schema"]["$ref"].endswith(
            "/Problem"
        )
    assert document["security"] == [{"bearer": []}]
    assert "Required capability: run.read" in document["description"]
    assert ("answers 501 WV-UNAVAILABLE" in document["description"]) is (identifier == "runs.logs")


def test_query_parameters_keep_their_constraints_and_defaults():
    def schema(identifier: str, name: str) -> dict:
        parameter = next(item for item in native(identifier)["parameters"] if item["name"] == name)
        value = parameter["schema"]
        while "$ref" in value:
            value = spec()["components"]["schemas"][value["$ref"].rsplit("/", 1)[1]] | {
                key: item for key, item in value.items() if key != "$ref"
            }
        return value

    assert schema("run_summaries.list", "limit").items() >= {"minimum": 1, "maximum": 100, "default": 50}.items()
    assert schema("runs.steps", "limit").items() >= {"minimum": 1, "maximum": 500, "default": 200}.items()
    assert schema("runs.logs", "limit").items() >= {"minimum": 1, "maximum": 500, "default": 200}.items()
    assert schema("run_summaries.list", "include_test") == {"type": "boolean", "default": False}
    assert schema("run_summaries.list", "order")["default"] == "started_desc"
    assert schema("run_summaries.list", "status")["maxItems"] == 8
    assert schema("runs.steps", "include") == {"const": "output", "type": "string"}
    assert RunStepQuery.model_fields["include"].default is None


def test_every_other_operation_documents_no_501():
    for identifier, operation in OPERATIONS.items():
        if identifier not in EXPECTED:
            assert "501" not in spec()["paths"][operation.canonical_path][operation.method.lower()]["responses"]
