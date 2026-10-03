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

"""Studio host endpoints for no-code integrations: paired, CSRF-bound, bounded, offline."""

import json
import socket
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
PETSTORE = (Path(__file__).parents[1] / "fixtures/openapi/petstore.yaml").read_text()
RELAX = {"numericFormats": True, "ignoreResponseHeaders": True, "jsonMediaOnly": True, "defaultStringMaxLength": 64}
ENDPOINTS = {
    "/studio/local/openapi/inventory": {"source": PETSTORE, "format": "yaml"},
    "/studio/local/openapi/import": {"source": PETSTORE, "format": "yaml", "selection": ["createPet"]},
    "/studio/local/http-action": {"request": {"name": "get-pet", "method": "GET", "pathTemplate": "/v1/pets/{id}"}},
}


@pytest.fixture
def browser(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    app = make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE))
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Studio integration endpoints must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def pair(browser):
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


@pytest.mark.parametrize("path", sorted(ENDPOINTS))
def test_endpoints_require_pairing_csrf_and_same_origin(browser, path):
    payload = ENDPOINTS[path]
    assert browser.post(path, json=payload, headers={"Origin": ORIGIN}).status_code == 401
    headers = pair(browser)
    assert browser.post(path, json=payload, headers={"Origin": ORIGIN}).status_code == 403
    assert browser.post(path, json=payload, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert browser.post(path, json=payload).status_code == 403
    assert browser.post(path, json=payload, headers=headers).status_code == 200


@pytest.mark.parametrize("path", sorted(ENDPOINTS))
def test_endpoints_bound_and_validate_bodies(browser, path):
    headers = pair(browser)
    assert browser.post(path, content=b"x" * (2 * 1024 * 1024 + 1), headers=headers).status_code == 413
    assert (
        browser.post(
            path, json={**ENDPOINTS[path], "url": "https://evil.example/api.json"}, headers=headers
        ).status_code
        == 422
    )
    assert browser.post(path, content=b"not json", headers=headers).status_code == 422


def test_inventory_lists_operations_offline(browser, offline):
    headers = pair(browser)
    response = browser.post(
        "/studio/local/openapi/inventory", json=ENDPOINTS["/studio/local/openapi/inventory"], headers=headers
    )
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["ok"] and {op["key"] for op in data["operations"]} >= {"createPet", "GET /health"}
    relaxed = browser.post(
        "/studio/local/openapi/inventory",
        json={"source": PETSTORE, "format": "yaml", "relaxations": RELAX},
        headers=headers,
    ).json()
    assert sum(op["supported"] for op in relaxed["operations"]) == 4
    broken = browser.post("/studio/local/openapi/inventory", json={"source": "{", "format": "json"}, headers=headers)
    assert broken.status_code == 200 and not broken.json()["ok"]


def test_import_scaffolds_a_policy_and_returns_builtin_actions(browser, offline):
    headers = pair(browser)
    response = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "selection": ["createPet", "listPets"], "relaxations": RELAX},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"], data["diagnostics"]
    assert {a["metadata"]["name"] for a in data["actions"]} == {"create-pet", "list-pets"}
    assert all(a["spec"]["implementation"]["uses"] == "weave-http@2.0.0" for a in data["actions"])
    assert data["connectionExample"]["secretRef"] == {"api_key": "<operator secret handle for api_key>"}
    assert data["policy"]["operations"]["listPets"]["name"] == "list-pets"
    assert "WV-IMPORT-RELAXED_FORMAT" in {d["code"] for d in data["diagnostics"]}
    reviewed = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "policy": data["policy"]},
        headers=headers,
    ).json()
    assert reviewed["ok"] and reviewed["actions"] == data["actions"]


def test_import_reports_every_problem_and_never_builds_packages(browser):
    headers = pair(browser)
    data = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "selection": ["listPets", "showPetById"]},
        headers=headers,
    ).json()
    assert not data["ok"] and data["actions"] == [] and len(data["diagnostics"]) == 4
    package = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "selection": ["createPet"], "target": "package"},
        headers=headers,
    )
    assert package.status_code == 422


def test_http_action_endpoint_builds_and_compiles(browser, offline):
    headers = pair(browser)
    data = browser.post(
        "/studio/local/http-action",
        json={
            "request": {
                "name": "create-pet",
                "method": "POST",
                "pathTemplate": "/v1/pets",
                "bodySample": {"name": "STUDIO-CANARY"},
                "responseSample": {"id": 1},
                "statuses": [201],
            }
        },
        headers=headers,
    ).json()
    assert data["ok"] and data["compiled"]
    assert data["action"]["spec"]["sideEffect"] == "non_idempotent" and data["action"]["spec"]["retry"] == {
        "maxAttempts": 1
    }
    assert "STUDIO-CANARY" not in json.dumps(data)
    refused = browser.post(
        "/studio/local/http-action",
        json={"request": {"name": "x", "method": "GET", "pathTemplate": "/x", "sideEffect": "non_idempotent"}},
        headers=headers,
    ).json()
    assert not refused["ok"] and refused["diagnostics"][0]["code"] == "WV-HTTP-ACTION-REQUEST"


@pytest.mark.parametrize(
    "extra",
    [{"selection": ["createPet"]}, {"relaxations": RELAX}, {"name": "pets"}],
    ids=["selection", "relaxations", "name"],
)
def test_import_refuses_a_policy_mixed_with_scaffolding_fields(browser, extra):
    headers = pair(browser)
    scaffold = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "selection": ["createPet"]},
        headers=headers,
    ).json()
    assert scaffold["ok"], scaffold["diagnostics"]
    mixed = browser.post(
        "/studio/local/openapi/import",
        json={"source": PETSTORE, "format": "yaml", "policy": scaffold["policy"], **extra},
        headers=headers,
    )
    assert mixed.status_code == 422 and mixed.json()["code"] == "WV-STUDIO-REQUEST"


def test_slow_local_work_answers_busy_instead_of_hanging(browser, monkeypatch):
    import time

    from firefly_weave.sdk import openapi_import
    from firefly_weave.studio import host

    def slow(*args, **kwargs):
        time.sleep(0.5)
        raise AssertionError("the response must not wait for this result")

    monkeypatch.setattr(host, "LOCAL_WORK_SECONDS", 0.05)
    monkeypatch.setattr(openapi_import, "inventory", slow)
    headers = pair(browser)
    response = browser.post(
        "/studio/local/openapi/inventory", json=ENDPOINTS["/studio/local/openapi/inventory"], headers=headers
    )
    assert response.status_code == 503 and response.json()["code"] == "WV-STUDIO-BUSY"


def test_abandoned_analyses_hold_their_slot_until_they_finish(browser, monkeypatch):
    import threading

    from firefly_weave.sdk import openapi_import
    from firefly_weave.studio import host

    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(5)
        raise AssertionError("abandoned")

    monkeypatch.setattr(host, "LOCAL_WORK_SECONDS", 0.05)
    monkeypatch.setattr(openapi_import, "inventory", stuck)
    headers = pair(browser)
    payload = ENDPOINTS["/studio/local/openapi/inventory"]
    try:
        for _ in range(host.LOCAL_WORKERS):
            assert browser.post("/studio/local/openapi/inventory", json=payload, headers=headers).status_code == 503
        busy = browser.post("/studio/local/http-action", json=ENDPOINTS["/studio/local/http-action"], headers=headers)
        assert busy.status_code == 503 and "earlier document" in busy.json()["message"]
    finally:
        release.set()
    for _ in range(100):
        if host._LOCAL_WORK.running == 0:
            break
        threading.Event().wait(0.02)
    assert host._LOCAL_WORK.running == 0
    monkeypatch.setattr(host, "LOCAL_WORK_SECONDS", 30)
    ok = browser.post("/studio/local/http-action", json=ENDPOINTS["/studio/local/http-action"], headers=headers)
    assert ok.status_code == 200
