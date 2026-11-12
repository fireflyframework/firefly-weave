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

"""`runs.agent_log` checks run.read twice, refuses keys that name no activation, and pages a step's activations."""

import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))

from agent_fixtures import LOOKUP, agent, answering, artifact, plain, workflow  # noqa: E402

from firefly_weave.access.audit import AuditContext  # noqa: E402
from firefly_weave.access.authorization import AccessDenied, AuthorizationService  # noqa: E402
from firefly_weave.access.models import Grant, Principal  # noqa: E402
from firefly_weave.api.agent_log import AgentLogController  # noqa: E402
from firefly_weave.compiler.ir import WorkflowIR  # noqa: E402
from firefly_weave.contracts.access import Scope  # noqa: E402
from firefly_weave.contracts.agent_log import AgentLogPage  # noqa: E402
from firefly_weave.definitions.models import CatalogError  # noqa: E402
from firefly_weave.operations.agent_log import AgentLogService, StepRecord  # noqa: E402

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
RUN = UUID("2b4f6a1c-8d3e-4f5a-9b7c-0d1e2f3a4b5c")
CONTEXT = AuditContext()
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
IR = WorkflowIR.model_validate(artifact(plain(answering(workflow(agent(tools=[LOOKUP]))))).executable)


def final(index: int) -> dict:
    return {
        "kind": "final",
        "text": "",
        "result": f"Answer {index}",
        "usage": {"requests": 1, "inputTokens": 100 + index, "outputTokens": 10},
        "provider": "openai-chat",
        "model": "qwen3:4b",
        "finishReason": "stop",
    }


class Recorded:
    """What a run recorded for 25 loop iterations of the agent, each answered in one turn, read as the platform reads
    it: activations in iteration order after a position, and only the keys asked for."""

    def __init__(self, tx, iterations: int = 25) -> None:
        self.reads: list[str] = []
        keys = [f"support[{index}]" for index in range(iterations)]
        self.activations_ = keys
        self.facts = [
            fact
            for index, key in enumerate(keys)
            for fact in (
                StepRecord(
                    key, "succeeded", NOW + index * timedelta(seconds=2), None, NOW + index * timedelta(seconds=3)
                ),
                StepRecord(key + "#1", "succeeded", NOW, NOW, NOW + timedelta(seconds=1)),
            )
        ]
        self.answers = {key + "#1": final(index) for index, key in enumerate(keys)}

    async def workflow(self, run_id):
        self.reads.append("workflow")
        if run_id != RUN:
            raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
        return IR

    async def activations(self, run_id, step, after, limit):
        self.reads.append("activations")
        indexes = [[int(key[len(step) + 1 : -1])] for key in self.activations_]
        return [key for key, index in zip(self.activations_, indexes, strict=True) if after is None or index > after][
            :limit
        ]

    async def steps(self, run_id, step, keys):
        self.reads.append("steps")
        return [fact for fact in self.facts if any(fact.key == key or fact.key.startswith(key + "#") for key in keys)]

    async def outputs(self, run_id, keys):
        self.reads.append("outputs")
        return {key: self.answers[key] for key in keys if key in self.answers}, {}

    async def turns(self, run_id, keys):
        self.reads.append("turns")
        return []

    async def agents(self, run_id, keys):
        self.reads.append("agents")
        return {}


class ReadUnit:
    def __init__(self) -> None:
        self.opened = 0
        self.tx = SimpleNamespace(session=object(), scope=SCOPE)

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert scope == SCOPE and mutation is False
        self.opened += 1
        yield self.tx


def principal(role: str | None = "viewer", *, active: bool = True) -> Principal:
    grants = () if role is None else (Grant(role=role, scope=SCOPE),)
    return Principal(id=uuid4(), kind="human", active=active, grants=grants)


@pytest.fixture
def platform(monkeypatch):
    """The service over a read unit and the recorded run; ``platform.current`` is the principal stored now."""
    unit = ReadUnit()
    state = SimpleNamespace(unit=unit, current=principal(), recorded=None, iterations=25)

    async def load(session, identifier):
        assert session is unit.tx.session
        return state.current.model_copy(update={"id": identifier})

    def repository(tx):
        assert tx is unit.tx
        state.recorded = state.recorded or Recorded(tx, state.iterations)
        return state.recorded

    monkeypatch.setattr("firefly_weave.operations.agent_log.load_principal", load)
    monkeypatch.setattr("firefly_weave.operations.agent_log.AgentLogRepository", repository)
    state.service = AgentLogService(unit, AuthorizationService())
    return state


async def read(platform, key="support", *, actor=None, limit=20, cursor=None, run=RUN) -> AgentLogPage:
    return await platform.service.read(
        actor or principal(), SCOPE, run, key, limit=limit, cursor=cursor, context=CONTEXT
    )


@pytest.mark.parametrize(
    "token,current,opened",
    [
        (principal(None), principal("viewer"), 0),
        (principal("operator"), principal("operator"), 0),
        (principal("viewer"), principal("viewer", active=False), 1),
        (principal("viewer"), principal(None), 1),
    ],
)
async def test_a_person_without_run_read_gets_403_and_no_data(platform, token, current, opened):
    platform.current = current

    with pytest.raises(AccessDenied):
        await read(platform, actor=token)

    # The token is checked before any read unit opens; the stored principal inside one, before anything is read.
    assert platform.unit.opened == opened
    assert platform.recorded is None


async def test_a_step_id_pages_its_iterations(platform):
    first = await read(platform, limit=20)

    assert [item.instance_key for item in first.items] == [f"support[{index}]" for index in range(20)]
    assert first.items[3].turns[0].input_tokens == 103 and first.items[3].status == "succeeded"
    assert first.next_cursor is not None

    second = await read(platform, limit=20, cursor=first.next_cursor)

    assert [item.instance_key for item in second.items] == [f"support[{index}]" for index in range(20, 25)]
    assert second.next_cursor is None


async def test_a_last_full_page_has_no_cursor(platform):
    platform.iterations = 20

    page = await read(platform, limit=20)

    assert [item.instance_key for item in page.items] == [f"support[{index}]" for index in range(20)]
    assert page.next_cursor is None


async def test_a_cursor_belongs_to_its_run_and_key(platform):
    page = await read(platform, limit=5)

    with pytest.raises(ValueError):
        await read(platform, "support[3]", cursor=page.next_cursor)
    with pytest.raises(ValueError):
        await read(platform, cursor=page.next_cursor, run=uuid4())


async def test_an_activation_key_reads_that_activation(platform):
    page = await read(platform, "support[7]")

    assert [item.instance_key for item in page.items] == ["support[7]"] and page.next_cursor is None
    assert page.items[0].turns[0].final.value == "Answer 7"


@pytest.mark.parametrize("key", ["nope", "support[99]"])
async def test_an_unknown_key_is_404(platform, key):
    with pytest.raises(CatalogError) as failure:
        await read(platform, key)

    assert (failure.value.status, failure.value.code) == (404, "WV-NOT-FOUND")
    assert "outputs" not in platform.recorded.reads


async def test_an_unknown_run_is_404(platform):
    with pytest.raises(CatalogError) as failure:
        await read(platform, run=uuid4())

    assert (failure.value.status, failure.value.code, failure.value.message) == (404, "WV-NOT-FOUND", "Run not found")


@pytest.mark.parametrize("key", ["support#1", "support#1.1", "support#1.1.review", "each~1", "@run", "support[01]", ""])
async def test_a_key_that_names_no_activation_is_refused_before_any_read(platform, key):
    with pytest.raises(ValueError):
        await read(platform, key)

    assert platform.unit.opened == 0


@pytest.mark.parametrize("limit", [0, 21])
async def test_a_page_holds_one_to_twenty_activations(platform, limit):
    with pytest.raises(ValueError):
        await read(platform, limit=limit)

    assert platform.unit.opened == 0


def request(key: str, query: str) -> Request:
    value = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agent-log",
            "query_string": query.encode(),
            "headers": [],
            "path_params": {
                "tenant": str(SCOPE.tenant_id),
                "project": str(SCOPE.project_id),
                "environment": str(SCOPE.environment_id),
                "identifier": str(RUN),
                "key": key,
            },
        }
    )
    value.state.principal = principal()
    value.state.audit_context = CONTEXT
    return value


class Service:
    def __init__(self) -> None:
        self.calls = []

    async def read(self, actor, scope, run_id, key, *, limit, cursor, context):
        self.calls.append((scope, run_id, key, limit, cursor))
        return AgentLogPage(items=[])


async def test_the_route_reads_its_key_and_query():
    service = Service()

    response = await AgentLogController(service).log(request("support[3]", "limit=5&cursor=abc"))

    assert response.status_code == 200 and response.body == b'{"items":[],"next_cursor":null}'
    assert service.calls == [(SCOPE, RUN, "support[3]", 5, "abc")]


@pytest.mark.parametrize("query", ["limit=21", "limit=0", "step=support", "limit=5&limit=6"])
async def test_the_route_refuses_a_query_outside_its_contract(query):
    service = Service()

    with pytest.raises(ValueError):
        await AgentLogController(service).log(request("support", query))

    assert service.calls == []


def test_the_bridge_forwards_agent_log(tmp_path):
    from starlette.testclient import TestClient

    from firefly_weave.studio.host import make_studio_app
    from firefly_weave.studio.service import StudioOptions, StudioProfile

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    profile = StudioProfile(
        name="Dev",
        base_url="https://api.example",
        tenant_id=str(SCOPE.tenant_id),
        project_id=str(SCOPE.project_id),
        environment_id=str(SCOPE.environment_id),
    )
    options = StudioOptions(
        origin="http://127.0.0.1:8766", assets=assets, pairing_code="code", profile=profile, token_provider=lambda: "t"
    )
    environment = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"
    with TestClient(make_studio_app(options), base_url=options.origin) as browser:
        studio = browser.app.state.studio
        assert studio.check_scope(f"{environment}/runs/{RUN}/agents/support/log", "GET") == 200
        assert studio.check_scope(f"{environment}/runs/{RUN}/agents/support%5B2%5D/log", "GET") == 200
        # Only reads cross the bridge, and only for keys that name an agent step or an activation.
        assert studio.check_scope(f"{environment}/runs/{RUN}/agents/support/log", "POST") != 200
        for key in ("support%231.1", "..%2F..%2Fadmin", "support%5B01%5D", "%ff"):
            assert studio.check_scope(f"{environment}/runs/{RUN}/agents/{key}/log", "GET") == 404, key
