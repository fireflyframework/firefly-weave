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


"""Run statistics reads are registered, documented with their query, exported and reachable from the CLI and Studio."""

from functools import cache

import pytest
from click.testing import CliRunner

from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.operations_summary import OperationsSummary
from firefly_weave.contracts.run_statistics import (
    RunsDaySection,
    StatisticsSeriesAnswer,
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
    StepStatisticsAnswer,
    WorkflowStatisticsAnswer,
)
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.surface import OPERATIONS

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
EXPECTED = {
    "run_statistics.series": ("/run-statistics/series", StatisticsSeriesAnswer, StatisticsSeriesQuery),
    "run_statistics.workflows": ("/run-statistics/workflows", WorkflowStatisticsAnswer, StatisticsWorkflowsQuery),
    "run_statistics.steps": ("/run-statistics/steps", StepStatisticsAnswer, StatisticsStepsQuery),
}

spec = cache(export_openapi)


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_statistics_are_registered_reads_that_need_status_read(identifier):
    operation = OPERATIONS[identifier]
    path, response, query = EXPECTED[identifier]
    assert (operation.method, operation.canonical_path) == ("GET", ENVIRONMENT + path)
    assert (operation.response, operation.query, operation.capability) == (response, query, "status.read")
    assert not operation.page and operation.request is None
    # Published first; the statistics service starts serving them.
    assert operation.served is False


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_openapi_documents_from_and_to_under_their_wire_names(identifier):
    document = spec()["paths"][OPERATIONS[identifier].canonical_path]["get"]
    names = {parameter["name"]: parameter for parameter in document["parameters"] if parameter["in"] == "query"}
    assert {"from", "to", "granularity"} <= set(names) and "from_" not in names
    assert names["from"]["required"] and names["to"]["required"] and not names["granularity"]["required"]
    assert "Required capability: status.read" in document["description"]


def test_series_documents_repeatable_signals_and_grouping():
    document = spec()["paths"][OPERATIONS["run_statistics.series"].canonical_path]["get"]
    names = {parameter["name"]: parameter for parameter in document["parameters"] if parameter["in"] == "query"}
    assert names["signals"]["required"] and names["signals"]["schema"]["type"] == "array"
    assert names["top"]["schema"]["default"] == 4 and names["group_by"]["schema"]["default"] == "none"


def test_the_summary_may_carry_the_last_day_and_old_answers_still_parse():
    assert "runs_24h" in OperationsSummary.model_fields
    schema = export_schemas()["operations-summary"]
    assert "runs_24h" in schema["properties"] and "runs_24h" not in schema.get("required", [])
    assert RunsDaySection.model_fields.keys() == {"started", "succeeded", "failed", "failure_rate"}


def test_schemas_are_exported_for_every_answer():
    schemas = export_schemas()
    assert {
        "run-statistics-series",
        "run-statistics-workflows",
        "run-statistics-steps",
        "runs-day-section",
    } <= set(schemas)
    assert set(schemas["run-statistics-series"]["properties"]) == {
        "from",
        "to",
        "granularity",
        "complete_until",
        "series",
    }


@pytest.mark.parametrize("verb", ["series", "workflows", "steps"])
def test_the_cli_reads_statistics_under_operations(verb):
    from firefly_weave.cli.main import cli

    result = CliRunner().invoke(cli, ["operations", "statistics", verb, "--help"])
    assert result.exit_code == 0, result.output
    assert f"run_statistics.{verb}: status.read" in result.output


def test_studio_admits_the_statistics_family():
    from firefly_weave.studio.service import STUDIO_FAMILIES

    assert "run_statistics" in STUDIO_FAMILIES
