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

"""Documentation opt-in never weakens the API bearer authentication boundary."""

import json
import re

import pytest
from httpx import ASGITransport, AsyncClient
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from starlette.applications import Starlette

from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService, VerifierSet
from firefly_weave.app import make_app
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.settings import Settings


@pytest.mark.parametrize("enabled", [False, True])
async def test_native_app_mounts_docs_only_when_opted_in(enabled):
    settings = Settings(database_url="postgresql+asyncpg://user@localhost:1234/db", docs_enabled=enabled)
    app = make_app(settings)
    try:
        paths = {route.path for route in app.routes}
        assert ("/docs" in paths) is enabled
        assert ("/openapi.json" in paths) is enabled
        assert "/actuator/health" not in paths
    finally:
        await app.state.resources.close()


@pytest.mark.parametrize("raw,expected", [(None, False), ("true", True), ("false", False)])
def test_docs_env_is_explicit_opt_in(monkeypatch, raw, expected):
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://user@localhost:1234/db")
    if raw is None:
        monkeypatch.delenv("WEAVE_DOCS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WEAVE_DOCS_ENABLED", raw)
    assert Settings.from_env().docs_enabled is expected


def test_docs_env_rejects_invalid_toggle(monkeypatch):
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://user@localhost:1234/db")
    monkeypatch.setenv("WEAVE_DOCS_ENABLED", "yes")
    with pytest.raises(ValueError, match="WEAVE_DOCS_ENABLED"):
        Settings.from_env()


@pytest.mark.parametrize("enabled", [False, True])
async def test_documentation_anonymous_access_is_exact_path_and_method(enabled):
    from firefly_weave.api.documentation import install_documentation

    app = Starlette()
    if enabled:
        install_documentation(app)
    # Empty trusted verifier set makes authentication fail without database or OIDC IO.
    authentication = AuthenticationService(VerifierSet(()), None)
    app.add_middleware(WebFilterChainMiddleware, filters=[AuthenticationFilter(authentication)])
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        for path in ("/docs", "/openapi.json"):
            for method in ("GET", "HEAD"):
                response = await client.request(method, path)
                assert response.status_code == (200 if enabled else 401)
        for method, path in (
            ("POST", "/docs"),
            ("POST", "/openapi.json"),
            ("GET", "/docs/private"),
            ("GET", "/docs/"),
            ("GET", "/openapi.json/private"),
            ("GET", "/admin/tenants"),
            ("GET", "/api/v1/tenants/tenant/projects/project/catalog"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"


async def test_served_contract_has_canonical_inventory_and_safe_swagger_configuration():
    from firefly_weave.api.documentation import install_documentation

    app = Starlette()
    install_documentation(app)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        response = await client.get("/openapi.json")
        spec = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert {(path, method.upper()) for path, methods in spec["paths"].items() for method in methods} == {
            (operation.canonical_path, operation.method) for operation in OPERATIONS.values()
        }
        assert spec["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
        page = await client.get("/docs")
        config = json.loads(
            re.search(r'<script id="weave-docs-config" type="application/json">(.*?)</script>', page.text).group(1)
        )
        assert config["url"] == "/openapi.json"
        assert config["persistAuthorization"] is False
        assert config["validatorUrl"] is None
        assert config["queryConfigEnabled"] is False
        assert config["tryItOutEnabled"] is False
        assert config["withCredentials"] is False
        assert config["defaultModelsExpandDepth"] == 0
        assert config["defaultModelRendering"] == "model"
        assert 'integrity="sha384-' in page.text


async def test_swagger_uses_proxy_mount_prefix_for_schema():
    from firefly_weave.api.documentation import install_documentation

    app = Starlette()
    install_documentation(app)
    async with AsyncClient(transport=ASGITransport(app, root_path="/weave"), base_url="http://localhost") as client:
        page = await client.get("/docs")
        config = json.loads(
            re.search(r'<script id="weave-docs-config" type="application/json">(.*?)</script>', page.text).group(1)
        )
        assert config["url"] == "/weave/openapi.json"
        spec = (await client.get("/openapi.json")).json()
        assert spec["servers"] == [{"url": "/weave"}]


async def test_api_explorer_header_carries_no_mascot():
    from firefly_weave.api.documentation import install_documentation

    app = Starlette()
    install_documentation(app)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        page = (await client.get("/docs")).text
    assert "lumi" not in page.lower()
    assert "<svg" not in page
    assert "<header><h1>Firefly Weave · API explorer</h1>" in page


async def test_api_explorer_header_is_charcoal_with_amber_links_above_a_paper_body():
    from firefly_weave.api.documentation import install_documentation

    app = Starlette()
    install_documentation(app)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        page = (await client.get("/docs")).text
    style = re.search(r"<style>(.*?)</style>", page, re.DOTALL).group(1)
    rules = {selector.strip(): body for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", style)}
    assert "background: #F3F1EB" in rules["body"]
    assert "background: #10110F" in rules["header"] and "color: #F3F1EB" in rules["header"]
    assert "color: #FFB34A" in rules["header a"]
    for legacy in ("#EEF4F0", "#173D34", "#8EE3DC"):
        assert legacy.lower() not in page.lower()
