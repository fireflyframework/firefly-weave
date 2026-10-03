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

"""Deployment intent cannot become an arbitrary command or a second scheduler."""

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.deployments import ComponentSpec, TargetRequest


def component(**changes):
    return {
        "name": "api",
        "kind": "api",
        "image": "registry.example/weave@sha256:" + "a" * 64,
        "configuration": "production",
        **changes,
    }


def test_singleton_api_rejects_scale_and_mutable_artifacts():
    assert ComponentSpec.model_validate(component()).replicas == 1
    for changes in (
        {"replicas": 2},
        {"image": "registry.example/weave:latest"},
        {"command": ["sh", "-c", "anything"]},
        {"environment": {"TOKEN": "private"}},
    ):
        with pytest.raises(ValidationError):
            ComponentSpec.model_validate(component(**changes))


def test_target_never_accepts_cloud_credentials():
    from uuid import uuid4

    value = {
        "name": "local",
        "adapter": "docker-compose",
        "external_identity": "local-project",
        "boundary": "weave",
        "runner_principal_id": uuid4(),
        "capabilities": ["observe"],
    }
    assert TargetRequest.model_validate(value).adapter == "docker-compose"
    with pytest.raises(ValidationError):
        TargetRequest.model_validate({**value, "credentials": {"token": "private"}})


def test_observation_bounds_and_terminal_reports_require_actual_evidence():
    from uuid import uuid4

    from firefly_weave.contracts.deployments import (
        DeploymentLeaseProof,
        ObservationReport,
        ObservedResource,
        RunnerReport,
        SafeDeploymentReceipt,
    )

    proof = DeploymentLeaseProof(job_id=uuid4(), runner_id=uuid4(), generation=1, token=uuid4())
    with pytest.raises(ValidationError):
        RunnerReport(lease=proof, report_id=uuid4(), state="succeeded", receipt=SafeDeploymentReceipt(code="observed"))
    with pytest.raises(ValidationError):
        SafeDeploymentReceipt.model_validate({"code": "provider_failed", "logs": "secret output"})
    fact = ObservedResource(
        name="api",
        external_identity="api",
        kind="api",
        replicas=1,
        ready_replicas=1,
        version="1",
        ownership="managed",
        state="ready",
    )
    with pytest.raises(ValidationError):
        ObservationReport(resources=[fact, fact], complete=True)
    with pytest.raises(ValidationError):
        ObservationReport(resources=[fact.model_copy(update={"ready_replicas": 2})], complete=True)


def test_operations_schemas_and_proxy_keep_existing_host_scope(tmp_path):
    import httpx
    from test_studio_host import client, pair

    from firefly_weave.contracts.deployments import DeploymentRequest, TargetUpdate
    from firefly_weave.studio.service import StudioProfile

    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    profile = StudioProfile(
        name="Operations",
        base_url="https://api.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        environment_id="00000000-0000-0000-0000-000000000003",
    )
    with client(
        tmp_path, profile=profile, token_provider=lambda: "host-only-secret", transport=httpx.MockTransport(upstream)
    ) as browser:
        pair(browser)
        assert browser.get("/studio/contracts/deployment").json() == DeploymentRequest.model_json_schema()
        assert browser.get("/studio/contracts/deployment-target").json() == TargetRequest.model_json_schema()
        assert browser.get("/studio/contracts/deployment-target-update").json() == TargetUpdate.model_json_schema()
        prefix = (
            "/studio/api/api/v1/tenants/"
            + str(profile.tenant_id)
            + "/projects/"
            + str(profile.project_id)
            + "/environments/"
            + str(profile.environment_id)
        )
        for family in (
            "deployment-targets",
            "deployments",
            "deployment-plans",
            "deployment-observations",
            "deployment-jobs",
            "deployment-runners",
        ):
            assert browser.get(prefix + "/" + family).status_code == 200
        assert len(seen) == 6
        assert browser.get(prefix.replace("000000000003", "000000000009") + "/deployment-targets").status_code == 403
        assert browser.get(prefix + "/execute-arbitrary-command").status_code == 404
        assert len(seen) == 6


def test_ambiguous_receipt_cannot_unlock_target_as_an_ordinary_failure():
    from uuid import uuid4

    from firefly_weave.contracts.deployments import DeploymentLeaseProof, RunnerReport, SafeDeploymentReceipt

    proof = DeploymentLeaseProof(job_id=uuid4(), runner_id=uuid4(), generation=1, token=uuid4())
    with pytest.raises(ValidationError, match="Ambiguous"):
        RunnerReport(
            lease=proof,
            report_id=uuid4(),
            state="failed",
            receipt=SafeDeploymentReceipt(code="provider_failed", external_effects_may_continue=True),
        )
