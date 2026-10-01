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

"""Canonical aliases exercise the same native services and current database grants."""

import json
from uuid import UUID, uuid4

import pytest
from test_definitions import author as author

pytestmark = pytest.mark.integration


async def test_native_compiler_explicit_lock_and_partial_validation(author, headers, project_url):
    from firefly_weave.compiler.api import compile_source, validate_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    client = author[0]
    source = "kind: Unknown\n"
    payload = {
        "source": source,
        "format": "yaml",
        "filename": "editor.yaml",
        "catalog": {"definitions": [], "tasks": [], "adapters": [], "schemas": {}},
    }
    local = compile_source(source, format="yaml", filename="editor.yaml", catalog=CatalogSnapshot.empty())
    for root in (project_url, "/api/v1" + project_url):
        response = await client.post(root + "/compiler/compile", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json() == json.loads(local.to_bytes())
        partial = await client.post(
            root + "/compiler/validate", headers=headers, json={k: v for k, v in payload.items() if k != "catalog"}
        )
        assert partial.json() == json.loads(validate_source(source, format="yaml", filename="editor.yaml").to_bytes())


async def test_alias_denial_problem_and_safe_exceptions(author, headers, other_headers, project_url):
    client = author[0]
    for root in (project_url, "/api/v1" + project_url):
        response = await client.get(root + "/drafts")
        assert response.status_code == 401
        assert response.json()["status"] == 401
        assert response.json()["request_id"] == response.headers["X-Weave-Request-ID"]
        assert response.json()["diagnostics"] == []
        denied = await client.get(root + "/drafts", headers=other_headers)
        assert denied.status_code == 403
    for path in (
        "/api/v1/health/live",
        "/api/v1/webhooks/" + str(uuid4()),
        "/api/v1/api/v1" + project_url + "/drafts",
        "/admin/tenants",
        "/admin/grants",
    ):
        assert (await client.post(path)).status_code == 401


async def test_draft_zero_cursor_retirement_and_stale_etag(author, headers, project_url):
    client = author[0]
    root = "/api/v1" + project_url
    for identifier in (UUID(int=0), uuid4()):
        created = await client.put(root + "/drafts/" + str(identifier), headers=headers, json={"document": {}})
        assert created.status_code == 201, created.text
    first = (await client.get(root + "/drafts?limit=1", headers=headers)).json()
    assert first["items"][0]["id"] == str(UUID(int=0))
    assert first["next_cursor"] and first["next_cursor"] != str(UUID(int=0))
    second = await client.get(root + "/drafts", headers=headers, params={"cursor": first["next_cursor"], "limit": 1})
    assert len(second.json()["items"]) == 1
    url = root + "/drafts/" + str(UUID(int=0))
    retired = await client.delete(url, headers={**headers, "If-Match": '"1"'})
    assert retired.status_code == 200, retired.text
    assert retired.json()["retired"] and retired.headers["etag"] == '"2"'
    assert (await client.delete(url, headers={**headers, "If-Match": '"1"'})).status_code == 412
    assert (await client.put(url, headers={**headers, "If-Match": '"1"'}, json={"document": {}})).status_code == 409
    retained = (await client.get(url + "/export", headers=headers)).json()
    assert retained["revisions"][0]["retired"]
    assert retained["revisions"][0]["document"] == {}
    assert all(
        v["id"] != str(UUID(int=0)) for v in (await client.get(root + "/drafts", headers=headers)).json()["items"]
    )


async def test_discovery_lists_use_existing_scope_capabilities(
    author, headers, other_headers, env_url, access_db, provisioned
):
    from firefly_weave.access.models import Grant

    client, actor, scope = author
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="operator", scope=scope))
    for name in ("connections", "runs", "workers", "worker-releases", "triggers", "incidents"):
        root = "/api/v1" + env_url + "/" + name
        response = await client.get(root, headers=headers)
        assert response.status_code == 200, (name, response.text)
        assert response.json() == {"items": [], "next_cursor": None}
        assert (await client.get(root, headers=other_headers)).status_code == 403
        assert (await client.get(root, headers=headers, params={"limit": 101})).status_code == 422


async def test_canonical_schedule_history_is_bounded_page(author, headers, env_url):
    client = author[0]
    canonical = "/api/v1" + env_url + "/schedules/" + str(uuid4()) + "/occurrences"
    response = await client.get(canonical, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert (await client.get(canonical, headers=headers, params={"limit": 101})).status_code == 422
    assert (await client.get(canonical.removeprefix("/api/v1"), headers=headers)).json() == []


async def test_exact_native_operation_alias_identity_and_administrative_denial(author, headers):
    from firefly_weave.contracts.surface import OPERATIONS

    app = author[0]._transport.app
    assert app.state.pyfly.context
    named = {route.name: route for route in app.routes}
    for identifier, operation in OPERATIONS.items():
        canonical = named[identifier]
        assert canonical.path == operation.canonical_path
        assert operation.method in canonical.methods
        if operation.path.startswith("/tenants/"):
            legacy = named["legacy." + identifier]
            assert legacy.endpoint is canonical.endpoint
            values = {name: uuid4() for name in canonical.param_convertors}
            assert str(app.url_path_for(identifier, **values)).startswith("/api/v1/tenants/")
    for path in ("/admin/tenants", "/admin/grants"):
        response = await author[0].post(
            path,
            headers=headers,
            json={"name": "forbidden"}
            if path.endswith("tenants")
            else {"principal_id": str(author[1].id), "grant": {"role": "platform_admin", "scope": None}},
        )
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["request_id"] == response.headers["X-Weave-Request-ID"]


@pytest.mark.parametrize(
    "source",
    [
        "apiVersion: weave/v1alpha1\nkind: Workflow\nmetadata: {name: partial, version: 1.0.0}\n"
        "spec: {inputSchema: {}, outputSchema: {}, steps: [], output: {literal: null}}\n",
        "kind: Unknown\n",
    ],
)
async def test_strict_without_catalog_keeps_canonical_partial_validation(
    author, headers, project_url, tmp_path, source
):
    from click.testing import CliRunner
    from httpx import ASGITransport

    from firefly_weave.cli.main import cli
    from firefly_weave.compiler.api import validate_source
    from firefly_weave.sdk.client import WeaveClient

    path = tmp_path / "editor.yaml"
    path.write_text(source)
    expected = validate_source(source, format="yaml", filename=str(path))
    offline = CliRunner().invoke(cli, ["workflow", "validate", str(path), "--strict", "--output", "json"])
    assert offline.exit_code == (0 if expected.validation_ok else 1)
    assert json.loads(offline.stdout) == json.loads(expected.to_bytes())
    assert expected.partial and expected.artifact is None and not expected.ok
    for root in (project_url, "/api/v1" + project_url):
        response = await author[0].post(
            root + "/compiler/validate",
            headers=headers,
            json={"source": source, "format": "yaml", "filename": str(path), "strict": True},
        )
        assert response.status_code == 200
        assert response.json() == json.loads(expected.to_bytes())
    async with WeaveClient(
        "http://localhost",
        lambda: headers["Authorization"].split(" ", 1)[1],
        author[2],
        transport=ASGITransport(author[0]._transport.app),
    ) as sdk:
        result = await sdk.validate(source=source, format="yaml", filename=str(path), strict=True)
    assert result.to_bytes() == expected.to_bytes()
