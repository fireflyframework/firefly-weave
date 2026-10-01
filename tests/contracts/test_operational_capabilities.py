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

"""Operational declarations stay identical across controller, schema, SDK, and CLI."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from starlette.requests import Request

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operational_policy import OperationsPolicy
from firefly_weave.contracts.public import Capabilities


def controller_fixture(compatibility=None):
    from pyfly.container import Container

    from firefly_weave.api.compiler import CompilerController
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.settings import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:never-expose-this@localhost/test",
        secret_root="/private/never-expose-this",
        operations=OperationsPolicy(runs_active=7, debug_rows=13, outbox_pending=19),
    )
    service = SimpleNamespace(catalog=AsyncMock(), capabilities=SimpleNamespace(resources={}))
    graph = Container()
    graph.register_instance(Settings, settings)
    graph.register_instance(DefinitionService, service)
    graph.register(CompilerController)
    controller = graph.resolve(CompilerController)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    state = SimpleNamespace()
    if compatibility is not None:
        state.compatibility = compatibility
    request = Request(
        {
            "type": "http",
            "path_params": {"tenant": str(scope.tenant_id), "project": str(scope.project_id)},
            "state": {"principal": object(), "audit_context": object()},
            "app": SimpleNamespace(state=state),
        }
    )
    return controller, request, service, settings, scope


def ready_state():
    class Report:
        mode = "ready"
        complete = True
        checked_at = datetime(2026, 9, 30, tzinfo=UTC)

        @property
        def findings(self):
            raise AssertionError("Global foreign findings must never be read")

        @property
        def inspected(self):
            raise AssertionError("Global inventory counts must never be read")

    return SimpleNamespace(ready=True, report=Report())


async def test_native_injection_declares_actual_policy_and_enforcement_limits():
    from firefly_weave.connections.secret_execution import SECRET_SLOTS
    from firefly_weave.operations.compatibility_catalog import WORKER_PROTOCOL
    from firefly_weave.operations.debug.models import (
        DEBUG_SESSION_RESERVATION_BYTES,
        DEBUG_SESSIONS_PER_CREATOR,
        DEBUG_SESSIONS_PER_PROJECT,
        DebugLimits,
    )
    from firefly_weave.operations.execution import CONTROL_SLOTS, DEBUG_EXECUTION_SLOTS, WORK_SLOTS
    from firefly_weave.operations.transport import (
        BODY_BYTES,
        BODY_SLOTS,
        CONTROL_BODY_BYTES,
        CONTROL_BODY_SLOTS,
        DEBUG_SLOTS,
        TransportPolicy,
    )
    from firefly_weave.runtime.capacity import MAX_INTENTS, MAX_REDUCTIONS, STATE_BYTES, TRANSITION_BYTES

    controller, request, service, settings, scope = controller_fixture(ready_state())
    result = await controller.capabilities(request)
    service.catalog.assert_awaited_once_with(request.state.principal, scope, context=request.state.audit_context)
    assert controller.settings is settings
    declaration = result["operations"]
    assert declaration["policy"] == settings.operations.model_dump()
    assert declaration["policy_fingerprint"] == settings.operations.fingerprint != OperationsPolicy().fingerprint
    assert declaration["worker_protocol_versions"] == [WORKER_PROTOCOL]
    assert declaration["transport"] == asdict(TransportPolicy())
    assert declaration["runtime"] == {
        "state_bytes": STATE_BYTES,
        "transition_bytes": TRANSITION_BYTES,
        "reductions_per_turn": MAX_REDUCTIONS,
        "intents_per_turn": MAX_INTENTS,
    }
    assert declaration["process"] == {
        "work_slots": WORK_SLOTS,
        "control_slots": CONTROL_SLOTS,
        "secret_slots": SECRET_SLOTS,
        "body_slots": BODY_SLOTS,
        "body_bytes": BODY_BYTES,
        "control_body_slots": CONTROL_BODY_SLOTS,
        "control_body_bytes": CONTROL_BODY_BYTES,
        "debug_body_slots": DEBUG_SLOTS,
        "debug_execution_slots": DEBUG_EXECUTION_SLOTS,
    }
    assert declaration["debug"] == {
        "session_bytes": DebugLimits().session_bytes,
        "lifetime_seconds": DebugLimits().lifetime_seconds,
        "sessions_per_creator": DEBUG_SESSIONS_PER_CREATOR,
        "sessions_per_project": DEBUG_SESSIONS_PER_PROJECT,
        "session_reservation_bytes": DEBUG_SESSION_RESERVATION_BYTES,
    }
    assert declaration["compatibility"] == {
        "mode": "ready",
        "ready": True,
        "complete": True,
        "checked_at": "2026-09-30T00:00:00Z",
    }
    assert "never-expose-this" not in json.dumps(result)
    assert set(declaration["compatibility"]) == {"mode", "ready", "complete", "checked_at"}


@pytest.mark.parametrize(
    "state",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(ready=True, report=None),
        SimpleNamespace(ready=True, report=SimpleNamespace(mode="future", complete=True, checked_at=None)),
        SimpleNamespace(ready=True, report=SimpleNamespace(mode="ready", complete=False, checked_at=None)),
        SimpleNamespace(ready=False, report=SimpleNamespace(mode="ready", complete=True, checked_at=None)),
        SimpleNamespace(ready="true", report=SimpleNamespace(mode="ready", complete=True, checked_at=None)),
        SimpleNamespace(ready=True, report=SimpleNamespace(mode="ready", complete=True, checked_at="not-a-date")),
    ],
)
async def test_missing_or_incoherent_compatibility_never_claims_readiness(state):
    controller, request, *_ = controller_fixture(state)
    result = await controller.capabilities(request)
    assert result["operations"]["compatibility"] == {
        "mode": "restricted",
        "ready": False,
        "complete": False,
        "checked_at": None,
    }


async def test_completed_restricted_report_preserves_safe_provenance():
    state = ready_state()
    state.ready = False
    state.report.mode = "restricted"
    controller, request, *_ = controller_fixture(state)
    result = (await controller.capabilities(request))["operations"]["compatibility"]
    assert result == {"ready": False, "mode": "restricted", "complete": True, "checked_at": "2026-09-30T00:00:00Z"}


async def test_authority_is_checked_before_operational_state_is_read():
    class Forbidden:
        @property
        def ready(self):
            raise AssertionError("operational state read before authority")

    controller, request, service, *_ = controller_fixture(Forbidden())
    service.catalog.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        await controller.capabilities(request)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["operations"].update(policy_fingerprint="0" * 64),
        lambda d: d["operations"]["policy"].update(runs_active=1001),
        lambda d: d["operations"]["policy"].update(runs_active=0),
        lambda d: d["operations"]["runtime"].update(state_bytes="33554432"),
        lambda d: d["operations"]["transport"].update(idle_seconds=float("inf")),
        lambda d: d["operations"]["process"].update(work_slots=0),
        lambda d: d["operations"]["debug"].update(sessions_per_project=-1),
        lambda d: d["operations"].update(worker_protocol_versions=["weave/future"]),
        lambda d: d["operations"].update(worker_protocol_versions=[]),
        lambda d: d["operations"]["compatibility"].update(ready=False),
        lambda d: d["operations"]["compatibility"].update(findings=["foreign-id"]),
    ],
)
async def test_strict_declaration_rejects_mismatches_and_unavailable_conventions(edit):
    controller, request, *_ = controller_fixture(ready_state())
    result = await controller.capabilities(request)
    edit(result)
    with pytest.raises(ValidationError):
        Capabilities.model_validate_json(json.dumps(result))


def test_legacy_response_omits_unknown_operations_instead_of_inventing_readiness():
    result = Capabilities(limits={}, schemas=[], connectors=[])
    assert result.operations is None
    assert "operations" not in result.model_dump(mode="json")
    assert Capabilities.model_validate_json(result.model_dump_json()).operations is None


@pytest.mark.parametrize("legacy", [False, True])
async def test_controller_schema_native_openapi_sdk_and_cli_share_the_same_declaration(monkeypatch, legacy):
    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.schema_export import export_schemas
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.sdk import client

    controller, request, _, _, scope = controller_fixture(ready_state())
    payload = await controller.capabilities(request)
    if legacy:
        payload.pop("operations")
    exported = export_schemas()["capabilities"]
    Draft202012Validator(exported).validate(payload)
    assert "operations" not in exported["required"]
    spec = export_openapi()
    response = spec["paths"][OPERATIONS["capabilities.read"].canonical_path]["get"]["responses"]["200"]
    schema = response["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": spec["components"]}).validate(payload)
    assert "OperationalCapabilities" in json.dumps(spec)
    original_client = client.WeaveClient
    calls = []

    def receive(http_request):
        calls.append(http_request)
        assert http_request.url.path.endswith("/capabilities")
        return httpx.Response(200, json=payload)

    def connected(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(receive))

    async with connected("https://api.invalid", lambda: "test-token", scope) as sdk:
        decoded = await sdk.capabilities()
    assert decoded.model_dump(mode="json") == payload
    monkeypatch.setattr(client, "WeaveClient", connected)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "test-token")
    result = await asyncio.to_thread(
        CliRunner().invoke,
        cli,
        [
            "remote",
            "capabilities",
            "--base-url",
            "https://api.invalid",
            "--tenant",
            str(scope.tenant_id),
            "--project",
            str(scope.project_id),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload
    assert len(calls) == 2


def test_sql_owned_debug_limits_match_frozen_migration_statements():
    from firefly_weave.operations.debug.models import (
        DEBUG_SESSION_RESERVATION_BYTES,
        DEBUG_SESSIONS_PER_CREATOR,
        DEBUG_SESSIONS_PER_PROJECT,
        DebugLimits,
    )

    migrations = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    sql = (migrations / "0021_operations.py").read_text()
    assert f"live_count>={DEBUG_SESSIONS_PER_PROJECT} OR creator_count>={DEBUG_SESSIONS_PER_CREATOR}" in sql
    assert f"THEN {DEBUG_SESSION_RESERVATION_BYTES} ELSE 0 END" in sql
    expiry = next(migrations.glob("*debug*.py")).read_text()
    assert f"interval '{DebugLimits().lifetime_seconds} seconds'" in expiry
    assert DebugLimits().session_bytes == DEBUG_SESSION_RESERVATION_BYTES


def test_pure_public_contract_does_not_import_server_or_framework():
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"pyfly", "starlette", "sqlalchemy", "asyncpg"} or fullname.startswith((
            "firefly_weave.runtime", "firefly_weave.settings", "firefly_weave.operations.debug",
            "firefly_weave.operations.transport", "firefly_weave.operations.execution")):
            raise AssertionError("Server dependency imported: " + fullname)
sys.meta_path.insert(0, Deny())
from firefly_weave.contracts.public import Capabilities
assert Capabilities(limits={}, schemas=[], connectors=[]).operations is None
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=os.environ.copy(), capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
