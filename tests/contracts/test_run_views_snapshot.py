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

"""Reviewed snapshot of the run views contract; any change needs Lane E and Lane L review."""

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures/contracts/run-views.snapshot.json"
OPERATION_IDS = ("run_summaries.list", "runs.steps", "runs.logs")
# Pages embed their item schemas (RunSummary, StepFact, StepFactWithOutput, RunLogEntry) under $defs.
SCHEMAS = ("run-summary-page", "run-summary-query", "step-fact-page", "run-step-query", "run-log-page", "run-log-query")


def render() -> str:
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.schema_export import export_schemas
    from firefly_weave.contracts.surface import OPERATIONS

    spec, schemas = export_openapi(), export_schemas()
    operations = {}
    for identifier in OPERATION_IDS:
        operation = OPERATIONS[identifier]
        native = spec["paths"][operation.canonical_path][operation.method.lower()]
        operations[identifier] = {
            "method": operation.method,
            "path": operation.canonical_path,
            "capability": operation.capability,
            "served": operation.served,
            "query": [parameter["name"] for parameter in native["parameters"] if parameter["in"] == "query"],
            "responses": sorted(native["responses"]),
            "response": native["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[1],
        }
    document = {"operations": operations, "schemas": {name: schemas[name] for name in SCHEMAS}}
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_run_views_contract_matches_the_reviewed_snapshot():
    assert render() == SNAPSHOT.read_text(encoding="utf-8"), (
        "The run views contract changed. Review the change with Lanes E and L, then regenerate it with "
        "`python tests/contracts/test_run_views_snapshot.py --write`."
    )


def test_snapshot_documents_each_operation_exactly():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    environment = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
    assert document["operations"] == {
        "run_summaries.list": {
            "method": "GET",
            "path": environment + "/run-summaries",
            "capability": "run.read",
            "served": False,
            "query": [
                "workflow",
                "version",
                "status",
                "origin",
                "started_after",
                "started_before",
                "include_test",
                "caller_run_id",
                "top_level_only",
                "retried_from_run_id",
                "business_key",
                "correlation_key",
                "has_active_incident",
                "include_archived",
                "activation_id",
                "order",
                "limit",
                "cursor",
            ],
            "responses": ["200", "401", "403", "404", "409", "412", "413", "422", "500", "501"],
            "response": "RunSummaryPage",
        },
        "runs.steps": {
            "method": "GET",
            "path": environment + "/runs/{identifier}/steps",
            "capability": "run.read",
            "served": False,
            "query": ["step", "include", "limit", "cursor"],
            "responses": ["200", "401", "403", "404", "409", "412", "413", "422", "500", "501"],
            "response": "StepFactPage",
        },
        "runs.logs": {
            "method": "GET",
            "path": environment + "/runs/{identifier}/logs",
            "capability": "run.read",
            "served": False,
            "query": ["level", "source", "node_id", "limit", "cursor"],
            "responses": ["200", "401", "403", "404", "409", "412", "413", "422", "500", "501"],
            "response": "RunLogPage",
        },
    }
    assert sorted(document["schemas"]) == sorted(SCHEMAS)
    summary = document["schemas"]["run-summary-page"]["$defs"]["RunSummary"]
    assert sorted(summary["required"]) == sorted(summary["properties"])
    steps = document["schemas"]["step-fact-page"]["$defs"]
    assert set(steps["StepFact"]["properties"]) - set(steps["StepFact"]["required"]) == {"omissions"}
    assert set(steps["StepFactWithOutput"]["required"]) - set(steps["StepFact"]["required"]) == {"output"}


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        raise SystemExit("Usage: python tests/contracts/test_run_views_snapshot.py --write")
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(render(), encoding="utf-8", newline="\n")
