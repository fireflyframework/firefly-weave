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

"""`runs.agent_log` is registered, documented and reachable from the SDK and the CLI like every operation."""

import json
import re
from functools import cache
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_log import AgentLogPage, AgentLogQuery, AgentToolTarget, agent_log_key
from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.sdk.client import WeaveClient

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())

spec = cache(export_openapi)


def native() -> dict:
    operation = OPERATIONS["runs.agent_log"]
    return spec()["paths"][operation.canonical_path][operation.method.lower()]


def resolved(value: dict) -> dict:
    while "$ref" in value:
        value = spec()["components"]["schemas"][value["$ref"].rsplit("/", 1)[1]] | {
            key: item for key, item in value.items() if key != "$ref"
        }
    return value


def test_the_agent_log_is_a_served_run_read():
    operation = OPERATIONS["runs.agent_log"]

    assert (operation.method, operation.canonical_path) == ("GET", ENVIRONMENT + "/runs/{identifier}/agents/{key}/log")
    assert (operation.response, operation.query, operation.capability) == (AgentLogPage, AgentLogQuery, "run.read")
    assert operation.served is True and operation.request is None and operation.page is False


def test_openapi_documents_the_key_and_a_page_of_at_most_twenty():
    parameters = {parameter["name"]: parameter for parameter in native()["parameters"]}

    key = resolved(parameters["key"]["schema"])
    assert parameters["key"]["in"] == "path" and parameters["key"]["required"] is True
    assert key["maxLength"] == 512 and re.fullmatch(key["pattern"][1:-1], "support[2][0]")
    assert not re.fullmatch(key["pattern"][1:-1], "support#1.1")
    assert resolved(parameters["limit"]["schema"]).items() >= {"minimum": 1, "maximum": 20, "default": 20}.items()
    assert parameters["cursor"]["required"] is False
    assert native()["security"] == [{"bearer": []}]
    assert "Required capability: run.read" in native()["description"]
    assert "501" not in native()["responses"] and "404" in native()["responses"]


def test_a_tool_target_kind_is_open_text():
    # A new kind of tool reaches clients without a contract change: the kind is text, not a closed list.
    assert AgentToolTarget(kind="mcp", reference="orders-mcp@1.0.0").kind == "mcp"
    schema = AgentToolTarget.model_json_schema()["properties"]["kind"]
    assert "enum" not in schema and schema["pattern"] == "^[a-z][a-z0-9_]*$"


@pytest.mark.parametrize("key", ["support", "support[0]", "a.b-c_d[12][3]", "7days"])
def test_agent_log_keys_are_steps_and_their_activations(key):
    assert agent_log_key(key) == key


@pytest.mark.parametrize(
    "key", ["support#1", "support#1.1.review", "each~1", "@run", "support[01]", "support[]", "a/b", "", 7, "x" * 513]
)
def test_other_keys_are_refused(key):
    with pytest.raises(ValueError):
        agent_log_key(key)


def recording(calls: list[httpx.Request]) -> WeaveClient:
    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    return WeaveClient("https://platform.example", lambda: "token", SCOPE, transport=httpx.MockTransport(receive))


async def test_the_sdk_reads_the_agent_log_with_loop_indexes_encoded():
    run, calls = uuid4(), []

    async with recording(calls) as client:
        page = await client.read_agent_log(run, "support[2]", limit=5, cursor="abc")
        assert isinstance(page, AgentLogPage) and page.items == []
        await client.invoke("runs.agent_log", identifier=run, key="support")

    environment = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"
    assert [call.url.raw_path.decode() for call in calls] == [
        f"{environment}/runs/{run}/agents/support%5B2%5D/log?limit=5&cursor=abc",
        f"{environment}/runs/{run}/agents/support/log",
    ]


@pytest.mark.parametrize("key", ["support#1.1", "support/../steps", "each~1", "@run", "support[01]", "", None])
async def test_the_sdk_refuses_a_key_that_names_no_agent_before_any_request(key):
    calls: list[httpx.Request] = []

    async with recording(calls) as client:
        with pytest.raises(ValueError):
            await client.invoke("runs.agent_log", identifier=uuid4(), key=key)

    assert calls == []


class Client:
    """The SDK client as the CLI uses it, keeping each call."""

    calls: list[dict] = []

    def __init__(self, base_url, provider, scope) -> None:
        self.scope = scope

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def invoke(self, operation, **values):
        Client.calls.append({"operation": operation, **values})
        return AgentLogPage(items=[])


def run_cli(monkeypatch, *arguments: str):
    from firefly_weave.cli.runs import runs

    Client.calls = []
    monkeypatch.setattr("firefly_weave.sdk.client.WeaveClient", Client)
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "token")
    scope = ["--base-url", "https://platform.example", "--tenant", str(SCOPE.tenant_id)]
    scope += ["--project", str(SCOPE.project_id), "--environment", str(SCOPE.environment_id)]
    return CliRunner().invoke(runs, ["agent-log", *arguments, *scope])


def test_the_cli_reads_the_agent_log_by_key(monkeypatch):
    run = uuid4()

    result = run_cli(monkeypatch, str(run), "--key", "support[2]", "--limit", "5", "--cursor", "abc")

    assert result.exit_code == 0, result.output
    [call] = Client.calls
    assert (call["operation"], call["identifier"], call["key"]) == ("runs.agent_log", run, "support[2]")
    assert call["query"] == {"cursor": "abc", "limit": 5}
    assert json.loads(result.output) == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("arguments", [["--key", "support#1.1"], ["--limit", "21", "--key", "support"], []])
def test_the_cli_refuses_a_key_or_limit_outside_the_contract(monkeypatch, arguments):
    result = run_cli(monkeypatch, str(uuid4()), *arguments)

    assert result.exit_code == 2 and Client.calls == []
