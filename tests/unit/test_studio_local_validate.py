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

"""The Studio host's local Validate runs offline flow and type analysis through ``validate_authoring``."""

import json
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from firefly_weave.compiler.api import validate_authoring, validate_source
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


def workflow(steps: list[dict]) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "w", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}}},
            "outputSchema": {"type": "object"},
            "steps": steps,
            "output": {"literal": {}},
        },
    }


BRANCH_SCOPING = workflow(
    [
        {
            "id": "switch-1",
            "kind": "switch",
            "cases": [
                {
                    "when": {"literal": True},
                    "steps": [{"id": "b", "kind": "transform", "value": {"literal": {"x": 1}}}],
                    "output": {"literal": {}},
                }
            ],
            "default": {
                "steps": [{"id": "c", "kind": "transform", "value": {"ref": "/steps/b/output"}}],
                "output": {"literal": {}},
            },
        },
        {"id": "d", "kind": "transform", "value": {"ref": "/steps/b/output"}},
    ]
)
PLACEHOLDER = workflow([{"id": "action-1", "kind": "action", "uses": "your-action@1.0.0", "with": {"literal": {}}}])


def test_local_validate_reports_flow_errors_the_partial_check_missed(tmp_path):
    source = yaml.safe_dump(BRANCH_SCOPING, sort_keys=False)
    with client(tmp_path) as browser:
        headers = pair(browser)
        response = browser.post(
            "/studio/local/validate",
            json={"source": source, "format": "yaml", "filename": "draft.workflow.yaml"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    # Exactly the library result: no host-side reshaping of diagnostics.
    assert response.content == validate_authoring(source, format="yaml", filename="draft.workflow.yaml").to_bytes()
    body = response.json()
    assert body["validationOk"] is False and body["ok"] is False
    assert body["partial"] is True and body["artifact"] is None
    assert [(d["code"], d["path"]) for d in body["diagnostics"]] == [
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/0/default/steps/0/value/ref"),
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/1/value/ref"),
    ]
    assert all(d["source"]["file"] == "draft.workflow.yaml" for d in body["diagnostics"])
    # The CLI's partial entry point is untouched and still passes this document.
    assert validate_source(source, format="yaml").validation_ok


def test_local_validate_marks_catalog_references_pending_not_failed(tmp_path):
    with client(tmp_path) as browser:
        headers = pair(browser)
        response = browser.post(
            "/studio/local/validate", json={"source": json.dumps(PLACEHOLDER), "format": "json"}, headers=headers
        )
    body = response.json()
    assert response.status_code == 200
    assert body["validationOk"] is True and body["errorCount"] == 0 and body["ok"] is False
    assert [(d["code"], d["severity"], d["path"]) for d in body["diagnostics"]] == [
        ("WV-COMP-CATALOG_PENDING", "info", "/spec/steps/0/uses")
    ]


def test_local_validate_analyzes_off_the_event_loop_with_identical_bytes(tmp_path, monkeypatch):
    # Flow and type analysis of a bounded document can take seconds; it must never stall the host.
    import asyncio

    from firefly_weave.studio import host

    real = host.validate_authoring
    seen: list[str] = []

    def probe(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append("worker thread")
        else:
            seen.append("event loop")
        return real(*args, **kwargs)

    monkeypatch.setattr(host, "validate_authoring", probe)
    many = workflow([{"id": f"a{i}", "kind": "action", "uses": "x@1.0.0", "with": {"literal": {}}} for i in range(130)])
    documents = [
        (yaml.safe_dump(BRANCH_SCOPING, sort_keys=False), "yaml", "flows/branch.workflow.yaml"),
        (json.dumps(PLACEHOLDER), "json", None),
        (json.dumps(many), "json", "many.json"),
        ("not: [valid", "yaml", "broken.yaml"),
    ]
    with client(tmp_path) as browser:
        headers = pair(browser)
        for source, source_format, filename in documents:
            payload = {"source": source, "format": source_format}
            if filename is not None:
                payload["filename"] = filename
            response = browser.post("/studio/local/validate", json=payload, headers=headers)
            assert response.status_code == 200
            assert response.content == real(source, format=source_format, filename=filename).to_bytes()
    assert seen == ["worker thread"] * len(documents)
    assert json.loads(response.content)["validationOk"] is False


def test_slow_local_validate_answers_busy_and_frees_its_slot(tmp_path, monkeypatch):
    import threading

    from firefly_weave.studio import host

    real = host.validate_authoring
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(host, "LOCAL_WORK_SECONDS", 0.05)
    monkeypatch.setattr(host, "validate_authoring", stuck)
    payload = {"source": json.dumps(PLACEHOLDER), "format": "json"}
    with client(tmp_path) as browser:
        headers = pair(browser)
        try:
            busy = browser.post("/studio/local/validate", json=payload, headers=headers)
        finally:
            release.set()
        assert busy.status_code == 503 and busy.json()["code"] == "WV-STUDIO-BUSY"
        # The wording fits a definition check, not the OpenAPI import's operation selection.
        assert "definition" in busy.json()["message"] and "operations" not in busy.json()["message"]
        for _ in range(250):
            if host._LOCAL_WORK.running == 0:
                break
            threading.Event().wait(0.02)
        assert host._LOCAL_WORK.running == 0
        monkeypatch.setattr(host, "LOCAL_WORK_SECONDS", 30)
        assert browser.post("/studio/local/validate", json=payload, headers=headers).status_code == 200


def test_local_validate_keeps_its_pairing_and_request_bounds(tmp_path):
    payload = {"source": json.dumps(BRANCH_SCOPING), "format": "json"}
    with client(tmp_path) as browser:
        assert browser.post("/studio/local/validate", json=payload, headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        assert browser.post("/studio/local/validate", json=payload, headers={"Origin": ORIGIN}).status_code == 403
        assert (
            browser.post("/studio/local/validate", json={**payload, "format": "xml"}, headers=headers).status_code
            == 422
        )
        assert browser.post("/studio/local/validate", json=payload, headers=headers).status_code == 200


def test_local_llm_contract_is_canonical_and_requires_paired_session(tmp_path):
    from firefly_weave.contracts.llm import LLMProfile

    with client(tmp_path) as browser:
        assert browser.get("/studio/contracts/llm-profile").status_code == 401
        pair(browser)
        response = browser.get("/studio/contracts/llm-profile")
        assert response.status_code == 200
        assert response.json() == LLMProfile.model_json_schema(by_alias=True)
        assert response.headers["cache-control"] == "no-store"


def test_file_and_lumi_contracts_require_pairing_and_match_canonical_models(tmp_path):
    from firefly_weave.contracts.files import file_reference_schema
    from firefly_weave.contracts.lumi import LumiConfigurationRequest

    with client(tmp_path) as browser:
        paths = {
            "/studio/contracts/file-reference": file_reference_schema(),
            "/studio/contracts/lumi-configuration": LumiConfigurationRequest.model_json_schema(by_alias=True),
        }
        for path in paths:
            assert browser.get(path).status_code == 401
        pair(browser)
        for path, schema in paths.items():
            response = browser.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == schema
