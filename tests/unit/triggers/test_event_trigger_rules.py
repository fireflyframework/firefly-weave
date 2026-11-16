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

"""Binding a workflow to platform events: what a binding may ask for, and where its commands and bridge reach."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner

from firefly_weave.cli.triggers import triggers
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.definitions import EventTriggerSpec
from firefly_weave.contracts.event_triggers import EventTriggerRequest
from firefly_weave.contracts.transaction_resolve import resolve_action_definition, resolve_task_capability
from firefly_weave.contracts.workers import ReleaseRequest, WorkerRelease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.studio.service import StudioOptions, StudioProfile, StudioService
from firefly_weave.triggers.event_triggers import binding_problem, effective_sources
from firefly_weave.workers.repository import WorkerRepository
from firefly_weave.workers.service import WorkerService

IN_DOUBT, COMPENSATED, COMPLETED = "transaction.in_doubt", "transaction.compensated", "transaction.completed"
# The workflow of the bound activation.
BOUND = "notify-team"


def declared(*events: str, sources: list[str] | None = None) -> EventTriggerSpec:
    value: dict = {"events": list(events)}
    if sources is not None:
        value["from"] = sources
    return EventTriggerSpec.model_validate_json(json.dumps(value))


def request(*events: str, sources: list[str] | None = None) -> EventTriggerRequest:
    value: dict = {"name": "notify", "activation_id": "5b0f3c0e-8f0e-4b8a-9c39-3c1f7c0b8a11", "events": list(events)}
    if sources is not None:
        value["source_workflows"] = sources
    return EventTriggerRequest.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    ("definition", "binding", "problem"),
    [
        (None, request(IN_DOUBT), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT), request(COMPLETED), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT), request(IN_DOUBT, COMPLETED), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT, sources=["move"]), request(IN_DOUBT, sources=["other"]), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT, sources=["move"]), request(IN_DOUBT, sources=["move", "other"]), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT, COMPENSATED), request(COMPENSATED), None),
        (declared(IN_DOUBT, sources=["move", "other"]), request(IN_DOUBT, sources=["other"]), None),
        (declared(IN_DOUBT), request(IN_DOUBT, sources=["any-workflow"]), None),
        (declared(IN_DOUBT), request(IN_DOUBT, sources=["move", BOUND]), "WV-TRIGGER-EVENTS"),
        (declared(IN_DOUBT, sources=["move", BOUND]), request(IN_DOUBT, sources=[BOUND]), None),
    ],
    ids=[
        "no declaration",
        "an undeclared event",
        "one undeclared event among declared ones",
        "a source outside from",
        "one source outside from",
        "a subset of the events",
        "a subset of the sources",
        "any source when from is omitted",
        "its own workflow when from is omitted",
        "its own workflow when from names it",
    ],
)
def test_a_binding_may_ask_only_for_what_the_workflow_declares(definition, binding, problem):
    assert binding_problem(definition, binding, BOUND) == problem


def test_a_bindings_sources_are_its_own_else_the_workflows_else_any():
    assert effective_sources(declared(IN_DOUBT, sources=["move"]), request(IN_DOUBT, sources=["move"])) == ["move"]
    assert effective_sources(declared(IN_DOUBT, sources=["move", "other"]), request(IN_DOUBT)) == ["move", "other"]
    assert effective_sources(declared(IN_DOUBT), request(IN_DOUBT)) is None


def test_the_cli_offers_platform_event_trigger_commands():
    result = CliRunner().invoke(triggers, ["events", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("create", "list", "read", "disable", "starts"):
        assert f"  {name} " in result.output


def test_the_studio_bridge_reaches_event_triggers(tmp_path: Path):
    tenant, project, environment = (f"00000000-0000-0000-0000-00000000000{n}" for n in (1, 2, 3))
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    profile = StudioProfile(
        name="Dev", base_url="https://api.example", tenant_id=tenant, project_id=project, environment_id=environment
    )
    service = StudioService(StudioOptions(origin="http://127.0.0.1:8766", assets=assets, profile=profile))
    path = f"/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/event-triggers"
    trigger = "00000000-0000-0000-0000-0000000000aa"
    assert service.match(path, "POST") == (200, "event_triggers.create")
    assert service.match(f"{path}/{trigger}/disable", "POST") == (200, "event_triggers.disable")
    assert service.match(f"{path}/{trigger}/starts", "GET") == (200, "event_triggers.starts")
    other = path.replace(environment, "00000000-0000-0000-0000-000000000009")
    assert service.match(other, "POST") == (403, "event_triggers.create")


def test_no_worker_release_may_offer_a_platform_task_type():
    registry = ConnectorRegistry()

    def release(capability: dict) -> ReleaseRequest:
        return ReleaseRequest.model_validate_json(
            json.dumps({"image_digest": "sha256:" + "a" * 64, "capabilities": [capability]})
        )

    platform = resolve_task_capability()
    for task_type in (platform["taskType"], "weave-platform.anything"):
        with pytest.raises(CatalogError) as refused:
            registry.validate_release(release({**platform, "taskType": task_type}))
        assert (refused.value.status, refused.value.code) == (409, "WV-CAPABILITY-CONFLICT")
    registry.validate_release(release({**platform, "taskType": "record-event"}))


def test_no_activation_may_pin_a_worker_release_for_a_platform_task_type(monkeypatch):
    """A release registered before the platform reserved its task types can still offer one: admission refuses the
    pin, so no run ever creates a pinned platform task."""
    capability = resolve_task_capability()
    reference = f"{capability['taskType']}@{capability['taskVersion']}"
    release = WorkerRelease.model_validate_json(
        json.dumps({"id": str(uuid4()), "image_digest": "sha256:" + "a" * 64, "capabilities": [capability]})
    )
    offered = CatalogSnapshot.from_definitions([], tasks=release.capabilities).resolve("TaskCapability", reference)
    assert offered is not None
    executable = {
        "graph": {"nodes": []},
        "dependencies": [
            {"kind": "Action", "reference": "weave.transaction-resolve@1.0.0", "document": resolve_action_definition()},
            {"kind": "TaskCapability", "reference": reference, "digest": offered.digest},
        ],
    }

    async def stored(_repository: WorkerRepository, _identifier: UUID) -> WorkerRelease:
        return release

    monkeypatch.setattr(WorkerRepository, "release", stored)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    request = ActivationRequest(
        version_id=uuid4(),
        artifact_digest="a" * 64,
        scope=scope,
        worker_release_ids={capability["taskType"]: release.id},
    )
    admission = WorkerService(MagicMock(), MagicMock()).admit(
        MagicMock(),
        scope,
        request,
        SimpleNamespace(executable=executable),
        capability="release.activate",
        context=MagicMock(),
        tx=MagicMock(),
    )
    with pytest.raises(CatalogError) as refused:
        asyncio.run(admission)
    # The platform-task refusal itself, not another readiness refusal on the way.
    assert (refused.value.status, refused.value.code, refused.value.message) == (
        422,
        "WV-READINESS",
        "Platform task types are never pinned to a worker release",
    )
