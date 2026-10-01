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

"""Retention apply previews the exact plan through the real SDK before mutation."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.maintenance import RetentionPlan
from firefly_weave.sdk import client


@pytest.mark.parametrize("selector", ["option", "positional", "both"])
@pytest.mark.parametrize("response", ["ok", "denied", "mismatch"])
def test_apply_reads_displays_then_applies_selected_immutable_plan(monkeypatch, selector, response):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    identifier = uuid4()
    now = datetime.now(UTC)
    plan = RetentionPlan(
        id=uuid4() if response == "mismatch" else identifier,
        scope=scope,
        principal_id=uuid4(),
        created_at=now,
        cutoff=now - timedelta(days=1),
        expires_at=now + timedelta(minutes=15),
        complete=True,
        candidates=[],
    )
    calls, displayed = [], []
    original_client = client.WeaveClient

    def receive(request):
        calls.append((request.method, request.url.path))
        assert str(identifier) in request.url.path
        if request.method == "GET":
            if response == "denied":
                return httpx.Response(
                    404,
                    json={
                        "status": 404,
                        "code": "WV-NOT-FOUND",
                        "message": "Retention plan not found",
                        "diagnostics": [],
                    },
                )
            return httpx.Response(200, json=plan.model_dump(mode="json"))
        assert displayed == [plan.model_dump(mode="json")]
        return httpx.Response(
            200, json={"plan_id": str(identifier), "deleted": [], "blocked": [], "policy": "weave/operations-v1"}
        )

    monkeypatch.setattr(
        client, "WeaveClient", lambda *a, **k: original_client(*a, **k, transport=httpx.MockTransport(receive))
    )
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "test-token")
    import click

    echo = click.echo

    def observed(message=None, *args, **kwargs):
        if kwargs.get("err") and message:
            displayed.append(json.loads(message))
        return echo(message, *args, **kwargs)

    monkeypatch.setattr(click, "echo", observed)
    args = ["retention", "apply"]
    if selector in {"positional", "both"}:
        args.append(str(identifier))
    if selector in {"option", "both"}:
        args.extend(["--plan-id", str(identifier)])
    args.extend(
        ["--base-url", "https://unused.invalid", "--tenant", str(scope.tenant_id), "--project", str(scope.project_id)]
    )
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == (0 if response == "ok" else 1 if response == "denied" else 2), result.output
    assert [method for method, _ in calls] == (["GET", "POST"] if response == "ok" else ["GET"])
    if response == "ok":
        assert json.loads(result.stderr) == plan.model_dump(mode="json")
        assert json.loads(result.stdout)["plan_id"] == str(identifier)
    else:
        assert not displayed


@pytest.mark.parametrize("selectors", [[], ["positional", "different"]])
def test_missing_or_conflicting_plan_selection_never_opens_sdk(monkeypatch, selectors):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid selection opened client")

    monkeypatch.setattr(client, "WeaveClient", forbidden)
    args = ["retention", "apply"]
    if selectors:
        args.extend([str(uuid4()), "--plan-id", str(uuid4())])
    args.extend(["--base-url", "https://unused.invalid", "--tenant", str(uuid4()), "--project", str(uuid4())])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2
