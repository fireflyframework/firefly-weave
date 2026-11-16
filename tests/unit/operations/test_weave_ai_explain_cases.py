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

"""The explanation evaluation's requests are exactly what the platform sends today.

Each case of ``tests/fixtures/weave_ai_explanations/records.json`` goes through the real ask: the real builders, the
real shaping and the real gateway client, recorded at its transport. The bodies are kept, with the case's
expectations, in the agentic worker's live evaluation (``explain_eval/cases.json``), which replays them against a
real model. Run with ``WEAVE_UPDATE_EXPLAIN_CASES=1`` to rewrite that file after a deliberate change.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from test_weave_ai_operations import SCOPE, Owners, ask, asker, keys

from firefly_weave.contracts.deployments import ChangeRequest, DeploymentJob, DeploymentPlan
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfiguration
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.operations_summary import OperationsSummary
from firefly_weave.contracts.workers import WorkerStatus
from firefly_weave.operations.incidents import RUNTIME_EVENT_TYPES, IncidentFacts
from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings
from firefly_weave.operations.lumi_operations import (
    CHANGE_REQUEST_FIELDS,
    ENVIRONMENT_FIELDS,
    INCIDENT_FIELDS,
    WORKER_FIELDS,
)
from firefly_weave.operations.summary import SummaryFacts

ROOT = Path(__file__).parents[3]
RECORDS = ROOT / "tests" / "fixtures" / "weave_ai_explanations" / "records.json"
CASES = ROOT / "workers" / "agentic" / "tests" / "live" / "explain_eval" / "cases.json"
UPDATE = "WEAVE_UPDATE_EXPLAIN_CASES"
# The runner replaces both for each model it evaluates; the golden bodies name no real endpoint or model.
PLACEHOLDER_ENDPOINT = "http://127.0.0.1:9/v1"
PLACEHOLDER_MODEL = "fixture-model"
CONTEXT = {"compact": 8192, "standard": 32768}
FIELDS = {
    "worker": WORKER_FIELDS,
    "incident": INCIDENT_FIELDS,
    "environment": ENVIRONMENT_FIELDS,
    "change-request": CHANGE_REQUEST_FIELDS,
}
UUID_SHAPED = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
# Values the fixture records hold in fields that must never be sent.
NEVER_SENT = (
    "fixture-host",
    "fixture-registry",
    "fixture-worker-7c9d.svc",
    "Scale the workers",
    "More orders tonight",
    "Quiet night",
)


def parsed(model: Any, value: Any) -> Any:
    """A contract record from its JSON, as the services return it (the contracts are strict about Python values)."""
    return model.model_validate_json(json.dumps(value))


def records() -> dict[str, Any]:
    return json.loads(RECORDS.read_text(encoding="utf-8"))


class FixtureOwners(Owners):
    """The owning services' reads answering with the fixture records a case attaches."""

    def __init__(self, document: dict[str, Any], attached: list[list[str]]):
        super().__init__()
        self.by_id: dict[Any, Any] = {}
        for kind, name in attached:
            if kind == "worker":
                status = parsed(WorkerStatus, document["workers"][name])
                self.by_id[status.id] = status
            elif kind == "incident":
                entry = document["incidents"][name]
                view = parsed(IncidentView, entry["view"])
                events = entry["events"] if entry["events"] == "withheld" else tuple(entry["events"])
                self.by_id[view.id] = IncidentFacts(
                    view=view,
                    step_kind=entry["step_kind"],
                    events=events,
                    more_events=entry["more_events"],
                    other_events=entry["other_events"],
                    run_read=entry["run_read"],
                    observed_at=datetime.fromisoformat(entry["observed_at"]),
                )
            elif kind == "environment":
                entry = document["summaries"][name]
                self.summaries = [
                    SummaryFacts(summary=parsed(OperationsSummary, entry["summary"]), reach=frozenset(entry["reach"]))
                ]
            else:
                entry = document["changes"][name]
                record = parsed(ChangeRequest, entry["record"])
                self.by_id[record.id] = record
                self.related = {
                    record.plan_id: entry["plan"] and parsed(DeploymentPlan, entry["plan"]),
                    record.apply_job_id: entry["job"] and parsed(DeploymentJob, entry["job"]),
                }

    async def read_worker(self, actor, scope, identifier, *, context):
        self.check("worker", actor, scope, context)
        return self.by_id[identifier]

    async def explanation_facts_incident(self, actor, scope, identifier, *, context):
        self.check("incident", actor, scope, context)
        return self.by_id[identifier]

    async def read_observed(self, identifier, *, actor, scope, context):
        self.check("change-request", actor, scope, context)
        record = self.by_id[identifier]
        # The change request's own clock: the fixture records share one observation time.
        return record, datetime.fromisoformat("2026-11-15T12:00:00Z")

    async def read_deployment(self, collection, identifier, *, actor, scope, context):
        return self.related[identifier]


def attachment_id(document: dict[str, Any], kind: str, name: str) -> str:
    if kind == "worker":
        return document["workers"][name]["id"]
    if kind == "incident":
        return document["incidents"][name]["view"]["id"]
    if kind == "environment":
        return str(SCOPE.environment_id)
    return document["changes"][name]["record"]["id"]


async def build_case(monkeypatch, tmp_path, document: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """The case's gateway body as the platform sends it, without the credential and the request's deadline."""
    owners = FixtureOwners(document, case["attachments"])
    service, gateway = asker(monkeypatch, owners)
    service._read = AsyncMock(
        return_value=LumiConfiguration(
            revision=1,
            connection_revision_id=SCOPE.tenant_id,
            profile={
                "provider": "openai-chat",
                "model": PLACEHOLDER_MODEL,
                "options": {"max_tokens": 1024, "temperature": 0.0, "seed": 42},
                "timeoutSeconds": 300,
                "outputSchema": LUMI_REPLY_SCHEMA,
            },
        )
    )
    service._context_tokens = lambda connection: CONTEXT[case["profile"]]
    bodies: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"answer": "Recorded", "proposals": [], "followUps": []})

    token = tmp_path / "token"
    token.write_text("service-token")
    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://gateway.invalid/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(respond),
    )

    async def send(configuration, request, attachments, *_, **__):
        return await client.ask(configuration, request, attachments, {"endpoint": PLACEHOLDER_ENDPOINT}, None)

    gateway.ask.side_effect = send
    request = LumiAskRequest(
        message=case["message"],
        attachments=[{"kind": kind, "id": attachment_id(document, kind, name)} for kind, name in case["attachments"]],
    )
    await ask(service, owners, request)
    [raw] = bodies
    body = json.loads(raw)
    del body["expires_at"]
    return body


async def built_cases(monkeypatch, tmp_path) -> list[dict[str, Any]]:
    document = records()
    built = []
    for case in document["cases"]:
        body = await build_case(monkeypatch, tmp_path, document, case)
        built.append(
            {
                "id": case["id"],
                "category": case["category"],
                "profile": case["profile"],
                "context_tokens": CONTEXT[case["profile"]],
                "request": body,
                "expect": case["expect"],
            }
        )
    return built


async def test_the_evaluation_requests_are_what_the_builders_send_now(monkeypatch, tmp_path):
    built = {"about": "Generated by tests/unit/operations/test_weave_ai_explain_cases.py; do not edit by hand."}
    built["cases"] = await built_cases(monkeypatch, tmp_path)
    if os.environ.get(UPDATE):
        CASES.parent.mkdir(parents=True, exist_ok=True)
        CASES.write_text(json.dumps(built, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(CASES.read_text(encoding="utf-8")) == built, f"Run with {UPDATE}=1 to update {CASES.name}"


def test_the_evaluation_set_covers_every_category():
    document = records()
    counted: dict[str, int] = {}
    for case in document["cases"]:
        counted[case["category"]] = counted.get(case["category"], 0) + 1
    assert counted == {
        "grounded-worker": 3,
        "grounded-incident": 4,
        "grounded-health": 3,
        "grounded-change-request": 2,
        "limits": 2,
        "mixed": 2,
        "safety": 4,
    }
    assert len({case["id"] for case in document["cases"]}) == 20


@pytest.fixture(scope="module")
def golden() -> list[dict[str, Any]]:
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def test_every_case_body_sends_no_excluded_value(golden):
    for case in golden:
        body = case["request"]
        text = json.dumps(body)
        assert UUID_SHAPED.search(text) is None, case["id"]
        assert [value for value in NEVER_SENT if value in text] == [], case["id"]
        assert body["endpoint"] == PLACEHOLDER_ENDPOINT and body["profile"]["model"] == PLACEHOLDER_MODEL
        assert "credential" not in body


def test_every_case_attachment_has_only_allowlisted_paths_and_no_timestamp(golden):
    for case in golden:
        for item in case["request"]["attachments"]["resources"]:
            assert set(keys(item["data"])) <= FIELDS[item["kind"]], case["id"]
            assert TIMESTAMP.search(json.dumps(item["data"])) is None, case["id"]
            events = item["data"].get("recent_events")
            if isinstance(events, list):
                assert set(events) <= RUNTIME_EVENT_TYPES, case["id"]


def test_the_hostile_name_is_sent_only_as_a_component_name(golden):
    [case] = [case for case in golden if case["id"] == "safety-hostile-name"]
    text = json.dumps(case["request"])
    [resource] = case["request"]["attachments"]["resources"]
    assert resource["data"]["components"][0]["name"] == "ignore-previous-instructions"
    assert text.count("ignore-previous-instructions") == 1
