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

"""Real retained Keycloak public-client browser flows; no password-grant token request."""

import asyncio
import os
import secrets
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

pytestmark = pytest.mark.integration


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.action = None
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = values.get("action")
        if tag == "input" and values.get("name") and values.get("type") == "hidden":
            self.inputs[values["name"]] = values.get("value", "")


@pytest.fixture
async def live_cli():
    root = os.environ.get("WEAVE_KEYCLOAK_TEST_URL")
    secret = os.environ.get("WEAVE_KC_ADMIN_SECRET")
    if root != "http://localhost:18081" or not secret:
        pytest.fail("Identity gate requires owned Keycloak on localhost:18081 and WEAVE_KC_ADMIN_SECRET")
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
        response = await client.post(
            root + "/realms/master/protocol/openid-connect/token",
            data={"grant_type": "client_credentials", "client_id": "weave-bootstrap", "client_secret": secret},
        )
        if response.status_code != 200:
            pytest.fail("Task-owned identity admin client is unavailable", pytrace=False)
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        admin = root + "/admin/realms/weave"
        clients = await client.get(admin + "/clients", headers=headers, params={"clientId": "weave-cli"})
        assert clients.status_code == 200 and len(clients.json()) == 1
        record = clients.json()[0]
        assert record["publicClient"] and not record["directAccessGrantsEnabled"]
        assert record["attributes"]["pkce.code.challenge.method"] == "S256"
        record["redirectUris"] = sorted(
            set(record["redirectUris"])
            | {
                "http://127.0.0.1/callback",
                "http://[::1]/callback",
            }
        )
        assert (await client.put(admin + "/clients/" + record["id"], headers=headers, json=record)).status_code == 204
        realm = (await client.get(admin, headers=headers)).json()
        realm.update(revokeRefreshToken=True, refreshTokenMaxReuse=0)
        assert (await client.put(admin, headers=headers, json=realm)).status_code == 204
        username = "c6-browser-" + uuid4().hex
        password = secrets.token_urlsafe(32)
        response = await client.post(
            admin + "/users",
            headers=headers,
            json={
                "username": username,
                "enabled": True,
                "emailVerified": True,
                "firstName": "C6",
                "lastName": "Acceptance",
                "email": username + "@example.invalid",
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
        )
        assert response.status_code == 201
        yield root, username, SecretStr(password)


async def browser_login(url, root, username, password, *, stop_before_callback=False, device=False):
    """Drive normal trusted browser forms with a bounded redirect chain, retaining cookies."""
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as browser:
        response = await browser.get(url)
        for _ in range(15):
            # Keycloak marks localhost cookies Secure; browsers trust localhost.
            # HTTPX lacks that exception. This test-only jar is confined to the guarded local IdP.
            for cookie in browser.cookies.jar:
                if cookie.domain == "localhost.local":
                    cookie.secure = False
            if response.is_redirect:
                destination = urljoin(str(response.url), response.headers["location"])
                parsed = urlsplit(destination)
                if parsed.hostname in {"127.0.0.1", "::1"} and parsed.path == "/callback":
                    if stop_before_callback:
                        return destination
                    result = await browser.get(destination)
                    assert result.status_code == 200
                    return destination
                assert destination.startswith(root + "/")
                response = await browser.get(destination)
                continue
            if device and response.status_code == 200 and "Device Login Successful" in response.text:
                return "device-approved"
            form = Form(response.text)
            assert response.status_code == 200 and form.action, (
                "Expected browser form",
                response.status_code,
                [
                    word
                    for word in (
                        "Cookie not found",
                        "Invalid parameter",
                        "Invalid Request",
                        "Invalid request",
                        "Session",
                        "HTTPS required",
                        "Invalid username",
                        "Client",
                        "invalid_code",
                        "Expired",
                        "Restart",
                        "error",
                        "cookie",
                    )
                    if word in response.text
                ],
            )
            action = urljoin(str(response.url), form.action)
            assert action.startswith(root + "/")
            response = await browser.post(
                action,
                data={**form.inputs, "username": username, "password": password.get_secret_value(), "accept": "Yes"},
            )
        pytest.fail("Normal browser flow exceeded its redirect bound", pytrace=False)


async def test_live_pkce_ephemeral_rotation_logout_and_token_purpose(
    live_cli, tmp_path, services, access_db, provisioned
):
    from firefly_weave.access.authentication import AuthenticationService, VerifierSet
    from firefly_weave.access.authorization import AccessDenied, AuthorizationService
    from firefly_weave.access.models import Grant
    from firefly_weave.access.oidc import AuthenticationFailed, OIDCVerifier, ProviderConfig
    from firefly_weave.sdk.auth import AuthError, LoginConfig, OAuthSession
    from firefly_weave.sdk.credentials import FileCredentialStore

    root, username, password = live_cli
    tmp_path.chmod(0o700)
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=root + "/realms/weave",
        client_id="weave-cli",
        target="https://api.example",
        account=username,
        scopes=("openid",),
        allow_loopback_http=True,
    )
    store = FileCredentialStore(tmp_path / "session.json")
    ports = []

    class ObservedSession(OAuthSession):
        id_token = None

        async def _save_login(self, tokens, generation):
            self.id_token = tokens.id_token
            return await super()._save_login(tokens, generation)

    session = ObservedSession(config, store)

    async def browser(url):
        ports.append(urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port)
        return await browser_login(url, root, username, password)

    for _ in range(2):
        assert (await session.login(flow="pkce", browser=browser))["authenticated"]
    assert len(set(ports)) == 2 and all(port != 18555 for port in ports)
    first = store.load(config.binding)
    assert first.refresh_token and session.id_token
    verifier = OIDCVerifier(
        ProviderConfig(
            provider_id="local-keycloak",
            issuer=config.issuer,
            jwks_uri=config.issuer + "/protocol/openid-connect/certs",
            audience="weave-api",
            clients={"weave-cli": "human"},
            local_development=True,
        )
    )
    identity = await verifier.verify(first.access_token.get_secret_value())
    with pytest.raises(AuthenticationFailed):
        await verifier.verify(session.id_token)
    sessions, _, service, _ = access_db
    admin, scopes = provisioned
    graph = services(sessions, verifiers=VerifierSet((verifier,)))
    authentication = graph.resolve(AuthenticationService)
    with pytest.raises(AuthenticationFailed):
        await authentication.authenticate(first.access_token.get_secret_value())
    principal = await service.create_principal(admin, "human")
    await service.link_identity(admin, principal, identity)
    await service.grant(admin, principal, Grant(role="developer", scope=scopes[0]))
    actor = await authentication.authenticate(first.access_token.get_secret_value())
    authorization = graph.resolve(AuthorizationService)
    authorization.require(actor, scopes[0], "definition.write")
    with pytest.raises(AccessDenied):
        authorization.require(actor, scopes[1], "definition.write")
    # Keycloak stale-token comparison has whole-second iat granularity.
    # Advance to the next actual token timestamp before creating the newer chain.
    import time

    import jwt

    issued = jwt.decode(first.refresh_token.get_secret_value(), options={"verify_signature": False})["iat"]
    async with asyncio.timeout(2):
        while int(time.time()) <= issued:
            await asyncio.sleep(0.02)
    store.save(config.binding, first.model_copy(update={"expires_at": 0}))
    results = await asyncio.gather(*(session.get_access_token(config.target) for _ in range(4)))
    assert len(set(results)) == 1
    second = store.load(config.binding)
    assert second.refresh_token != first.refresh_token
    store.save(config.binding, second.model_copy(update={"expires_at": 0}))
    await session.get_access_token(config.target)
    third = store.load(config.binding)
    assert third.refresh_token != second.refresh_token
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        stale = await client.post(
            config.issuer + "/protocol/openid-connect/token",
            data={
                "client_id": config.client_id,
                "grant_type": "refresh_token",
                "refresh_token": first.refresh_token.get_secret_value(),
            },
        )
        assert stale.status_code == 400 and stale.json()["error"] == "invalid_grant"
    assert (await session.logout(revoke=True))["logged_out"]
    assert store.load(config.binding) is None
    with pytest.raises(AuthError):
        await session.get_access_token(config.target)


async def test_live_device_pkce_completion_and_cancel_no_late_save(live_cli, tmp_path):
    from urllib.parse import urlencode

    from firefly_weave.sdk.auth import LoginConfig, OAuthSession
    from firefly_weave.sdk.credentials import FileCredentialStore

    root, username, password = live_cli
    tmp_path.chmod(0o700)
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=root + "/realms/weave",
        client_id="weave-cli",
        target="https://api.example",
        account=username,
        scopes=("openid",),
        allow_loopback_http=True,
        login_timeout=20,
    )
    store = FileCredentialStore(tmp_path / "device.json")
    session = OAuthSession(config, store)
    browsers = []

    def instructions(uri, code):
        browsers.append(
            asyncio.create_task(
                browser_login(uri + "?" + urlencode({"user_code": code}), root, username, password, device=True)
            )
        )

    try:
        try:
            assert (await session.login(flow="device", instructions=instructions))["authenticated"]
        except Exception:
            if browsers and browsers[0].done():
                await browsers[0]
            raise
        assert await browsers[0] == "device-approved"
    finally:
        for task in browsers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*browsers, return_exceptions=True)
    assert store.load(config.binding).refresh_token
    ready = asyncio.Event()
    task = asyncio.create_task(session.login(flow="device", instructions=lambda *_: ready.set()))
    await asyncio.wait_for(ready.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # A cancelled attempt is abandoned as signed out (never left reading as in progress) and saves no token.
    assert store.load(config.binding).state == "logged_out"
    assert store.load(config.binding).access_token is None
    await session.logout()
    assert store.load(config.binding) is None


async def test_live_pkce_wrong_verifier_rejected(live_cli):
    from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints, generate_pkce

    from firefly_weave.sdk.auth import PKCETransaction

    root, username, password = live_cli
    issuer = root + "/realms/weave"
    transaction = PKCETransaction(issuer, "http://127.0.0.1:18555/callback", require_issuer=True)
    callback = await browser_login(
        transaction.authorization_url(issuer + "/protocol/openid-connect/auth", "weave-cli", ("openid",)),
        root,
        username,
        password,
        stop_before_callback=True,
    )
    parsed = urlsplit(callback)
    code = transaction.accept(parsed.path, parse_qs(parsed.query))
    async with OAuth2Client(
        "weave-cli", OAuth2Endpoints(issuer + "/protocol/openid-connect/token"), allow_loopback_http=True
    ) as client:
        with pytest.raises(OAuth2ClientError) as failure:
            await client.exchange_code(
                code, redirect_uri=transaction.redirect_uri, code_verifier=generate_pkce().verifier
            )
        assert failure.value.code == "invalid_grant"


async def test_live_device_expiry_with_retained_unique_public_client(live_cli, tmp_path):
    from firefly_weave.sdk.auth import AuthError, LoginConfig, OAuthSession
    from firefly_weave.sdk.credentials import FileCredentialStore

    root, username, _ = live_cli
    identifier = "c6-expiry-" + uuid4().hex
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            root + "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "weave-bootstrap",
                "client_secret": os.environ["WEAVE_KC_ADMIN_SECRET"],
            },
        )
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        response = await client.post(
            root + "/admin/realms/weave/clients",
            headers=headers,
            json={
                "clientId": identifier,
                "protocol": "openid-connect",
                "publicClient": True,
                "enabled": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "implicitFlowEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": ["http://127.0.0.1/callback"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "oauth2.device.authorization.grant.enabled": "true",
                    "oauth2.device.code.lifespan": "1",
                    "oauth2.device.polling.interval": "1",
                },
            },
        )
        assert response.status_code == 201
    tmp_path.chmod(0o700)
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=root + "/realms/weave",
        client_id=identifier,
        target="https://api.example",
        account=username,
        scopes=("openid",),
        allow_loopback_http=True,
        login_timeout=10,
    )
    store = FileCredentialStore(tmp_path / "expired.json")
    session = OAuthSession(config, store)
    notified = []
    with pytest.raises(AuthError) as failure:
        await session.login(flow="device", instructions=lambda *_: notified.append(True))
    assert notified == [True] and failure.value.code in {"WV-AUTH-DENIED", "WV-AUTH-EXPIRED"}
    assert store.load(config.binding).state == "logged_out"
    assert store.load(config.binding).refresh_token is None


async def test_installed_cli_device_login_status_logout_json(live_cli, tmp_path):
    import json
    import sys
    from urllib.parse import urlencode

    from firefly_weave.sdk.auth import LoginConfig
    from firefly_weave.sdk.credentials import FileCredentialStore

    root, username, password = live_cli
    tmp_path.chmod(0o700)
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=root + "/realms/weave",
        client_id="weave-cli",
        target="https://api.example",
        account=username,
        scopes=("openid",),
        allow_loopback_http=True,
        login_timeout=20,
    )
    configuration, credentials = tmp_path / "config.json", tmp_path / "cli.json"
    configuration.write_text(config.model_dump_json())
    common = ["--auth-config", str(configuration), "--credential-store", "file", "--credential-file", str(credentials)]
    python = os.environ.get("WEAVE_C6_CLIENT_PYTHON", sys.executable)

    async def start(command, *arguments):
        return await asyncio.create_subprocess_exec(
            python,
            "-m",
            "firefly_weave.cli.main",
            "auth",
            command,
            *common,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    process = await start("login", "--flow", "device")
    try:
        instruction = (await asyncio.wait_for(process.stderr.readline(), 10)).decode().strip()
        assert instruction.startswith("Open ") and " and enter " in instruction
        uri, code = instruction[5:].split(" and enter ", 1)
        assert (
            await browser_login(uri + "?" + urlencode({"user_code": code}), root, username, password, device=True)
            == "device-approved"
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0 and not stderr
        assert json.loads(stdout)["authenticated"] is True
        record = FileCredentialStore(credentials).load(config.binding)
        assert record.access_token.get_secret_value().encode() not in stdout
        assert record.refresh_token.get_secret_value().encode() not in stdout
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
    for command, expected, authenticated in (
        ("status", 0, True),
        ("logout", 0, False),
        ("logout", 0, False),
        ("status", 1, False),
    ):
        process = await start(command)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == expected and not stderr
        result = json.loads(stdout)
        if command == "logout":
            assert result == {"logged_out": True, "remote_revocation": "not_requested"}
        else:
            assert result["authenticated"] is authenticated
        assert record.access_token.get_secret_value().encode() not in stdout
        assert record.refresh_token.get_secret_value().encode() not in stdout
    assert not credentials.exists()


async def keycloak_browser(browser, url, root, username, password):
    """Drive one authorization request in a persistent cookie jar; return how many sign-in forms were shown."""
    forms = 0
    response = await browser.get(url)
    for _ in range(15):
        for cookie in browser.cookies.jar:
            if cookie.domain == "localhost.local":
                cookie.secure = False
        if response.is_redirect:
            destination = urljoin(str(response.url), response.headers["location"])
            parsed = urlsplit(destination)
            if parsed.hostname in {"127.0.0.1", "::1"} and parsed.path == "/callback":
                assert "error" not in parse_qs(parsed.query), parsed.query
                assert (await browser.get(destination)).status_code == 200
                return forms
            assert destination.startswith(root + "/")
            response = await browser.get(destination)
            continue
        form = Form(response.text)
        assert response.status_code == 200 and form.action, response.status_code
        action = urljoin(str(response.url), form.action)
        assert action.startswith(root + "/")
        forms += 1
        response = await browser.post(
            action,
            data={**form.inputs, "username": username, "password": password.get_secret_value(), "accept": "Yes"},
        )
    pytest.fail("Normal browser flow exceeded its redirect bound", pytrace=False)


async def test_cli_profile_setup_status_switch_account_and_revoke(live_cli, tmp_path):
    """Saved-platform journey through the CLI process against the real isolated Keycloak (no Weave API)."""
    import json
    import sys
    from pathlib import Path

    from firefly_weave.sdk.auth import LoginConfig
    from firefly_weave.sdk.credentials import FileCredentialStore
    from firefly_weave.sdk.profiles import ProfileStore

    root, username, password = live_cli
    tmp_path.chmod(0o700)
    issuer = root + "/realms/weave"
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=issuer,
        client_id="weave-cli",
        target="https://api.example",
        account="operator-file-account",
        scopes=("openid",),
        allow_loopback_http=True,
        login_timeout=60,
    )
    connection, credentials = tmp_path / "connection.json", tmp_path / "credentials.json"
    connection.write_text(config.model_dump_json())
    bound = LoginConfig.model_validate({**config.model_dump(), "account": "kc"})
    python = os.environ.get("WEAVE_C6_CLIENT_PYTHON")
    environment = {k: v for k, v in os.environ.items() if k not in {"WEAVE_PROFILE", "WEAVE_BASE_URL"}}
    environment.update(WEAVE_CONFIG_HOME=str(tmp_path / "config"), WEAVE_NO_ANIMATION="1")
    if python is None:
        # Exercise this source tree with the test interpreter.
        python = sys.executable
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    async def start(*arguments):
        return await asyncio.create_subprocess_exec(
            python,
            "-m",
            "firefly_weave.cli.main",
            "auth",
            *arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def sign_in(process, browser):
        """Read the printed sign-in address from stderr, complete it, and return (url, forms, result)."""
        lines = []
        try:
            while True:
                line = (await asyncio.wait_for(process.stderr.readline(), 20)).decode()
                assert line, "".join(lines)
                lines.append(line)
                if line.startswith("  ") and "/protocol/openid-connect/auth?" in line:
                    url = line.strip()
                    break
            forms = await keycloak_browser(browser, url, root, username, password)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0, ("".join(lines), stderr.decode(), stdout.decode())
        return url, forms, json.loads(stdout), "".join(lines) + stderr.decode() + stdout.decode()

    async def run(*arguments, expected=0):
        process = await start(*arguments)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == expected, (stdout.decode(), stderr.decode())
        return json.loads(stdout), stdout.decode() + stderr.decode()

    seen = []
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as browser:
        setup = await start(
            "setup", "--auth-config", str(connection), "--name", "kc", "--yes", "--flow", "browser",
            "--no-browser", "--skip-workspace", "--credential-store", "file", "--credential-file",
            str(credentials), "--output", "json",
        )  # fmt: skip
        url, forms, summary, output = await sign_in(setup, browser)
        seen.append(output)
        assert forms >= 1 and "prompt" not in parse_qs(urlsplit(url).query)
        assert "Step 3 of 4 · Sign in" in output and "no passwords or tokens" in output
        assert summary["profile"] == "kc" and summary["authenticated"] is True and summary["active"] is True
        assert summary["workspace"] is None and summary["saved"]["credentials"] == f"file:{credentials}"
        assert summary["account"]["issuer"] == issuer and summary["account"]["provider_id"] == "local-keycloak"
        # With only the "openid" scope Keycloak's id_token carries no username, so the hint is the subject.
        assert summary["account"]["display_name"] in {None, username} and summary["account"]["subject"]
        profile = ProfileStore(tmp_path / "config" / "profiles.json").get("kc")
        assert profile.source == "file" and profile.login == bound
        first = FileCredentialStore(credentials).load(bound.binding)
        assert first.state == "active" and first.refresh_token is not None

        status, output = await run("status", "--profile", "kc", "--output", "json")
        seen.append(output)
        assert status["authenticated"] is True and status["state"] == "signed_in"
        assert status["account"]["subject"] == summary["account"]["subject"]

        # Control: the same browser session signs in again without a form (single sign-on).
        process = await start("login", "--profile", "kc", "--flow", "browser", "--no-browser", "--output", "json")
        url, forms, result, output = await sign_in(process, browser)
        seen.append(output)
        assert forms == 0 and "prompt" not in parse_qs(urlsplit(url).query) and result["authenticated"]

        # Switch account: prompt=login makes Keycloak ask again despite the session cookie.
        process = await start("login", "--profile", "kc", "--switch-account", "--no-browser", "--output", "json")
        url, forms, result, output = await sign_in(process, browser)
        seen.append(output)
        assert parse_qs(urlsplit(url).query)["prompt"] == ["login"]
        assert forms >= 1 and result["authenticated"] is True and result["account"]["subject"]
        current = FileCredentialStore(credentials).load(bound.binding)
        assert current.refresh_token.get_secret_value() != first.refresh_token.get_secret_value()

    logout, output = await run("logout", "--profile", "kc", "--revoke", "--output", "json")
    seen.append(output)
    assert logout == {
        "profile": "kc",
        "server": "https://api.example",
        "logged_out": True,
        "remote_revocation": "confirmed",
        "account": None,
    }
    assert FileCredentialStore(credentials).load(bound.binding) is None
    assert ProfileStore(tmp_path / "config" / "profiles.json").get("kc").account is None
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        revoked = await client.post(
            issuer + "/protocol/openid-connect/token",
            data={
                "client_id": "weave-cli",
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token.get_secret_value(),
            },
        )
        assert revoked.status_code == 400 and revoked.json()["error"] == "invalid_grant"
    after, output = await run("status", "--profile", "kc", "--output", "json", expected=1)
    seen.append(output)
    assert after["authenticated"] is False and after["state"] == "signed_out"
    profiles = (tmp_path / "config" / "profiles.json").read_text()
    for record in (first, current):
        for secret in (record.access_token, record.refresh_token):
            value = secret.get_secret_value()
            assert value not in profiles and all(value not in text for text in seen)
