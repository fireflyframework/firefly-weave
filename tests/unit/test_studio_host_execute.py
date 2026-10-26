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

"""The Studio host's Execute route: same session and CSRF boundary as every local route, its own work slot."""

import json
from pathlib import Path

from starlette.testclient import TestClient

from firefly_weave.studio import host
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "orders", "version": "1.0.0"},
    "spec": {
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "pin": {"type": "string", "x-secret": True}},
        },
        "outputSchema": {},
        "steps": [{"id": "shape", "kind": "transform", "value": {"object": {"id": {"ref": "/input/id"}}}}],
        "output": {"literal": None},
    },
}
BODY = {
    "source": json.dumps(WORKFLOW),
    "format": "json",
    "catalog": "none",
    "input": {"id": "c-1"},
    "now": "2026-10-26T10:00:00Z",
    "target": {"mode": "workflow"},
}


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


def test_execute_answers_a_paired_browser_with_the_trace(tmp_path):
    with client(tmp_path) as browser:
        answer = browser.post("/studio/local/debug/execute", json=BODY, headers=pair(browser))
        assert answer.status_code == 200
        assert answer.json()["status"] == "completed"
        assert answer.json()["trace"][0]["output"] == {"id": "c-1"}


def test_execute_needs_the_session_and_the_csrf_token(tmp_path):
    with client(tmp_path) as browser:
        assert browser.post("/studio/local/debug/execute", json=BODY, headers={"Origin": ORIGIN}).status_code == 401
        pair(browser)
        refused = browser.post("/studio/local/debug/execute", json=BODY, headers={"Origin": ORIGIN})
        assert refused.status_code == 403 and refused.json()["code"] == "WV-STUDIO-CSRF"


def test_a_secret_value_is_refused_with_its_pointer_and_never_echoed(tmp_path):
    with client(tmp_path) as browser:
        answer = browser.post(
            "/studio/local/debug/execute", json=BODY | {"input": {"id": "c-1", "pin": "9911"}}, headers=pair(browser)
        )
        assert answer.status_code == 422
        assert answer.json()["code"] == "WV-SCHEMA-SECRET_VALUE"
        assert answer.json()["result"] == {"paths": ["/input"]}
        assert "9911" not in answer.text


def test_a_body_that_isnt_an_execute_request_is_refused(tmp_path):
    with client(tmp_path) as browser:
        answer = browser.post("/studio/local/debug/execute", json=BODY | {"extra": 1}, headers=pair(browser))
        assert answer.status_code == 422 and answer.json()["code"] == "WV-STUDIO-REQUEST"


def test_a_busy_execution_slot_says_so_and_validation_keeps_its_own_slots(tmp_path, monkeypatch):
    with client(tmp_path) as browser:
        headers = pair(browser)
        monkeypatch.setattr(host._DEBUG_WORK, "claim", lambda: False)
        busy = browser.post("/studio/local/debug/execute", json=BODY, headers=headers)
        assert busy.status_code == 503 and busy.json()["code"] == "WV-STUDIO-BUSY"
        validation = browser.post(
            "/studio/local/validate", json={"source": BODY["source"], "format": "json"}, headers=headers
        )
        assert validation.status_code == 200


TENANT = "00000000-0000-0000-0000-000000000001"
PROJECT = "00000000-0000-0000-0000-000000000002"


def connected(tmp_path: Path, upstream) -> TestClient:
    import httpx

    from firefly_weave.studio.service import StudioProfile

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    profile = StudioProfile(name="Dev", base_url="https://api.example", tenant_id=TENANT, project_id=PROJECT)
    options = StudioOptions(
        origin=ORIGIN,
        assets=assets,
        pairing_code=CODE,
        profile=profile,
        token_provider=lambda: "host-token",
        transport=httpx.MockTransport(upstream),
    )
    return TestClient(make_studio_app(options), base_url=ORIGIN)


def lock() -> dict:
    import yaml

    from firefly_weave.compiler.catalog import FrozenDocument
    from firefly_weave.contracts.definitions import load_definition

    action = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))
    document = load_definition(action).model_dump(by_alias=True, mode="json")
    spec = action["spec"]
    task = {
        "taskType": "onboarding.check-customer",
        "taskVersion": "1.0.0",
        "inputSchema": spec["inputSchema"],
        "outputSchema": spec["outputSchema"],
        "sideEffect": "read_only",
        "timeoutSeconds": 60,
    }
    return {
        "definitions": [{"document": document, "digest": FrozenDocument.from_value(document).digest}],
        "tasks": [task],
        "adapters": [],
        "schemas": {},
    }


CONNECTED = BODY | {
    "catalog": "project",
    "source": json.dumps(
        WORKFLOW
        | {
            "spec": WORKFLOW["spec"]
            | {
                "steps": [
                    {
                        "id": "check",
                        "kind": "action",
                        "uses": "onboarding.check-customer@1.0.0",
                        "with": {"object": {"customerId": {"ref": "/input/id"}}},
                    }
                ]
            }
        }
    ),
    "mocks": {"node:check": {"eligible": True}},
}


def test_connected_execution_reads_only_the_project_catalog_once_and_runs_nothing_on_the_platform(tmp_path):
    import httpx

    seen: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=lock())

    with connected(tmp_path, upstream) as browser:
        headers = pair(browser)
        for _ in range(2):
            answer = browser.post("/studio/local/debug/execute", json=CONNECTED, headers=headers)
            assert answer.status_code == 200 and answer.json()["status"] == "completed"
        browser.post("/studio/local/debug/execute", json=CONNECTED | {"refreshCatalog": True}, headers=headers)
    catalog = f"/api/v1/tenants/{TENANT}/projects/{PROJECT}/catalog"
    assert seen == [("GET", catalog), ("GET", catalog)]


def test_an_unreadable_project_catalog_is_said_and_nothing_runs(tmp_path):
    import httpx

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "WV-UNAVAILABLE", "message": "down"})

    with connected(tmp_path, upstream) as browser:
        answer = browser.post("/studio/local/debug/execute", json=CONNECTED, headers=pair(browser))
        assert answer.status_code == 502 and answer.json()["code"] == "WV-STUDIO-CATALOG"


def test_execution_makes_no_request_but_the_catalog(tmp_path):
    import httpx

    seen: list[tuple[str, str]] = []
    catalog = f"/api/v1/tenants/{TENANT}/projects/{PROJECT}/catalog"

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert (request.method, request.url.path) == ("GET", catalog), "only the catalog may be read"
        return httpx.Response(200, json=lock())

    summarize = {
        "id": "summarize",
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "summarize",
        "prompt": {"literal": "Summarize the ticket."},
        "context": {"literal": {}},
        "connection": "ai",
    }
    document = json.loads(CONNECTED["source"])
    document["spec"]["steps"].append(summarize)
    document["spec"]["connections"] = {"ai": {"connector": "weave-agentic-provider@1.0.0"}}
    document["spec"]["llmProfiles"] = {
        "summarize": {
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "options": {"max_tokens": 1024},
            "outputSchema": {"type": "string", "maxLength": 20000},
        }
    }
    body = CONNECTED | {
        "source": json.dumps(document),
        "scripts": {"aiTurns": {"summarize": {"turns": [{"final": "Done."}], "source": "manual"}}},
    }
    with connected(tmp_path, upstream) as browser:
        answer = browser.post("/studio/local/debug/execute", json=body, headers=pair(browser))
        assert answer.status_code == 200, answer.text
        assert answer.json()["status"] == "completed", answer.text
        assert [entry["nodeId"] for entry in answer.json()["trace"]] == ["check", "summarize"]
    assert seen == [("GET", catalog)]


def test_an_oversized_execute_body_is_refused_before_it_is_read(tmp_path):
    with client(tmp_path) as browser:
        huge = BODY | {"input": {"id": "x" * (2 * 1024 * 1024)}}
        assert browser.post("/studio/local/debug/execute", json=huge, headers=pair(browser)).status_code == 413
