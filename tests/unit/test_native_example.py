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

"""Native walkthrough must declare authority before requesting connection grants."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import httpx

from firefly_weave.contracts.workers import ReleaseRequest


async def test_native_example_declares_credential_capability_before_grant(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("native_example", root / "examples/admit_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scope = {key: str(uuid4()) for key in ("tenant_id", "project_id", "environment_id")}
    receipt = tmp_path / "scope.json"
    receipt.write_text(json.dumps({"scope": scope}))
    principal = tmp_path / "principal.json"
    principal.write_text(json.dumps({"principal_id": str(uuid4())}))
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", "http://localhost:18082")
    monkeypatch.setenv("WEAVE_API_URL", "http://localhost:18083")
    monkeypatch.setenv("WEAVE_HOST_SECRET", "mock-only-credential")
    release_id = str(uuid4())
    release = None
    granted = []

    def handle(request):
        nonlocal release
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "mock-only-token"})
        payload = json.loads(request.content)
        if request.url.path.endswith("/worker-releases"):
            release = ReleaseRequest.model_validate(payload)
            return httpx.Response(201, json={"id": release_id})
        if request.url.path.endswith("/worker-connection-grants"):
            assert release is not None and payload["release_id"] == release_id
            if payload["capability"] not in release.credential_capabilities:
                return httpx.Response(403, json={"code": "WV-FORBIDDEN"})
            granted.append(payload["capability"])
        return httpx.Response(201, json={"id": str(uuid4()), "digest": "sha256:" + "b" * 64})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    output = tmp_path / "native.json"
    await module.admit(
        receipt, principal, root / "examples/host_product/http-manifest.json", "sha256:" + "a" * 64, output
    )
    result = json.loads(output.read_text())
    assert granted == result["executor"]["task_types"]
    assert list(release.credential_capabilities) == granted
    assert result["executor"]["release_id"] == release_id
