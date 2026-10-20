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
"""Weave AI attachments come from allowlisted builders: run facts, never inputs, outputs, logs or hosts."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.lumi import LumiAskRequest
from firefly_weave.contracts.run_views import RunStepQuery, RunSummary, StepFact, StepFactPage
from firefly_weave.operations.lumi import LumiService
from firefly_weave.operations.lumi_context import RUN_FIELDS, run_context, shape

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
RUN_ID = uuid4()


def summary():
    return RunSummary.model_validate(
        {
            "id": RUN_ID,
            "workflow": {"name": "support-inbox", "version": "1.2.0", "definition_version_id": uuid4()},
            "activation_id": uuid4(),
            "activation": {"name": "support-inbox", "revision": 3},
            "status": "failed",
            "paused": False,
            "test": False,
            "origin": None,
            "caller": None,
            "retried_from_run_id": None,
            "started_at": START,
            "updated_at": START + timedelta(seconds=9),
            "ended_at": START + timedelta(seconds=9),
            "duration_ms": 9000,
            "business_key": "customer-secret-canary",
            "correlation_key": None,
            "failed_step": {"node_id": "charge", "instance_key": "", "error_code": "PAYMENT_DECLINED"},
            "active_incidents": 1,
            "archived": False,
        }
    )


def step(node_id, minute, status="succeeded", error_code=None, kind="action"):
    at = START + timedelta(minutes=minute)
    return StepFact.model_validate(
        {
            "node_id": node_id,
            "instance_key": "",
            "iteration": [],
            "kind": kind,
            "status": status,
            "scheduled_at": at,
            "started_at": at,
            "ended_at": at,
            "duration_ms": 0,
            "queue_wait_ms": 0,
            "attempts": 1,
            "worker_id": uuid4(),
            "error_code": error_code,
            "child_run_id": None,
            "log_entries": 4,
        }
    )


STEPS = [step(f"step-{index}", index) for index in range(7)] + [step("charge", 8, "failed", "PAYMENT_DECLINED")]


def keys(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield prefix + "/" + key
            yield from keys(child, prefix + "/" + key)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child, prefix + "/*")


def test_a_run_attachment_keeps_only_the_allowlisted_fields():
    data = run_context(summary(), STEPS)
    assert data == {
        "workflow": {"name": "support-inbox", "version": "1.2.0"},
        "status": "failed",
        "started_at": "2026-10-20T09:00:00Z",
        "ended_at": "2026-10-20T09:00:09Z",
        "duration_ms": 9000,
        "active_incidents": 1,
        "failed_steps": [{"node_id": "charge", "instance_key": "", "error_code": "PAYMENT_DECLINED"}],
        "recent_steps": [
            {"node_id": "step-3", "kind": "action", "status": "succeeded", "at": "2026-10-20T09:03:00Z"},
            {"node_id": "step-4", "kind": "action", "status": "succeeded", "at": "2026-10-20T09:04:00Z"},
            {"node_id": "step-5", "kind": "action", "status": "succeeded", "at": "2026-10-20T09:05:00Z"},
            {"node_id": "step-6", "kind": "action", "status": "succeeded", "at": "2026-10-20T09:06:00Z"},
            {"node_id": "charge", "kind": "action", "status": "failed", "at": "2026-10-20T09:08:00Z"},
        ],
    }
    assert set(keys(data)) <= RUN_FIELDS


def test_the_spy_refuses_any_field_outside_the_allowlist():
    data = run_context(summary(), STEPS)
    assert "customer-secret-canary" not in json.dumps(data)
    assert not {"/worker_id", "/business_key", "/log_entries"} & set(keys(data))


async def test_asking_about_a_run_sends_its_facts_and_rechecks_access():
    actor = SimpleNamespace(id=uuid4())
    runs = SimpleNamespace(
        summary=AsyncMock(side_effect=[summary(), AccessDenied()]),
        steps=AsyncMock(return_value=StepFactPage(items=STEPS, complete=True)),
    )
    service = LumiService(SimpleNamespace(definitions=None), None, None, None, None, None, runs)
    request = LumiAskRequest(message="Why did it fail?", attachments=[{"kind": "run", "id": RUN_ID}])
    audit = AuditContext()
    resources = await service._attachments(actor, SCOPE, request, audit)
    assert resources == [{"kind": "run", "id": str(RUN_ID), "data": run_context(summary(), STEPS)}]
    runs.steps.assert_awaited_once_with(actor, SCOPE, RUN_ID, RunStepQuery(limit=200), None, context=audit)
    with pytest.raises(AccessDenied):
        await service._attachments(actor, SCOPE, request, AuditContext())


async def test_a_run_without_facts_yet_is_named_as_such():
    actor = SimpleNamespace(id=uuid4())
    runs = SimpleNamespace(summary=AsyncMock(return_value=None), steps=AsyncMock())
    service = LumiService(SimpleNamespace(definitions=None), None, None, None, None, None, runs)
    request = LumiAskRequest(message="Why?", attachments=[{"kind": "run", "id": RUN_ID}])
    resources = await service._attachments(actor, SCOPE, request, AuditContext())
    assert resources == [{"kind": "run", "id": str(RUN_ID), "data": {"status": "not_recorded_yet"}}]
    runs.steps.assert_not_awaited()


def test_a_compact_request_outlines_a_saved_draft():
    document = {
        "kind": "Workflow",
        "metadata": {"name": "n", "version": "1.0.0"},
        "spec": {"steps": [{"id": "a", "kind": "action", "uses": "x@1.0.0", "with": {"literal": "private"}}]},
    }
    saved = {"kind": "draft", "id": "d", "data": {"document": document, "revision": 4}}
    compact = shape(
        "compact", 4096, message="m", history=[], resources=[saved], draft=None, selected_step=None, schemas={}
    )
    assert compact.context["resources"][0]["data"] == {
        "outline": {
            "kind": "Workflow",
            "metadata": document["metadata"],
            "steps": [{"id": "a", "kind": "action", "uses": "x@1.0.0"}],
        },
        "revision": 4,
    }
    full = shape("full", None, message="m", history=[], resources=[saved], draft=None, selected_step=None, schemas={})
    assert full.context["resources"][0] == saved
