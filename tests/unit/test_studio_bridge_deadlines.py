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

"""A model test may take longer than other platform calls, and stops when its browser leaves."""

import asyncio
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from firefly_weave.studio import service as studio_service
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions, StudioProfile, StudioService

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
TENANT = "00000000-0000-0000-0000-000000000001"
PROJECT_ID = "00000000-0000-0000-0000-000000000002"
ENVIRONMENT_ID = "00000000-0000-0000-0000-000000000003"
REVISION = "00000000-0000-0000-0000-0000000000aa"
ENVIRONMENT = f"/api/v1/tenants/{TENANT}/projects/{PROJECT_ID}/environments/{ENVIRONMENT_ID}"
MODEL_TEST = f"{ENVIRONMENT}/ai/connections/{REVISION}/test"
READINESS = f"{ENVIRONMENT}/ai/readiness"
PROFILE = StudioProfile(
    name="Dev", base_url="https://api.example", tenant_id=TENANT, project_id=PROJECT_ID, environment_id=ENVIRONMENT_ID
)


def assets(tmp_path: Path) -> Path:
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return folder


def paired(tmp_path: Path, upstream) -> TestClient:
    browser = TestClient(
        make_studio_app(
            StudioOptions(
                origin=ORIGIN,
                assets=assets(tmp_path),
                pairing_code=CODE,
                profile=PROFILE,
                token_provider=lambda: "host-token",
                transport=httpx.MockTransport(upstream),
            )
        ),
        base_url=ORIGIN,
    )
    response = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    browser.headers.update({"Origin": ORIGIN, "X-Weave-CSRF": response.json()["csrfToken"]})
    return browser


def test_the_bridge_names_the_operation_a_path_matches(tmp_path):
    service = StudioService(StudioOptions(origin=ORIGIN, assets=assets(tmp_path), profile=PROFILE))
    assert service.match(MODEL_TEST, "POST") == (200, "ai_connections.test")
    assert service.match(READINESS, "GET") == (200, "ai_readiness.read")
    other = MODEL_TEST.replace(ENVIRONMENT_ID, "00000000-0000-0000-0000-000000000009")
    assert service.match(other, "POST") == (403, "ai_connections.test")
    assert service.match("/api/v1/unknown", "GET") == (404, None)
    assert service.check_scope(MODEL_TEST, "POST") == 200


def test_a_model_test_may_outlast_the_ordinary_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(studio_service, "BRIDGE_SECONDS", 0.2)
    monkeypatch.setattr(studio_service, "LONG_OPERATIONS", MappingProxyType({"ai_connections.test": 3.0}))

    async def upstream(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        if request.url.path.endswith("/test"):
            return httpx.Response(200, json={"ok": True, "code": "ok", "latency_ms": 500, "model": "qwen3:4b"})
        return httpx.Response(200, json={"items": []})

    with paired(tmp_path, upstream) as browser:
        tested = browser.post("/studio/api" + MODEL_TEST, json={"model": "qwen3:4b", "probe_tools": True})
        assert tested.status_code == 200, tested.text
        assert tested.json()["model"] == "qwen3:4b"
        slow = browser.get("/studio/api" + READINESS)
        assert slow.status_code == 502
        assert slow.json()["code"] == "WV-STUDIO-UPSTREAM"


async def test_a_model_test_stops_when_the_browser_leaves(tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx.Response(200, json={})

    service = StudioService(
        StudioOptions(
            origin=ORIGIN,
            assets=assets(tmp_path),
            profile=PROFILE,
            token_provider=lambda: "host-token",
            transport=httpx.MockTransport(upstream),
        )
    )
    messages = [{"type": "http.request", "body": b'{"model":"qwen3:4b","probe_tools":true}', "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await started.wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/studio/api" + MODEL_TEST,
        "raw_path": ("/studio/api" + MODEL_TEST).encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "scheme": "http",
        "server": ("127.0.0.1", 8766),
        "root_path": "",
    }
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.bridge(Request(scope, receive), MODEL_TEST)
        assert cancelled.is_set()
    finally:
        await service.close()
