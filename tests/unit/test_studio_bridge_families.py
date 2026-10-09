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

"""The Studio host bridge admits drafts, Simulate, release reads and connector discovery only."""

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions, StudioProfile

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
TENANT = "00000000-0000-0000-0000-000000000001"
PROJECT_ID = "00000000-0000-0000-0000-000000000002"
ENVIRONMENT_ID = "00000000-0000-0000-0000-000000000003"
RESOURCE = "00000000-0000-0000-0000-0000000000aa"
PROJECT = f"/api/v1/tenants/{TENANT}/projects/{PROJECT_ID}"
ENVIRONMENT = f"{PROJECT}/environments/{ENVIRONMENT_ID}"
OTHER_PROJECT = f"/api/v1/tenants/{TENANT}/projects/00000000-0000-0000-0000-000000000009"


def client(tmp_path: Path, **options):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    profile = StudioProfile(
        name="Dev",
        base_url="https://api.example",
        tenant_id=TENANT,
        project_id=PROJECT_ID,
        environment_id=ENVIRONMENT_ID,
    )
    return TestClient(
        make_studio_app(
            StudioOptions(
                origin=ORIGIN, assets=assets, pairing_code=CODE, profile=profile, token_provider=lambda: "t", **options
            )
        ),
        base_url=ORIGIN,
    )


def pair(browser):
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", f"{PROJECT}/drafts/{RESOURCE}"),
        ("DELETE", f"{PROJECT}/drafts/{RESOURCE}"),
        ("POST", f"{PROJECT}/debug/sessions"),
        ("GET", f"{PROJECT}/debug/sessions/{RESOURCE}"),
        ("POST", f"{PROJECT}/debug/sessions/{RESOURCE}/commands"),
        ("GET", f"{ENVIRONMENT}/worker-releases"),
        ("GET", f"{ENVIRONMENT}/worker-releases/{RESOURCE}"),
        ("GET", f"{PROJECT}/connector-descriptors"),
        ("GET", f"{PROJECT}/connector-descriptors/weave-http-v2"),
        ("GET", f"{PROJECT}/connector-descriptors/Pets_API.v1"),
        ("GET", f"{PROJECT}/drafts"),
        ("GET", f"{PROJECT}/workflows/{RESOURCE}"),
        ("GET", f"{PROJECT}/language"),
        ("GET", f"{ENVIRONMENT}/run-summaries"),
        ("GET", f"{ENVIRONMENT}/runs/{RESOURCE}/steps"),
        ("GET", f"{ENVIRONMENT}/runs/{RESOURCE}/logs"),
        ("POST", f"{ENVIRONMENT}/ai/connections/{RESOURCE}/test"),
    ],
)
def test_studio_journeys_are_bridged(tmp_path, method, path):
    with client(tmp_path) as browser:
        assert browser.app.state.studio.check_scope(path, method) == 200


@pytest.mark.parametrize(
    "method,path,status",
    [
        # Releases stay read-only: admitting image identities is an operator step.
        ("POST", f"{ENVIRONMENT}/worker-releases", 404),
        # Generic grants, bootstrap and worker credential leases never cross the bridge.
        ("POST", "/admin/grants", 404),
        ("POST", "/api/v1/admin/grants", 404),
        ("POST", "/api/v1/admin/bootstrap", 404),
        ("POST", f"{ENVIRONMENT}/tasks/credentials", 404),
        # Adapter names are bounded and cannot smuggle encodings or other operations.
        ("GET", f"{PROJECT}/connector-descriptors/-leading-dash", 404),
        ("GET", f"{PROJECT}/connector-descriptors/" + "a" * 129, 404),
        ("GET", f"{PROJECT}/connector-descriptors/weave http", 404),
        ("GET", f"{PROJECT}/connector-descriptors/weave-http-v2/extra", 404),
        ("POST", f"{PROJECT}/connector-descriptors", 404),
        ("PUT", f"{PROJECT}/drafts/not-a-uuid", 404),
        ("GET", f"{PROJECT}/unknown-collection", 404),
        ("DELETE", f"{PROJECT}/workflows/{RESOURCE}", 404),
        # Every new family keeps the exact profile scope.
        ("PUT", f"{OTHER_PROJECT}/drafts/{RESOURCE}", 403),
        ("POST", f"{OTHER_PROJECT}/debug/sessions", 403),
        ("GET", f"{OTHER_PROJECT}/connector-descriptors", 403),
        ("GET", f"{OTHER_PROJECT}/connector-descriptors/weave-http-v2", 403),
        ("GET", f"{OTHER_PROJECT}/language", 403),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/worker-releases", 403),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/run-summaries", 403),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + f"/runs/{RESOURCE}/steps", 403),
        ("POST", f"{ENVIRONMENT}/run-summaries", 404),
        ("GET", f"{ENVIRONMENT}/runs/not-a-uuid/logs", 404),
        ("POST", ENVIRONMENT.replace("000000000003", "000000000009") + f"/ai/connections/{RESOURCE}/test", 403),
        ("GET", f"{ENVIRONMENT}/ai/connections/{RESOURCE}/test", 404),
    ],
)
def test_bridge_boundaries_hold(tmp_path, method, path, status):
    with client(tmp_path) as browser:
        assert browser.app.state.studio.check_scope(path, method) == status


def test_draft_save_and_simulate_forward_preconditions_through_the_real_host(tmp_path):
    seen = []

    async def upstream(request):
        seen.append((request.method, request.url.path, request.headers.get("if-match"), request.content))
        return httpx.Response(200, json={"ok": True}, headers={"ETag": '"2"'})

    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        headers = pair(browser)
        saved = browser.put(
            f"/studio/api{PROJECT}/drafts/{RESOURCE}",
            json={"document": {}},
            headers={**headers, "If-Match": '"1"'},
        )
        assert saved.status_code == 200 and saved.headers["etag"] == '"2"'
        assert (
            browser.delete(
                f"/studio/api{PROJECT}/drafts/{RESOURCE}", headers={**headers, "If-Match": '"2"'}
            ).status_code
            == 200
        )
        assert browser.post(f"/studio/api{PROJECT}/debug/sessions", json={}, headers=headers).status_code == 200
        assert (
            browser.get(f"/studio/api{PROJECT}/connector-descriptors/weave-http-v2", headers=headers).status_code == 200
        )
        assert browser.post(f"/studio/api{ENVIRONMENT}/worker-releases", json={}, headers=headers).status_code == 404
    assert [(method, path, match) for method, path, match, _ in seen] == [
        ("PUT", f"{PROJECT}/drafts/{RESOURCE}", '"1"'),
        ("DELETE", f"{PROJECT}/drafts/{RESOURCE}", '"2"'),
        ("POST", f"{PROJECT}/debug/sessions", None),
        ("GET", f"{PROJECT}/connector-descriptors/weave-http-v2", None),
    ]


def test_every_operation_crosses_the_bridge_exactly_when_its_family_or_id_is_admitted(tmp_path):
    import re

    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.studio.service import STUDIO_FAMILIES, STUDIO_OPERATIONS

    values = {
        "tenant": TENANT,
        "project": PROJECT_ID,
        "environment": ENVIRONMENT_ID,
        "collection": "workflows",
        "adapter": "weave-http-v2",
    }
    with client(tmp_path) as browser:
        studio = browser.app.state.studio
        crossing = set()
        for identifier, operation in OPERATIONS.items():
            path = re.sub(r"\{([^}]+)\}", lambda match: values.get(match.group(1), RESOURCE), operation.canonical_path)
            if studio.check_scope(path, operation.method) == 200:
                crossing.add(identifier)
    # Trying the next template on a non-matching segment never admits an operation by accident.
    assert crossing == {
        identifier
        for identifier in OPERATIONS
        if identifier.split(".")[0] in STUDIO_FAMILIES or identifier in STUDIO_OPERATIONS
    }
    assert {"releases.create", "admin.grant", "admin.tenant"}.isdisjoint(crossing)


@pytest.mark.parametrize(
    "method,suffix",
    [("GET", "status"), ("GET", "configuration"), ("PUT", "configuration"), ("POST", "ask")],
)
@pytest.mark.parametrize("upstream_status", [200, 403])
def test_lumi_operations_keep_host_scope_csrf_and_upstream_authorization(tmp_path, method, suffix, upstream_status):
    seen = []

    async def upstream(request):
        seen.append(
            (request.method, request.url.path, request.headers["authorization"], request.headers.get("if-match"))
        )
        return httpx.Response(upstream_status, json={"ok": upstream_status == 200})

    path = f"{ENVIRONMENT}/lumi/{suffix}"
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        assert browser.request(method, f"/studio/api{path}", headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        if method != "GET":
            assert browser.request(method, f"/studio/api{path}", headers={"Origin": ORIGIN}).status_code == 403
        response = browser.request(method, f"/studio/api{path}", headers={**headers, "If-Match": '"3"'})
        assert response.status_code == upstream_status
        assert (
            browser.request(method, f"/studio/api{path.replace(ENVIRONMENT_ID, RESOURCE)}", headers=headers).status_code
            == 403
        )
        assert browser.request(method, f"/studio/api{path}/extra", headers=headers).status_code == 404
        assert browser.delete(f"/studio/api{path}", headers=headers).status_code == 404
        assert browser.post(f"/studio/api{ENVIRONMENT}/lumi/arbitrary-provider", headers=headers).status_code == 404
    assert seen == [(method, path, "Bearer t", '"3"')]
