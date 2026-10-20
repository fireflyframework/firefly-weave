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

"""Change requests, restarts and approval policies keep stored plans and targets byte-stable."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    ChangeRequestApply,
    ChangeRequestCreate,
    ComponentSpec,
    DeploymentPlan,
    DeploymentTarget,
    PlanRequest,
    PlanStep,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy

IMAGE = "registry.example/worker@sha256:" + "a" * 64
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def worker(**changes):
    return ComponentSpec(
        name="fixture-worker",
        kind="worker",
        image=IMAGE,
        configuration="current",
        replicas=3,
        worker_release_id=UUID(int=7),
        **changes,
    )


def legacy_plan_document() -> dict:
    """A plan exactly as servers before change requests stored it."""
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return {
        "id": str(UUID(int=1)),
        "scope": SCOPE.model_dump(mode="json"),
        "target_id": str(UUID(int=2)),
        "target_revision": 1,
        "deployment_id": str(UUID(int=3)),
        "deployment_revision": 1,
        "adapter": "docker-compose",
        "adapter_version": "1",
        "intent": "scale_workers",
        "observation_id": str(UUID(int=4)),
        "observation_digest": "b" * 64,
        "steps": [
            {
                "action": "scale_workers",
                "component": worker().model_dump(mode="json"),
                "expected_version": "c" * 64,
            }
        ],
        "risks": ["external_effects"],
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "digest": "0" * 64,
    }


def test_a_stored_plan_keeps_its_bytes_and_digest():
    document = legacy_plan_document()
    plan = DeploymentPlan.model_validate_json(json.dumps(document))
    assert canonical_digest(plan.model_dump(mode="json")) == canonical_digest(document)


def test_a_plan_document_never_carries_its_planner_or_the_members_it_removes():
    # Runners and clients of earlier releases forbid unknown fields: both are kept beside the plan instead.
    for field, value in (("planned_by", str(uuid4())),):
        document = {**legacy_plan_document(), field: value}
        with pytest.raises(ValidationError):
            DeploymentPlan.model_validate_json(json.dumps(document))
    document = legacy_plan_document()
    document["steps"][0]["remove_members"] = ["0123456789ab"]
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate_json(json.dumps(document))


def test_restart_is_an_intent_a_step_and_a_capability():
    PlanRequest(
        deployment_id=uuid4(), deployment_revision=1, observation_id=uuid4(), intent="restart_workers", ttl_seconds=900
    )
    assert PlanStep(action="restart_workers", component=worker()).action == "restart_workers"
    target = TargetRequest(
        name="local-docker",
        adapter="docker-compose",
        external_identity="daemon-id",
        boundary="weave-local-workers",
        runner_principal_id=uuid4(),
        capabilities=["observe", "deploy", "update", "scale_workers", "restart_workers", "drain_workers"],
    )
    assert "restart_workers" in target.capabilities


def test_an_approval_policy_is_left_out_at_its_default_and_kept_when_it_is_off():
    base = {
        "name": "local-docker",
        "adapter": "docker-compose",
        "external_identity": "daemon-id",
        "boundary": "weave-local-workers",
        "runner_principal_id": str(uuid4()),
        "capabilities": ["observe"],
    }

    def target(value):
        return TargetRequest.model_validate_json(json.dumps(value))

    assert "approval_policy" not in target(base).model_dump(mode="json")
    strict = target({**base, "approval_policy": {"distinct_approver": True}})
    assert strict.approval_policy.distinct_approver and "approval_policy" not in strict.model_dump(mode="json")
    local = target({**base, "approval_policy": {"distinct_approver": False}})
    assert local.model_dump(mode="json")["approval_policy"] == {"distinct_approver": False}
    stored = {**base, "id": str(uuid4()), "scope": SCOPE.model_dump(mode="json"), "revision": 1, "disabled": False}
    stored["created_at"] = "2026-10-01T00:00:00Z"
    legacy = DeploymentTarget.model_validate_json(json.dumps(stored))
    # A target registered before approval policies existed requires a second approver.
    assert legacy.approval_policy.distinct_approver
    assert legacy.model_dump(mode="json") == stored
    update = TargetUpdate(runner_principal_id=uuid4(), capabilities=["observe"])
    assert update.approval_policy is None and "approval_policy" not in update.model_dump(mode="json")


def change(**changes):
    value = {
        "target_id": str(uuid4()),
        "deployment_id": str(uuid4()),
        "expected_deployment_revision": 1,
        "intent": "scale_workers",
        "components": ["fixture-worker"],
        "desired": [{"name": "fixture-worker", "replicas": 1}],
        "title": "Scale fixture-worker to 1",
        "reason": "",
        **changes,
    }
    return ChangeRequestCreate.model_validate_json(json.dumps(value))


def test_a_change_request_is_bounded_and_carries_no_free_form_execution():
    request = change()
    assert request.drain.mode == "none" and request.drain.timeout_seconds == 300
    assert request.plan_ttl_seconds is None
    for bad in (
        {"title": ""},
        {"title": "x" * 121},
        {"title": "line\nbreak"},
        {"reason": "x" * 2001},
        {"components": []},
        {"components": ["fixture-worker", "fixture-worker"]},
        {"desired": [{"name": "other", "replicas": 1}]},
        {"desired": [{"name": "fixture-worker", "command": ["sh"]}]},
        {"drain": {"mode": "drain_first", "timeout_seconds": 59}},
        {"drain": {"mode": "drain_first", "timeout_seconds": 601}},
        {"plan_ttl_seconds": 29},
        {"plan_ttl_seconds": 901},
        {"intent": "drain_workers"},
    ):
        with pytest.raises(ValidationError):
            change(**bad)
    restart = change(intent="restart_workers", desired=None, drain={"mode": "drain_first", "timeout_seconds": 600})
    assert restart.drain.timeout_seconds == 600


def test_forcing_an_apply_after_a_drain_timeout_needs_a_reason():
    assert ChangeRequestApply(digest="a" * 64).force_after_timeout is False
    with pytest.raises(ValidationError):
        ChangeRequestApply(digest="a" * 64, force_after_timeout=True)
    with pytest.raises(ValidationError):
        ChangeRequestApply(digest="a" * 64, force_after_timeout=True, force_reason=" ")
    forced = ChangeRequestApply(digest="a" * 64, force_after_timeout=True, force_reason="Worker is stuck")
    assert forced.force_reason == "Worker is stuck"


def test_the_compose_runner_may_restart_workers(tmp_path):
    policy = DestinationPolicy(
        target_id=uuid4(),
        adapter="docker-compose",
        external_identity="daemon-id",
        boundary="weave-local-workers",
        context="weave-runner",
        executable="/usr/bin/docker",
        capabilities=["observe", "deploy", "update", "scale_workers", "restart_workers"],
        compose_file=str(tmp_path / "compose.json"),
        lock_file=str(tmp_path / "runner.lock"),
        components=[{"name": "fixture-worker", "kind": "worker", "configuration": "current"}],
        image_repositories=["127.0.0.1:5000/example-sleep"],
    )
    assert "restart_workers" in policy.capabilities
    with pytest.raises(ValidationError):
        DestinationPolicy.model_validate(
            {
                **policy.model_dump(mode="json"),
                "adapter": "azure-container-apps",
                "capabilities": ["observe", "restart_workers"],
            }
        )
