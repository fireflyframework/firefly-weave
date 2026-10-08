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

"""Local Studio boundary tests exercise real ASGI requests and compiler validation."""

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions, StudioProfile

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def client(tmp_path: Path, **options):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(
        make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE, **options)), base_url=ORIGIN
    )


def pair(browser):
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert CODE not in result.text
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


def test_pairing_origin_cookie_and_one_use(tmp_path):
    with client(tmp_path) as browser:
        assert browser.get("/studio/session").json()["paired"] is False
        assert browser.post("/studio/session", json={"code": CODE}).status_code == 403
        assert (
            browser.post("/studio/session", json={"code": CODE}, headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        headers = pair(browser)
        assert browser.get("/studio/session").json()["paired"] is True
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 403
        assert browser.delete("/studio/session", headers=headers).status_code == 204
        assert browser.get("/studio/session").json()["paired"] is False


def test_rebinding_host_and_cross_site_reads_rejected(tmp_path):
    with client(tmp_path) as browser:
        assert browser.get("/", headers={"Host": "attacker.example:8766"}).status_code == 403
        assert browser.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        response = browser.get("/")
        assert response.status_code == 200
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"


def test_real_compiler_requires_paired_csrf_and_bounds_body(tmp_path):
    with client(tmp_path) as browser:
        payload = {"source": "not: [valid", "format": "yaml"}
        assert browser.post("/studio/local/validate", json=payload, headers={"Origin": ORIGIN}).status_code == 401
        headers = pair(browser)
        assert browser.post("/studio/local/validate", json=payload, headers={"Origin": ORIGIN}).status_code == 403
        response = browser.post("/studio/local/validate", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()["validationOk"] is False
        assert response.json()["diagnostics"]
        assert (
            browser.post("/studio/local/validate", content=b"x" * (2 * 1024 * 1024 + 1), headers=headers).status_code
            == 413
        )
        assert (
            browser.post("/studio/local/validate", json={**payload, "path": "/etc/passwd"}, headers=headers).status_code
            == 422
        )


def test_bridge_allowlist_no_credentials_or_redirects(tmp_path):
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"}, headers={"Set-Cookie": "upstream=secret", "ETag": '"2"'})

    profile = StudioProfile(
        name="Development",
        base_url="https://api.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        environment_id="00000000-0000-0000-0000-000000000003",
    )
    with client(
        tmp_path, profile=profile, token_provider=lambda: "host-only-secret", transport=httpx.MockTransport(upstream)
    ) as browser:
        pair(browser)
        path = (
            "/studio/api/api/v1/tenants/00000000-0000-0000-0000-000000000001"
            "/projects/00000000-0000-0000-0000-000000000002/capabilities"
        )
        response = browser.get(
            path, headers={"Authorization": "Bearer browser-secret", "X-Forwarded-Host": "evil.example"}
        )
        assert response.status_code == 200
        assert len(seen) == 1
        assert seen[0].headers["authorization"] == "Bearer host-only-secret"
        assert "cookie" not in seen[0].headers
        assert "x-forwarded-host" not in seen[0].headers
        assert "set-cookie" not in response.headers
        assert response.headers["etag"] == '"2"'
        assert browser.get("/studio/api/https://evil.example/path").status_code == 404
        assert browser.get("/studio/api/api/v1/unknown").status_code == 404
        other = path.replace("000000000001", "000000000009")
        assert browser.get(other).status_code == 403
        assert len(seen) == 1


def test_offline_bridge_is_explicit_and_profile_requires_tls(tmp_path):
    with client(tmp_path) as browser:
        pair(browser)
        response = browser.get("/studio/api/api/v1/capabilities")
        assert response.status_code == 409
    with pytest.raises(ValueError):
        StudioProfile(name="Unsafe", base_url="http://remote.example")


def test_scope_switch_uses_authorized_discovery(tmp_path):
    tenant, project, environment = ("00000000-0000-0000-0000-00000000000" + str(i) for i in (1, 2, 3))

    async def upstream(request):
        assert request.url.path == "/api/v1/identity"
        return httpx.Response(
            200,
            json={
                "workspaces": [
                    {
                        "id": tenant,
                        "name": "Tenant",
                        "projects": [
                            {"id": project, "name": "Project", "environments": [{"id": environment, "name": "Dev"}]}
                        ],
                    }
                ]
            },
        )

    with client(
        tmp_path,
        profile=StudioProfile(name="Platform", base_url="https://api.example"),
        token_provider=lambda: "test",
        transport=httpx.MockTransport(upstream),
    ) as browser:
        headers = pair(browser)
        selected = {"tenantId": tenant, "projectId": project, "environmentId": environment}
        response = browser.post("/studio/scope", json=selected, headers=headers)
        assert response.status_code == 200
        assert response.json()["profile"]["environmentId"] == environment
        assert (
            browser.post(
                "/studio/scope",
                json={**selected, "environmentId": "00000000-0000-0000-0000-000000000009"},
                headers=headers,
            ).status_code
            == 403
        )
        assert browser.get("/studio/session").json()["profile"]["environmentId"] == environment


def test_bridge_never_exposes_worker_credential_lease(tmp_path):
    async def upstream(request):
        raise AssertionError("Studio must reject credential endpoints before sending anything")

    profile = StudioProfile(
        name="Dev",
        base_url="https://api.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        environment_id="00000000-0000-0000-0000-000000000003",
    )
    with client(
        tmp_path, profile=profile, token_provider=lambda: "test", transport=httpx.MockTransport(upstream)
    ) as browser:
        headers = pair(browser)
        path = (
            "/studio/api/api/v1/tenants/00000000-0000-0000-0000-000000000001"
            "/projects/00000000-0000-0000-0000-000000000002"
            "/environments/00000000-0000-0000-0000-000000000003/tasks/credentials"
        )
        assert browser.post(path, json={}, headers=headers).status_code == 404


def test_expired_host_credentials_return_actionable_authentication_error(tmp_path):
    from firefly_weave.sdk.auth import AuthError

    async def expired(origin):
        raise AuthError("WV-AUTH-EXPIRED")

    class Session:
        get_access_token = staticmethod(expired)

    with client(
        tmp_path,
        profile=StudioProfile(name="Dev", base_url="https://api.example"),
        token_provider=Session(),
    ) as browser:
        pair(browser)
        response = browser.get("/studio/api/api/v1/identity")
        assert response.status_code == 401
        assert response.json()["code"] == "WV-AUTH-REQUIRED"
        assert "weave auth login" in response.json()["message"]


def test_non_ascii_pairing_code_returns_controlled_rejection(tmp_path):
    with client(tmp_path) as browser:
        result = browser.post("/studio/session", json={"code": "código"}, headers={"Origin": ORIGIN})
        assert result.status_code == 403
        assert result.json()["code"] == "WV-STUDIO-PAIRING"


def test_bridge_rejects_encoded_response_before_consuming(tmp_path):
    consumed = []

    class Encoded(httpx.AsyncByteStream):
        async def __aiter__(self):
            consumed.append(True)
            yield b"not consumed"

    async def upstream(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Encoded())

    with client(
        tmp_path,
        profile=StudioProfile(name="Dev", base_url="https://api.example"),
        token_provider=lambda: "test",
        transport=httpx.MockTransport(upstream),
    ) as browser:
        pair(browser)
        response = browser.get("/studio/api/api/v1/identity")
        assert response.status_code == 502
        assert response.json()["code"] == "WV-STUDIO-ENCODING"
        assert not consumed


def test_email_receipts_and_tokens_are_scoped_bridge_operations(tmp_path):
    profile = StudioProfile(
        name="Dev",
        base_url="https://api.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        environment_id="00000000-0000-0000-0000-000000000003",
    )
    with client(tmp_path, profile=profile, token_provider=lambda: "test") as browser:
        service = browser.app.state.studio
        prefix = (
            "/api/v1/tenants/00000000-0000-0000-0000-000000000001"
            "/projects/00000000-0000-0000-0000-000000000002"
            "/environments/00000000-0000-0000-0000-000000000003"
        )
        assert service.check_scope(prefix + "/email/receipts", "GET") == 200
        assert service.check_scope(prefix + "/email/correlation-tokens", "POST") == 200
        assert service.check_scope(prefix.replace("000000000003", "000000000009") + "/email/receipts", "GET") == 403


def test_member_bridge_is_exact_scoped_and_never_allows_bootstrap_or_generic_grants(tmp_path):
    profile = StudioProfile(
        name="Dev", base_url="https://api.example", tenant_id="00000000-0000-0000-0000-000000000001"
    )
    with client(tmp_path, profile=profile, token_provider=lambda: "test") as browser:
        service = browser.app.state.studio
        assert service.check_scope("/api/v1/admin/principals", "GET") == 200
        assert service.check_scope("/api/v1/admin/principals", "POST") == 200
        assert service.check_scope("/api/v1/tenants/00000000-0000-0000-0000-000000000001/members", "GET") == 200
        assert service.check_scope("/api/v1/tenants/00000000-0000-0000-0000-000000000002/members", "POST") == 403
        assert service.check_scope("/admin/grants", "POST") == 404
        assert service.check_scope("/api/v1/admin/grants", "POST") == 404
        assert service.check_scope("/api/v1/admin/bootstrap", "POST") == 404


def test_desktop_pairing_is_reusable_rotates_secrets_and_still_limits_failures(tmp_path):
    with client(tmp_path, reusable_pairing=True) as browser:
        first = pair(browser)
        old_cookie = browser.cookies.get("weave_studio_session")
        assert browser.delete("/studio/session", headers=first).status_code == 204
        assert browser.get("/studio/session").json()["paired"] is False
        # The native shell pairs again after a reload, an unpairing or the end of the 8 h session.
        second = pair(browser)
        assert second["X-Weave-CSRF"] != first["X-Weave-CSRF"]
        assert browser.cookies.get("weave_studio_session") != old_cookie
        assert (
            browser.post("/studio/local/validate", json={"source": "a: 1", "format": "yaml"}, headers=first).status_code
            == 403
        )
        browser.app.state.studio.session_deadline = 0
        assert browser.get("/studio/session").json()["paired"] is False
        third = pair(browser)
        assert third["X-Weave-CSRF"] != second["X-Weave-CSRF"]
        for _ in range(10):
            refused = browser.post("/studio/session", json={"code": "wrong"}, headers={"Origin": ORIGIN})
            assert refused.status_code == 403
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 403


def test_browser_pairing_stays_single_use_after_unpairing(tmp_path):
    with client(tmp_path) as browser:
        headers = pair(browser)
        assert browser.delete("/studio/session", headers=headers).status_code == 204
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 403
    (tmp_path / "late").mkdir()
    with client(tmp_path / "late") as browser:
        browser.app.state.studio.pairing_deadline = 0
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 403


def test_brand_files_are_served_from_the_studio_origin_and_license_texts_are_not(tmp_path):
    with client(tmp_path) as browser:
        assets = tmp_path / "assets"
        (assets / "fonts/manrope").mkdir(parents=True)
        (assets / "fonts/manrope/manrope-latin-wght-normal.woff2").write_bytes(b"wOF2 font bytes")
        (assets / "favicon.ico").write_bytes(b"\0\0\1\0 icon bytes")
        (assets / "licenses").mkdir()
        (assets / "licenses/NOTICE.txt").write_text("Firefly Weave\n")
        (assets / "site.webmanifest").write_text("{}")
        font = browser.get("/fonts/manrope/manrope-latin-wght-normal.woff2")
        assert font.status_code == 200 and font.content == b"wOF2 font bytes"
        icon = browser.get("/favicon.ico")
        assert icon.status_code == 200 and icon.content == b"\0\0\1\0 icon bytes"
        # Only the allowlisted suffixes are served: license texts and a web manifest in the bundle are not.
        assert browser.get("/licenses/NOTICE.txt").status_code == 404
        assert browser.get("/site.webmanifest").status_code == 404
