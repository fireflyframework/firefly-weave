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

"""Sign acceptance people in to the platform's API the way a person does, then call it as them.

Sign-in uses the CLI's public client with PKCE through the identity provider's real pages: the
harness reads the login form, posts the person's generated password and follows redirects only
inside the platform's Keycloak, up to the loopback callback. Tokens go to a private file store
under the run's private/ folder, never the system credential store. Errors never carry the
password.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from uuid import UUID, uuid4

import httpx

from firefly_weave.contracts.access import Scope
from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.credentials import FileCredentialStore

CLIENT_ID = "weave-cli"
REDIRECTS = 15


class SignInFailed(AssertionError):
    """The identity provider's pages did not reach the loopback callback; the message holds no secret."""


class _Form(HTMLParser):
    """The first form's action and its hidden inputs."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = values.get("action")
        if tag == "input" and values.get("name") and values.get("type") == "hidden":
            self.inputs[str(values["name"])] = values.get("value") or ""


def _loopback_redirect(url: str) -> tuple[str, str, str]:
    """Scheme, host and port, and path of the authorization request's loopback redirect_uri."""
    values = parse_qs(urlsplit(url).query).get("redirect_uri", [])
    redirect = urlsplit(values[0]) if len(values) == 1 else None
    try:
        port = redirect.port if redirect is not None else None
    except ValueError:
        port = None
    if (
        redirect is None
        or redirect.scheme != "http"
        or redirect.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or redirect.username is not None
        or redirect.query
        or redirect.fragment
    ):
        raise SignInFailed("The authorization request names no loopback redirect_uri")
    return redirect.scheme, redirect.netloc, redirect.path


async def follow_sign_in(
    url: str,
    issuer_origin: str,
    username: str,
    password: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Complete one authorization request in the identity provider's pages, ending at the loopback callback.

    The only off-provider redirect followed is the loopback redirect_uri the authorization request names.
    """
    callback = _loopback_redirect(url)
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as browser:
        response = await browser.get(url)
        for _ in range(REDIRECTS):
            # Keycloak marks localhost cookies Secure; browsers trust localhost, HTTPX does not.
            for cookie in browser.cookies.jar:
                if cookie.domain == "localhost.local":
                    cookie.secure = False
            if response.is_redirect:
                destination = urljoin(str(response.url), response.headers["location"])
                parsed = urlsplit(destination)
                if (parsed.scheme, parsed.netloc, parsed.path) == callback:
                    answer = await browser.get(destination)
                    if answer.status_code != 200:
                        raise SignInFailed(f"The loopback callback answered HTTP {answer.status_code}")
                    return
                if not destination.startswith(issuer_origin + "/"):
                    raise SignInFailed("Sign-in redirected outside the platform's Keycloak")
                response = await browser.get(destination)
                continue
            form = _Form(response.text)
            if response.status_code != 200 or not form.action:
                raise SignInFailed(f"Expected the Keycloak sign-in form, got HTTP {response.status_code}")
            action = urljoin(str(response.url), form.action)
            if not action.startswith(issuer_origin + "/"):
                raise SignInFailed("The sign-in form posts outside the platform's Keycloak")
            response = await browser.post(
                action, data={**form.inputs, "username": username, "password": password, "accept": "Yes"}
            )
        raise SignInFailed("Sign-in exceeded its redirect bound")


def session_store(private: Path, name: str) -> FileCredentialStore:
    """A private token file, private/sessions/<name>.json, in a 0700 folder."""
    folder = private / "sessions"
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    return FileCredentialStore(folder / f"{name}.json")


def scope_of(scope: Mapping[str, str]) -> Scope:
    return Scope(
        tenant_id=UUID(scope["tenant_id"]),
        project_id=UUID(scope["project_id"]),
        environment_id=UUID(scope["environment_id"]),
    )


def api_origin(state: Mapping[str, Any]) -> str:
    ports = state["ports"]
    return f"http://127.0.0.1:{ports['container_api' if state.get('mode') == 'docker' else 'api']}"


@asynccontextmanager
async def person_client(run: Any, name: str) -> AsyncIterator[WeaveClient]:
    """A WeaveClient signed in as ``name`` on the demo environment (local), through a fresh sign-in.

    Every call signs in again, whatever the private store holds: a failed sign-in leaves a
    logged_out record, a failed refresh leaves a token-free refreshing fence, and the identity
    provider's idle timeout can end a session a journey kept from an earlier one. A fresh
    sign-in costs one form post and keeps journeys independent.
    """
    person = run.person(name)
    state = run.platform_state()
    api = api_origin(state)
    issuer_origin = f"http://localhost:{state['ports']['keycloak']}"
    config = LoginConfig(
        provider_id="local-keycloak",
        issuer=issuer_origin + "/realms/weave",
        client_id=CLIENT_ID,
        target=api,
        account=person["username"],
        scopes=("openid",),
        allow_loopback_http=True,
    )
    # Each client has its own token file, removed when the client closes: a sign-in marks the record
    # authenticating, and a second client sharing the file would fail every request of the first meanwhile.
    store = session_store(run.private, f"{name}-{uuid4().hex}")
    session = OAuthSession(config, store)

    async def browser(url: str) -> None:
        await follow_sign_in(url, issuer_origin, person["username"], person["password"])

    try:
        await session.login(flow="pkce", browser=browser)
        async with WeaveClient(api, session, scope_of(run.demo_scope())) as client:
            yield client
    finally:
        store.path.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))
