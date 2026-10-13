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
from urllib.parse import urljoin, urlsplit
from uuid import UUID

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


async def follow_sign_in(
    url: str,
    issuer_origin: str,
    username: str,
    password: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Complete one authorization request in the identity provider's pages, ending at the loopback callback."""
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
                if parsed.hostname in {"127.0.0.1", "::1"} and parsed.path == "/callback":
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
    """The person's private token file: private/sessions/<name>.json in a 0700 folder."""
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
    """A WeaveClient signed in as ``name`` on the demo environment (local)."""
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
    store = session_store(run.private, name)
    session = OAuthSession(config, store)
    if store.load(config.binding) is None:

        async def browser(url: str) -> None:
            await follow_sign_in(url, issuer_origin, person["username"], person["password"])

        await session.login(flow="pkce", browser=browser)
    async with WeaveClient(api, session, scope_of(run.demo_scope())) as client:
        yield client


def read_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))
