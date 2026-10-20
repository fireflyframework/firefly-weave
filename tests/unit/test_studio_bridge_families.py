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

import hashlib
import json
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
        ("GET", f"{ENVIRONMENT}/bindable-connections"),
        ("GET", f"{ENVIRONMENT}/run-summaries"),
        ("GET", f"{ENVIRONMENT}/runs/{RESOURCE}/steps"),
        ("GET", f"{ENVIRONMENT}/runs/{RESOURCE}/logs"),
        ("POST", f"{ENVIRONMENT}/ai/connections/{RESOURCE}/test"),
        ("GET", f"{ENVIRONMENT}/ai/endpoints"),
        ("GET", f"{ENVIRONMENT}/ai/models"),
        ("GET", f"{ENVIRONMENT}/ai/readiness"),
        ("POST", f"{ENVIRONMENT}/ai/setup/publish"),
        ("POST", f"{ENVIRONMENT}/ai/setup/grant"),
        ("GET", f"{ENVIRONMENT}/operations/summary"),
        ("GET", f"{PROJECT}/operations/telemetry"),
        ("GET", f"{ENVIRONMENT}/worker-releases/{RESOURCE}/usage"),
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
        ("POST", f"{ENVIRONMENT}/bindable-connections", 404),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/bindable-connections", 403),
        ("GET", f"{OTHER_PROJECT}/environments/{ENVIRONMENT_ID}/bindable-connections", 403),
        ("GET", f"{ENVIRONMENT}/runs/not-a-uuid/logs", 404),
        ("POST", ENVIRONMENT.replace("000000000003", "000000000009") + f"/ai/connections/{RESOURCE}/test", 403),
        ("GET", f"{ENVIRONMENT}/ai/connections/{RESOURCE}/test", 404),
        ("POST", f"{ENVIRONMENT}/ai/models", 404),
        ("POST", f"{ENVIRONMENT}/ai/readiness", 404),
        ("GET", f"{ENVIRONMENT}/ai/setup/publish", 404),
        ("GET", f"{ENVIRONMENT}/ai/setup/grant", 404),
        ("POST", f"{ENVIRONMENT}/ai/setup/publish/extra", 404),
        ("POST", f"{ENVIRONMENT}/ai/setup/grant/extra", 404),
        ("POST", ENVIRONMENT.replace("000000000003", "000000000009") + "/ai/setup/grant", 403),
        ("POST", f"{OTHER_PROJECT}/environments/{ENVIRONMENT_ID}/ai/setup/publish", 403),
        ("POST", f"{ENVIRONMENT}/workers/grants", 404),
        ("GET", f"{ENVIRONMENT}/ai/readiness/extra", 404),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/ai/readiness", 403),
        ("POST", f"{ENVIRONMENT}/ai/endpoints", 404),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/ai/models", 403),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/ai/endpoints", 403),
        # The summary, the telemetry settings and release usage are reads of the paired scope only.
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + "/operations/summary", 403),
        ("GET", f"{OTHER_PROJECT}/operations/telemetry", 403),
        ("GET", ENVIRONMENT.replace("000000000003", "000000000009") + f"/worker-releases/{RESOURCE}/usage", 403),
        ("POST", f"{ENVIRONMENT}/operations/summary", 404),
        ("PUT", f"{PROJECT}/operations/telemetry", 404),
        ("POST", f"{ENVIRONMENT}/worker-releases/{RESOURCE}/usage", 404),
        ("GET", f"{ENVIRONMENT}/worker-releases/not-a-uuid/usage", 404),
        ("GET", f"{PROJECT}/operations/compatibility/summary", 404),
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
    # The AI setup operations add exactly these six, direct incident reads join the incidents
    # family, stale-worker retirement joins the workers family, and the environment summary,
    # telemetry settings and release usage join as reads, and change requests join as their own family; every
    # earlier crossing stays as it was.
    approved_additions = {
        "ai_endpoints.list",
        "ai_models.list",
        "ai_readiness.read",
        "ai_setup.publish",
        "ai_setup.grant",
        "bindable_connections.list",
        "incidents.read",
        "workers.retire_stale",
        "operations_summary.read",
        "operations_telemetry.read",
        "releases.usage",
        "runs.calls",
        *(
            "deployment_change_requests." + verb
            for verb in (
                "create",
                "list",
                "read",
                "advance",
                "approve",
                "drain",
                "apply",
                "replan",
                "decline",
                "cancel",
            )
        ),
    }
    assert approved_additions <= crossing
    preserved = crossing - approved_additions
    assert len(preserved) == 131
    assert hashlib.sha256("\n".join(sorted(preserved)).encode()).hexdigest() == (
        "3ba280e1522358905e2cdb2e4bf66976fdc988018cdc08742c4956b87d1ab159"
    )
    assert {"workers.grant", "ai_connections.test"} <= preserved
    assert {"releases.create", "tasks.credentials", "admin.grant", "admin.tenant"}.isdisjoint(crossing)


def test_releases_and_summary_reads_cross_by_this_literal_set(tmp_path):
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.studio.service import STUDIO_FAMILIES, STUDIO_OPERATIONS

    # Releases cross as three named reads, never as a family: registering an image stays an operator step.
    assert {"releases.list", "releases.read", "releases.usage"} == STUDIO_OPERATIONS
    assert "releases" not in STUDIO_FAMILIES and "tasks" not in STUDIO_FAMILIES
    assert {"operations_summary", "operations_telemetry"} <= STUDIO_FAMILIES
    with client(tmp_path) as browser:
        studio = browser.app.state.studio
        crossing = {
            identifier
            for identifier, operation in OPERATIONS.items()
            if identifier.startswith(("releases.", "operations_"))
            and studio.check_scope(
                operation.canonical_path.format(
                    tenant=TENANT, project=PROJECT_ID, environment=ENVIRONMENT_ID, identifier=RESOURCE
                ),
                operation.method,
            )
            == 200
        }
    assert crossing == {
        "releases.list",
        "releases.read",
        "releases.usage",
        "operations_summary.read",
        "operations_telemetry.read",
    }


@pytest.mark.parametrize("upstream_status", [200, 403])
def test_release_usage_and_summary_reads_keep_scope_method_and_upstream_authority(tmp_path, upstream_status):
    seen = []

    async def upstream(request):
        seen.append((request.method, request.url.path, request.url.query.decode(), request.headers["authorization"]))
        return httpx.Response(upstream_status, json={"code": "WV-FORBIDDEN"} if upstream_status == 403 else {})

    paths = (
        (f"{ENVIRONMENT}/worker-releases/{RESOURCE}/usage", "limit=10"),
        (f"{ENVIRONMENT}/operations/summary", ""),
        (f"{PROJECT}/operations/telemetry", ""),
    )
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        targets = [f"/studio/api{path}" + (f"?{query}" if query else "") for path, query in paths]
        # Nothing crosses before pairing.
        assert {browser.get(target, headers={"Origin": ORIGIN}).status_code for target in targets} == {401}
        headers = pair(browser)
        for (path, _), target in zip(paths, targets, strict=True):
            assert browser.get(target, headers=headers).status_code == upstream_status
            assert browser.post(f"/studio/api{path}", json={}, headers=headers).status_code == 404
            assert browser.delete(f"/studio/api{path}", headers=headers).status_code == 404
        assert (
            browser.get(f"/studio/api{OTHER_ENVIRONMENT}/worker-releases/{RESOURCE}/usage", headers=headers).status_code
            == 403
        )
        assert browser.get(f"/studio/api{OTHER_ENVIRONMENT}/operations/summary", headers=headers).status_code == 403
        assert browser.get(f"/studio/api{OTHER_PROJECT}/operations/telemetry", headers=headers).status_code == 403
        # Registering a release image still never crosses.
        assert browser.post(f"/studio/api{ENVIRONMENT}/worker-releases", json={}, headers=headers).status_code == 404
    assert seen == [("GET", path, query, "Bearer t") for path, query in paths]


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


@pytest.mark.parametrize("action", ["publish", "grant"])
@pytest.mark.parametrize("upstream_status", [200, 403])
def test_setup_bridge_preserves_csrf_scope_and_upstream_authority(tmp_path, action, upstream_status):
    seen = []
    body = {} if action == "publish" else {"connection_revision_id": RESOURCE}

    async def upstream(request):
        seen.append((request.method, request.url.path, request.headers["authorization"], request.content))
        return httpx.Response(upstream_status, json={"ok": upstream_status == 200})

    path = f"{ENVIRONMENT}/ai/setup/{action}"
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        assert browser.post(f"/studio/api{path}", json=body, headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        assert browser.post(f"/studio/api{path}", json=body, headers={"Origin": ORIGIN}).status_code == 403
        response = browser.post(f"/studio/api{path}", json=body, headers=headers)
        assert response.status_code == upstream_status
        assert (
            browser.post(f"/studio/api{path.replace(ENVIRONMENT_ID, RESOURCE)}", json=body, headers=headers).status_code
            == 403
        )
    assert len(seen) == 1 and seen[0][:3] == ("POST", path, "Bearer t")
    assert json.loads(seen[0][3]) == body


OTHER_ENVIRONMENT = ENVIRONMENT.replace(ENVIRONMENT_ID, RESOURCE)


@pytest.mark.parametrize(
    "suffix,query",
    [
        ("ai/endpoints", ""),
        ("ai/models", f"connection={RESOURCE}&refresh=true"),
        ("ai/readiness", ""),
        ("bindable-connections", "connector=weave-agentic-provider%401.0.0&limit=50"),
    ],
)
@pytest.mark.parametrize("upstream_status", [200, 403])
def test_ai_reads_keep_pairing_scope_method_and_upstream_authority(tmp_path, suffix, query, upstream_status):
    seen = []

    async def upstream(request):
        seen.append((request.method, request.url.path, request.url.query.decode(), request.headers["authorization"]))
        return httpx.Response(upstream_status, json={"code": "WV-FORBIDDEN"} if upstream_status == 403 else {})

    path = f"{ENVIRONMENT}/{suffix}"
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        target = f"/studio/api{path}" + (f"?{query}" if query else "")
        assert browser.get(target, headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        response = browser.get(target, headers=headers)
        assert response.status_code == upstream_status
        assert browser.get(f"/studio/api{OTHER_ENVIRONMENT}/{suffix}", headers=headers).status_code == 403
        assert (
            browser.get(
                f"/studio/api{OTHER_PROJECT}/environments/{ENVIRONMENT_ID}/{suffix}", headers=headers
            ).status_code
            == 403
        )
        assert browser.delete(f"/studio/api{path}", headers=headers).status_code == 404
        assert browser.post(f"/studio/api{path}", json={}, headers=headers).status_code == 404
    assert seen == [("GET", path, query, "Bearer t")]


@pytest.mark.parametrize("upstream_status", [201, 403])
def test_worker_grants_still_cross_and_the_server_keeps_its_management_check(tmp_path, upstream_status):
    seen = []
    body = {"release_id": RESOURCE, "connection_revision_id": RESOURCE, "capability": "weave-agentic.generate@1.0.0"}

    async def upstream(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(upstream_status, json={"code": "WV-FORBIDDEN"} if upstream_status == 403 else {})

    path = f"{ENVIRONMENT}/worker-connection-grants"
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        assert browser.post(f"/studio/api{path}", json=body, headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        assert browser.post(f"/studio/api{path}", json=body, headers={"Origin": ORIGIN}).status_code == 403
        assert browser.post(f"/studio/api{path}", json=body, headers=headers).status_code == upstream_status
        assert (
            browser.post(
                f"/studio/api{OTHER_ENVIRONMENT}/worker-connection-grants", json=body, headers=headers
            ).status_code
            == 403
        )
    assert seen == [("POST", path, body)]


@pytest.mark.parametrize("operation", ["releases.create", "tasks.credentials", "admin.grant", "admin.tenant"])
def test_operator_and_credential_operations_never_reach_the_platform(tmp_path, operation):
    from firefly_weave.contracts.surface import OPERATIONS

    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={})

    row = OPERATIONS[operation]
    path = row.canonical_path.format(tenant=TENANT, project=PROJECT_ID, environment=ENVIRONMENT_ID)
    with client(tmp_path, transport=httpx.MockTransport(upstream)) as browser:
        headers = pair(browser)
        assert browser.app.state.studio.check_scope(path, row.method) == 404
        assert browser.request(row.method, f"/studio/api{path}", json={}, headers=headers).status_code == 404
    assert seen == []
