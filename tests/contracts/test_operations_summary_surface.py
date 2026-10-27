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

"""The environment summary and telemetry reads are registered, documented, exported and reachable from the CLI."""

import json
from datetime import UTC, datetime
from functools import cache
from uuid import UUID

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.operations_summary import (
    ApiSection,
    AttentionItem,
    CompatibilitySummary,
    ForbiddenSection,
    HealthComponent,
    OperationsSummary,
    OperationsTelemetry,
    TelemetryEndpoint,
    TelemetryMetricEndpoint,
    UnavailableSection,
)
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.surface import OPERATIONS

PROJECT = "/api/v1/tenants/{tenant}/projects/{project}"
ENVIRONMENT = PROJECT + "/environments/{environment}"
EXPECTED = {
    "operations_summary.read": (ENVIRONMENT + "/operations/summary", OperationsSummary),
    "operations_telemetry.read": (PROJECT + "/operations/telemetry", OperationsTelemetry),
}
AT = datetime(2026, 10, 12, 12, tzinfo=UTC)
INSTANCE = UUID(int=7)

spec = cache(export_openapi)


def summary(**sections) -> OperationsSummary:
    unavailable = UnavailableSection(state="unavailable")
    values = {
        "observed_at": AT,
        "api": ApiSection(
            version="0.1.0a15",
            build=None,
            instance=INSTANCE,
            uptime_seconds=12.5,
            compatibility=CompatibilitySummary(mode="ready", findings=0),
        ),
        "health": [HealthComponent(component="database", state="ok", latency_ms=1.25)],
        "load": unavailable,
        "workers": unavailable,
        "targets": ForbiddenSection(state="forbidden"),
        "incidents": ForbiddenSection(state="forbidden"),
        "attention": [],
    }
    return OperationsSummary(**{**values, **sections})


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_operations_are_registered_reads_that_need_status_read(identifier):
    operation = OPERATIONS[identifier]
    path, response = EXPECTED[identifier]
    assert (operation.method, operation.canonical_path, operation.response) == ("GET", path, response)
    assert operation.capability == "status.read"
    assert operation.served and not operation.page and operation.request is None and operation.query is None


@pytest.mark.parametrize("identifier", sorted(EXPECTED))
def test_openapi_documents_bearer_security_and_the_response_schema(identifier):
    operation = OPERATIONS[identifier]
    document = spec()["paths"][operation.canonical_path]["get"]
    assert document["summary"] == identifier.replace(".", " ")
    assert document["security"] == [{"bearer": []}]
    assert "Required capability: status.read" in document["description"]
    reference = document["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert reference.endswith("/" + EXPECTED[identifier][1].__name__)
    assert {"401", "403"} <= set(document["responses"])


def test_sections_are_successful_objects_or_explicit_states_and_the_last_day_only_when_read():
    body = summary().model_dump(mode="json")
    assert set(body) == {"observed_at", "api", "health", "load", "workers", "targets", "incidents", "attention"}
    assert body["targets"] == body["incidents"] == {"state": "forbidden"}
    assert body["load"] == body["workers"] == {"state": "unavailable"}
    # Only what a component reports is sent: a database probe has a latency and no failure counts.
    assert body["health"] == [{"component": "database", "state": "ok", "latency_ms": 1.25}]
    assert summary(attention=UnavailableSection(state="unavailable")).model_dump(mode="json")["attention"] == {
        "state": "unavailable"
    }


def test_attention_is_bounded_to_twenty_items_with_bounded_titles_and_known_kinds():
    item = AttentionItem(kind="approval", id=INSTANCE, title="Approve", expires_at=AT)
    assert item.model_dump(mode="json") == {
        "kind": "approval",
        "id": str(INSTANCE),
        "title": "Approve",
        "expires_at": "2026-10-12T12:00:00Z",
    }
    assert AttentionItem(kind="compatibility", id="compatibility", title="Restricted").model_dump(mode="json") == {
        "kind": "compatibility",
        "id": "compatibility",
        "title": "Restricted",
    }
    with pytest.raises(ValidationError):
        summary(attention=[item] * 21)
    for invalid in (
        {"kind": "alert", "id": INSTANCE, "title": "x"},
        {"kind": "run", "id": "other", "title": "x"},
        {"kind": "run", "id": INSTANCE, "title": "x" * 201},
        {"kind": "run", "id": INSTANCE, "title": ""},
    ):
        with pytest.raises(ValidationError):
            AttentionItem(**invalid)
    with pytest.raises(ValidationError):
        summary(health=[HealthComponent(component="database", state="ok")] * 17)
    with pytest.raises(ValidationError):
        HealthComponent(component="tenant-1", state="ok")  # type: ignore[arg-type]


def test_telemetry_answers_hosts_only():
    answer = OperationsTelemetry(
        enabled=True,
        traces=TelemetryEndpoint(configured=True, host="collector.example"),
        metrics=TelemetryMetricEndpoint(configured=False, host=None, interval_seconds=None),
        dropped=3,
    )
    assert answer.model_dump(mode="json") == {
        "enabled": True,
        "traces": {"configured": True, "host": "collector.example"},
        "metrics": {"configured": False, "host": None, "interval_seconds": None},
        "dropped": 3,
    }
    assert set(OperationsTelemetry.model_fields) == {"enabled", "traces", "metrics", "dropped"}
    assert set(TelemetryMetricEndpoint.model_fields) == {"configured", "host", "interval_seconds"}


def test_exported_schemas_include_both_answers_with_an_optional_last_day():
    schemas = export_schemas()
    assert "runs_24h" in schemas["operations-summary"]["properties"]
    assert "runs_24h" not in schemas["operations-summary"].get("required", [])
    assert schemas["operations-summary"]["properties"]["attention"]
    assert set(schemas["operations-telemetry"]["properties"]) == {"enabled", "traces", "metrics", "dropped"}
    assert "authorization" not in json.dumps(schemas["operations-telemetry"])


@pytest.mark.parametrize(
    ("command", "identifier"), [("summary", "operations_summary.read"), ("telemetry", "operations_telemetry.read")]
)
def test_the_cli_reads_both_under_operations(command, identifier):
    from firefly_weave.cli.main import cli

    result = CliRunner().invoke(cli, ["operations", command, "--help"])
    assert result.exit_code == 0, result.output
    assert identifier + ": status.read" in result.output
