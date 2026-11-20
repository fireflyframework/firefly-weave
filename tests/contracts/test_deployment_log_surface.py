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

"""Container log requests are published, documented and bounded: people ask with two capabilities, and only the
target's runner claims and reports them."""

import json
from functools import cache
from uuid import uuid4

import pytest

from firefly_weave.contracts.deployments import (
    LogClaim,
    LogReport,
    LogReportAccepted,
    LogRequestCreate,
    LogRequestView,
    RunnerClaimRequest,
    RunnerRegistration,
    TargetRequest,
)
from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.surface import OPERATIONS

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
spec = cache(export_openapi)
TARGET = str(uuid4())
ALL = ["observe", "deploy", "update", "scale_workers", "drain_workers", "restart_workers", "read_logs"]


def native(identifier: str) -> dict:
    operation = OPERATIONS[identifier]
    return spec()["paths"][operation.canonical_path][operation.method.lower()]


def wire(model, value: dict):
    # As the wire sends it: strict models take UUIDs and times as strings only in JSON.
    return model.model_validate_json(json.dumps(value))


def test_a_person_requests_container_logs_with_both_capabilities_and_an_idempotency_key():
    create, read = OPERATIONS["deployment_log_requests.create"], OPERATIONS["deployment_log_requests.read"]
    assert (create.method, create.canonical_path) == ("POST", ENVIRONMENT + "/deployment-log-requests")
    assert (create.request, create.response, create.statuses, create.idempotency) == (
        LogRequestCreate,
        LogRequestView,
        (201,),
        True,
    )
    assert (read.method, read.canonical_path) == ("GET", ENVIRONMENT + "/deployment-log-requests/{identifier}")
    assert read.response is LogRequestView and read.request is None
    for operation in (create, read):
        assert operation.capability == "deployment_log.read and deployment.read"
    parameters = native("deployment_log_requests.create")["parameters"]
    assert "Idempotency-Key" in [item["name"] for item in parameters if item["in"] == "header"]
    description = native("deployment_log_requests.create")["description"]
    for refusal in ("409 WV-RUNNER-OFFLINE", "429 WV-LOG-RATE", "422 WV-DEPLOYMENT-UNSUPPORTED"):
        assert refusal in description


def test_the_runner_claims_and_reports_log_requests_beside_its_jobs():
    claim, report = OPERATIONS["deployment_runners.claim_logs"], OPERATIONS["deployment_runners.report_logs"]
    assert (claim.method, claim.canonical_path, claim.capability) == (
        "POST",
        ENVIRONMENT + "/deployment-runners/claim-logs",
        "runner.claim",
    )
    assert (claim.request, claim.response) == (RunnerClaimRequest, LogClaim | None)
    assert (report.method, report.canonical_path, report.capability) == (
        "POST",
        ENVIRONMENT + "/deployment-runners/report-logs",
        "runner.report",
    )
    assert (report.request, report.response) == (LogReport, LogReportAccepted)


REQUEST = {"target_id": TARGET, "component": "fixture-worker", "since_seconds": 900, "tail_lines": 200}


def test_a_request_reads_up_to_five_members_from_one_minute_to_a_day_and_up_to_a_thousand_lines():
    assert wire(LogRequestCreate, REQUEST).members == []
    wire(LogRequestCreate, {**REQUEST, "members": [f"weave-worker-{n}" for n in range(5)]})
    wire(LogRequestCreate, {**REQUEST, "since_seconds": 60, "tail_lines": 1})
    wire(LogRequestCreate, {**REQUEST, "since_seconds": 86400, "tail_lines": 1000})
    for refused in (
        {"members": [f"weave-worker-{n}" for n in range(6)]},
        {"members": ["weave-worker-1", "weave-worker-1"]},
        {"members": ["-leading-dash"]},
        {"members": ["a/b"]},
        {"since_seconds": 59},
        {"since_seconds": 86401},
        {"tail_lines": 0},
        {"tail_lines": 1001},
        {"component": "../etc"},
        {"component": "worker --since=1s"},
        {"command": "logs"},
    ):
        with pytest.raises(ValueError):
            wire(LogRequestCreate, {**REQUEST, **refused})


def test_targets_and_runners_may_hold_read_logs_beside_every_other_capability():
    target = {
        "name": "local-docker",
        "adapter": "docker-compose",
        "external_identity": "engine",
        "boundary": "weave-workers",
        "runner_principal_id": str(uuid4()),
        "capabilities": ALL,
    }
    assert "read_logs" in wire(TargetRequest, target).capabilities
    registration = {"target_id": TARGET, "adapter": "kubernetes", "capabilities": ALL}
    assert "read_logs" in wire(RunnerRegistration, registration).capabilities


LINE = {"at": "2026-11-19T10:41:52.123456Z", "stream": "stdout", "text": "Worker ready"}
PROOF = {"request_id": str(uuid4()), "runner_id": str(uuid4()), "token": str(uuid4())}


def test_a_report_holds_up_to_five_members_of_short_lines_or_says_why_it_has_none():
    members = [{"member": f"weave-worker-{n}", "lines": [LINE]} for n in range(5)]
    assert len(wire(LogReport, {"proof": PROOF, "members": members}).members) == 5
    assert wire(LogReport, {"proof": PROOF, "failure": "unknown_component"}).members == []
    for line in ({**LINE, "text": "x" * 2049}, {**LINE, "stream": "stdin"}, {**LINE, "at": "10:41:52"}):
        with pytest.raises(ValueError):
            wire(LogReport, {"proof": PROOF, "members": [{"member": "weave-worker-1", "lines": [line]}]})
    for refused in (
        {"members": [*members, {"member": "weave-worker-9", "lines": []}]},
        {"members": [{"member": "weave-worker-1", "lines": [LINE] * 1001}]},
        {"members": members[:1], "failure": "unreadable"},
        {"members": [members[0], members[0]]},
    ):
        with pytest.raises(ValueError):
            wire(LogReport, {"proof": PROOF, **refused})


def test_a_view_says_its_state_and_when_it_expires():
    view = {
        "id": str(uuid4()),
        "target_id": TARGET,
        "component": "fixture-worker",
        "requested_members": [],
        "since_seconds": 900,
        "tail_lines": 200,
        "state": "pending",
        "created_at": "2026-11-19T10:54:00Z",
        "expires_at": "2026-11-19T11:54:00Z",
    }
    pending = wire(LogRequestView, view)
    assert (pending.members, pending.truncated, pending.read_at, pending.failure) == ([], False, None, None)
    for state in ("ready", "failed", "expired"):
        wire(LogRequestView, {**view, "state": state})
    with pytest.raises(ValueError):
        wire(LogRequestView, {**view, "state": "claimed"})
