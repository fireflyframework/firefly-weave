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

"""`weave agent-conversations`: a key is hashed with the workflow on this machine before anything is sent."""

import json
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner

from firefly_weave.cli.remote import family
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_conversations import conversation_key_hash
from firefly_weave.sdk import client as sdk

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
HASH = conversation_key_hash("support-chat", "C-4777")
ENVIRONMENT = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"
AT = "2026-11-18T12:00:00Z"


def run_cli(monkeypatch, *arguments: str, request_file=None) -> tuple[object, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    real = sdk.WeaveClient

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "DELETE":
            body = {
                "workflow": "support-chat",
                "key_hash": HASH,
                "deleted": False,
                "loaded_by": [],
                "more_loaded_by": False,
            }
        elif request.method == "POST":
            body = {"workflow": "support-chat", "deleted": 0, "more": False}
        elif request.url.path.endswith("/agent-conversations"):
            body = {"items": [], "next_cursor": None}
        else:
            body = {
                "workflow": "support-chat",
                "key_hash": HASH,
                "message_count": 0,
                "version": 1,
                "created_at": AT,
                "updated_at": AT,
                "expires_at": AT,
                "messages": [],
                "last_run_id": str(uuid4()),
            }
        return httpx.Response(200, json=body)

    def recording(base_url, provider, scope):
        return real(base_url, provider, scope, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(sdk, "WeaveClient", recording)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "token")
    scope = ["--base-url", "https://platform.example", "--tenant", str(SCOPE.tenant_id)]
    scope += ["--project", str(SCOPE.project_id), "--environment", str(SCOPE.environment_id)]
    group = family("agent-conversations", "agent_conversations")
    return CliRunner().invoke(group, [*arguments, *scope]), calls


@pytest.mark.parametrize("command", ["read", "delete"])
def test_the_cli_hashes_a_key_before_it_sends_anything(monkeypatch, command):
    result, calls = run_cli(monkeypatch, command, "--workflow", "support-chat", "--key", "C-4777")

    assert result.exit_code == 0, result.output
    [call] = calls
    assert call.url.path == f"{ENVIRONMENT}/agent-conversations/support-chat/{HASH}"
    assert "C-4777" not in str(call.url) and b"C-4777" not in call.content and "C-4777" not in result.output


def test_the_cli_takes_a_key_hash_as_it_is(monkeypatch):
    result, [call] = run_cli(monkeypatch, "read", "--workflow", "support-chat", "--key-hash", HASH)

    assert result.exit_code == 0, result.output
    assert call.url.path.endswith(f"/support-chat/{HASH}")


@pytest.mark.parametrize(
    "names",
    [[], ["--key", "C-4", "--key-hash", HASH], ["--key", ""], ["--key-hash", "C-4"]],
    ids=["neither", "both", "empty-key", "raw-hash"],
)
def test_the_cli_needs_a_key_or_a_key_hash_not_both(monkeypatch, names):
    result, calls = run_cli(monkeypatch, "read", "--workflow", "support-chat", *names)

    assert result.exit_code == 2 and calls == []


def test_the_cli_lists_one_workflows_conversations(monkeypatch):
    result, [call] = run_cli(monkeypatch, "list", "--workflow", "support-chat", "--limit", "10")

    assert result.exit_code == 0, result.output
    assert call.url.raw_path.decode() == f"{ENVIRONMENT}/agent-conversations?workflow=support-chat&limit=10"
    assert json.loads(result.output) == {"items": [], "next_cursor": None}


def test_the_cli_purges_a_workflow_with_its_confirmation(monkeypatch):
    result, [call] = run_cli(
        monkeypatch,
        "purge-workflow",
        "--workflow",
        "support-chat",
        "--confirm-workflow",
        "support-chat",
        "--idempotency-key",
        "purge-1",
    )

    assert result.exit_code == 0, result.output
    assert call.url.path == f"{ENVIRONMENT}/agent-conversations/support-chat/purge"
    assert json.loads(call.content) == {"confirm_workflow": "support-chat"}
    assert call.headers["Idempotency-Key"] == "purge-1"
