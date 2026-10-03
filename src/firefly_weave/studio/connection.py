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

"""Platform connections for the local Studio host: saved platforms, discovery, owned sign-in and workspaces.

Studio runs in one of two modes. With a profile store (the default), the
shared `profiles.json` decides which platform is active and every change is
saved there, so the CLI, the browser host and the desktop app agree. With an
explicit legacy profile file or token the connection lives in memory only.
A server only proposes its sign-in settings: a person reviews them once and
the saved profile pins them, including the sign-in flows the administrator
allows. Tokens stay in the credential store and never reach the browser;
platform authorization stays on the server. Studio's start preference is kept
in `studio.json` next to the saved platforms (in memory without a store).
"""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
import webbrowser
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyfly.container import service
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave.contracts.client_configuration import SignInOption
from firefly_weave.sdk import sign_in
from firefly_weave.sdk.auth import AuthError, LoginConfig, OAuthSession, origin
from firefly_weave.sdk.credentials import CredentialError, NativeCredentialStore
from firefly_weave.sdk.errors import ContractError, TransportError, WeaveError
from firefly_weave.sdk.profiles import (
    PROFILE_NAME,
    AccountHint,
    PlatformProfile,
    ProfileDocument,
    ProfileError,
    ProfileStore,
    SignInFlow,
    StudioPreferences,
    WorkspaceSelection,
    profile_name,
)
from firefly_weave.sdk.server_config import (
    ServerConfigError,
    fetch_client_configuration,
    issuer_origin,
    login_config_for,
    normalize_server_address,
    probe_sign_in,
    require_sign_in,
)
from firefly_weave.studio.service import StudioProfile, StudioService, problem

TransportFactory = Callable[[], httpx.AsyncBaseTransport]
RequestedFlow = Literal["browser", "device", "auto"]
ALL_FLOWS: tuple[SignInFlow, ...] = ("browser", "device")
# Discovery reads one small document and probes at most eight providers concurrently.
DISCOVERY_BUDGET = 30.0
CONNECT_PROBLEMS: dict[str, tuple[int, str]] = {
    "WV-CONNECT-ADDRESS": (
        422,
        "Enter the server address, for example weave.example.com. Leave out any path, query or user name.",
    ),
    "WV-CONNECT-INSECURE": (
        422,
        "Studio connects to remote servers only over HTTPS. Use an address that starts with https://.",
    ),
    "WV-CONNECT-BLOCKED": (
        422,
        "That address points to a reserved network location, so Studio will not connect to it. "
        "Check the server address.",
    ),
    "WV-CONNECT-NO-SIGN-IN": (
        422,
        "That server does not offer sign-in settings yet. Ask your administrator to publish them, "
        "or connect with a connection file instead.",
    ),
    "WV-CONNECT-UNREACHABLE": (
        502,
        "Studio could not reach that server. Check the address and your network or VPN, then try again.",
    ),
    "WV-CONNECT-TLS": (
        502,
        "Studio could not make a secure connection to that server. Its certificate may be invalid "
        "or not trusted on this computer.",
    ),
    "WV-CONNECT-TIMEOUT": (
        502,
        "The server did not answer in time. Check your network or VPN, then try again.",
    ),
    "WV-CONNECT-NOT-WEAVE": (
        502,
        "That address does not look like a Firefly Weave server. Check the address with your administrator.",
    ),
    "WV-CONNECT-INCOMPATIBLE": (
        502,
        "That server runs a Firefly Weave version this Studio cannot use. Update Studio or ask your administrator.",
    ),
    "WV-CONNECT-PROVIDER": (
        502,
        "Studio could not use the sign-in service this server announces. "
        "Ask your administrator to check the server's sign-in settings.",
    ),
    "WV-CONNECT-REDIRECT": (
        409,
        "That address forwards to a different location. Enter the server's final address instead.",
    ),
}
PROVIDER_PROBLEMS = {
    "WV-AUTH-TRUST": (
        "This sign-in service uses addresses the server does not list as trusted, so Studio will not use it. "
        "Ask your administrator to check the sign-in settings."
    ),
    "WV-AUTH-DENIED": (
        "The sign-in service refused Studio's request. Ask your administrator to check the sign-in settings."
    ),
}
PROVIDER_UNREACHABLE = (
    "Studio could not reach this sign-in service. Check your network or VPN, or ask your administrator."
)
PROFILE_STATUS = {
    "WV-PROFILE-NOT-FOUND": 404,
    "WV-PROFILE-NAME": 422,
    "WV-PROFILE-STORE": 503,
}
PROFILE_MESSAGES = {
    "WV-PROFILE-EXISTS": "A saved platform already uses that name. Choose another name.",
}
CHANGED_SETTINGS = "The server's sign-in settings changed. Review them again."
NOT_LINKED = (
    "You signed in, but this platform does not know your account yet. "
    "Ask your administrator to add it, then check the connection again."
)
CREDENTIAL_STORE = (
    "Studio cannot use this computer's credential store. "
    "Unlock or set up your system keychain or credential manager, then try again."
)
FLOW_REFUSED = {
    "browser": ("Your administrator allows only sign-in with a code for this platform. Sign in with a code instead."),
    "device": "Your administrator allows only browser sign-in for this platform. Sign in with your browser instead.",
}
SWITCH_WITH_CODE = (
    "This platform allows only sign-in with a code, so Studio cannot ask the sign-in page to offer another account. "
    "On the sign-in page in your browser, choose the account you want to use "
    "(sign out of the other account first if the page signs you in automatically)."
)
SWITCH_WITH_CHOSEN_CODE = (
    "Sign-in with a code cannot ask the sign-in page to offer another account. "
    "On the sign-in page in your browser, choose the account you want to use "
    "(sign out of the other account first if the page signs you in automatically)."
)
PREFERENCES_UNSAVED = (
    "Studio could not save this preference in its settings folder. Check that the folder is yours, then try again."
)


class ConnectionConfigure(BaseModel):
    """Legacy reviewed connection file: an explicit `LoginConfig` and a display name."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=100)
    login: LoginConfig
    trust_confirmed: Literal[True]


class DiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    server: str = Field(min_length=1, max_length=2048)


class ProfileCreate(BaseModel):
    """Echo of the reviewed option; the host re-reads the server and pins only matching settings."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=1, max_length=2048)
    provider_id: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=200)
    trust_confirmed: Literal[True]


class ProfileSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=256)


class ProfileRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=256)
    sign_out: bool = True


class SignOut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    revoke: bool = True


class LoginStart(BaseModel):
    """`flow` left out means the browser when the platform allows it, else a code."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    flow: RequestedFlow | None = None
    switch_account: bool = False


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    start: Literal["ask", "local"]


def create_session(config: LoginConfig) -> OAuthSession:
    """Legacy in-memory connection: only the host chooses storage; browser-supplied paths are forbidden."""
    return OAuthSession(config, NativeCredentialStore())


def profile_session(profile: PlatformProfile, transport_factory: TransportFactory | None) -> OAuthSession:
    """The OAuth session for a saved platform, using the credential store its profile names."""
    return sign_in.session_for(profile, transport_factory=transport_factory)


@dataclass
class LoginFlow:
    id: str | None = None
    state: str = "idle"
    flow: str | None = None
    verification_uri: str | None = None
    user_code: str | None = None
    authorization_uri: str | None = None
    error_code: str | None = None
    # Epoch seconds when the host stops waiting for this sign-in; None when idle or finished.
    expires_at: float | None = None
    # Plain-language guidance for this sign-in (for example switching account with a code).
    notice: str | None = None


def _settle(flow: LoginFlow) -> None:
    """A sign-in that never reached its own cleanup (cancelled before it ran) still ends as cancelled."""
    if flow.state in {"starting", "awaiting_user"}:
        flow.state = "cancelled"
    if flow.state != "idle":
        flow.verification_uri = flow.user_code = flow.authorization_uri = None
        flow.expires_at = None


def choose_flow(
    requested: RequestedFlow | None, switch_account: bool, allowed: Iterable[SignInFlow]
) -> tuple[RequestedFlow, Literal["login"] | None, str | None] | JSONResponse:
    """The flow to run, its prompt and a notice; a 409 `WV-AUTH-FLOW` problem for a flow the platform refuses.

    Switching account prefers the browser (the provider shows its sign-in page
    again); a platform that allows only codes switches with a code instead. A
    code has no account prompt, so a code switch always says how to pick the account.
    """
    allowed = set(allowed)
    permitted = [flow for flow in ALL_FLOWS if flow in allowed]
    if switch_account:
        if requested == "device" and "device" in permitted:
            return "device", None, SWITCH_WITH_CHOSEN_CODE
        if "browser" in permitted:
            return "browser", "login", None
        return "device", None, SWITCH_WITH_CODE
    if requested is None:
        requested = "browser" if "browser" in permitted else "device"
    if requested == "auto":
        # Both allowed: the SDK's auto (a code when the provider offers one, else the browser).
        return ("auto", None, None) if len(permitted) == len(ALL_FLOWS) else (permitted[0], None, None)
    if requested not in permitted:
        return problem(409, "WV-AUTH-FLOW", FLOW_REFUSED[requested])
    return requested, None, None


def _signed_in(provider: OAuthSession) -> bool:
    """The local credential is valid right now (a keyring read may prompt; run it off the event loop)."""
    try:
        value = provider.status()
    except Exception:
        return False
    return isinstance(value, dict) and value.get("authenticated") is True


# --- plain-language problems --------------------------------------------------------------------------------------


def connect_problem(error: ServerConfigError) -> JSONResponse:
    status, message = CONNECT_PROBLEMS.get(error.code, CONNECT_PROBLEMS["WV-CONNECT-PROVIDER"])
    body: dict[str, Any] = {"status": status, "code": error.code, "message": message}
    if error.code == "WV-CONNECT-REDIRECT":
        # The detail is only ever a screened https origin, never a path or a response body.
        body["suggested_server"] = error.detail
        if error.detail:
            body["message"] = (
                f"That address forwards to {error.detail}. If you trust it, connect to that address instead."
            )
    elif error.code == "WV-CONNECT-PROVIDER" and error.detail:
        body["detail"] = error.detail
    return JSONResponse(body, status_code=status)


def profile_problem(error: ProfileError) -> JSONResponse:
    status = PROFILE_STATUS.get(error.code, 409)
    return problem(status, error.code, PROFILE_MESSAGES.get(error.code, error.message))


def request_problem(message: str) -> JSONResponse:
    return problem(422, "WV-STUDIO-REQUEST", message)


def no_store_problem() -> JSONResponse:
    return problem(
        409,
        "WV-PROFILE-STORE",
        "Saved platforms are not available while Studio uses a profile file. "
        "Start Studio without --profile to save platforms.",
    )


def option_problem(error: ServerConfigError) -> dict[str, Any]:
    if error.code == "WV-CONNECT-PROVIDER":
        message = PROVIDER_PROBLEMS.get(error.detail or "", PROVIDER_UNREACHABLE)
    else:
        message = CONNECT_PROBLEMS.get(error.code, (502, PROVIDER_UNREACHABLE))[1]
    return {"code": error.code, "message": message, "detail": error.detail}


def credential_problem() -> JSONResponse:
    return problem(503, "WV-AUTH-STORE", CREDENTIAL_STORE)


def _platform_refused(response: Response) -> bool:
    """A 401 the platform itself sent (the bridge's own sign-in problems are `WV-AUTH-REQUIRED`)."""
    try:
        body = json.loads(bytes(response.body))
    except ValueError:
        return True
    return not isinstance(body, dict) or body.get("code") != "WV-AUTH-REQUIRED"


# --- views (never include tokens, credential records or file contents) ----------------------------------------


def workspace_label(selection: WorkspaceSelection | None) -> str | None:
    if selection is None:
        return None
    names = (selection.tenant_name, selection.project_name, selection.environment_name)
    return " / ".join(name for name in names if name) if all(names) else None


def workspace_view(selection: WorkspaceSelection | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {**selection.model_dump(mode="json"), "label": workspace_label(selection)}


def account_view(hint: AccountHint | None) -> dict[str, Any] | None:
    return None if hint is None else hint.model_dump(mode="json")


def account_label(hint: AccountHint | None) -> str | None:
    return None if hint is None else hint.display_name or hint.subject


def _label(value: str | None) -> str | None:
    """A display label the profile can store, or None (labels are informational only)."""
    if value is None or not value or len(value) > 100 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        return None
    return value


def suggest_name(display_name: str | None, server: str, taken: Iterable[str]) -> str:
    """A valid, unused profile name from the server's display name, else its host name."""
    used = {name.casefold() for name in taken}
    base = ""
    for candidate in (display_name, urlsplit(server).hostname):
        if candidate:
            # Fold accents, keep the allowed characters and collapse everything else to spaces.
            text = unicodedata.normalize("NFKD", candidate).encode("ascii", "ignore").decode("ascii")
            text = " ".join("".join(c if c.isalnum() or c in "._-" else " " for c in text).split())
            text = text.lstrip(" ._-")[:64].rstrip(" ._-")
            if text and PROFILE_NAME.fullmatch(text):
                base = text
                break
    base = base or "Platform"
    for number in range(1, 1000):
        suffix = "" if number == 1 else f" {number}"
        name = base[: 64 - len(suffix)].rstrip(" ._-") + suffix
        if name.casefold() not in used:
            return name
    return "Platform " + uuid4().hex[:8]


def _same_sign_in(first: LoginConfig, second: LoginConfig) -> bool:
    return (first.target, first.provider_id, first.issuer, first.client_id) == (
        second.target,
        second.provider_id,
        second.issuer,
        second.client_id,
    )


def _find(document: ProfileDocument, name: str) -> PlatformProfile | None:
    folded = name.casefold()
    return next((value for key, value in document.profiles.items() if key.casefold() == folded), None)


def _merge(existing: PlatformProfile | None, candidate: PlatformProfile) -> PlatformProfile | None:
    """The profile to save, or None when the name belongs to a platform with other sign-in settings."""
    if existing is None:
        return candidate
    if existing.name != candidate.name or not _same_sign_in(existing.login, candidate.login):
        return None
    # The same reviewed server, provider and client: refresh settings, keep the person's choices.
    return PlatformProfile.model_validate(
        {
            **dict(candidate),
            "workspace": existing.workspace,
            "account": existing.account,
            "credential_store": existing.credential_store,
            "credential_file": existing.credential_file,
            "created_at": existing.created_at,
        }
    )


@service
class StudioConnectionService:
    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        self.store: ProfileStore | None = studio.options.profile_store
        self.platform: PlatformProfile | None = None
        self.store_error: ProfileError | None = None
        self.credential_error: str | None = None
        provider = studio.options.token_provider
        self.oauth: OAuthSession | None = (
            provider
            if isinstance(provider, OAuthSession) and isinstance(provider.store, NativeCredentialStore)
            else None
        )
        self.flow = LoginFlow()
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        # Unverified account hint of a sign-in without a saved platform (display and linking help only).
        self.session_account: AccountHint | None = None
        # Start preference when there is no profile store (lives as long as this host).
        self.preferences = StudioPreferences()
        self._restore()

    def _restore(self) -> None:
        """Reconnect to the active saved platform; a damaged store leaves Studio offline, never stopped."""
        if self.store is None:
            return
        try:
            active = self.store.active()
            if active is None:
                return
            studio_profile = active.to_studio_profile()
        except ProfileError as error:
            self.store_error = error
            return
        except (OSError, ValueError):
            self.store_error = ProfileError("WV-PROFILE-STORE")
            return
        self.platform = active
        self.studio.options.profile = studio_profile
        try:
            self.oauth = self._open(active)
        except CredentialError:
            self.credential_error = "WV-AUTH-STORE"
            return
        self.studio.options.token_provider = self.oauth

    def _open(self, profile: PlatformProfile) -> OAuthSession:
        return profile_session(profile, self.studio.options.sign_in_transport)

    def _reopen(self) -> JSONResponse | None:
        """Retry the active platform's credential store (for example after unlocking the keychain).

        The caller holds `self.lock`. Nothing in flight used the missing session, so no fence is needed.
        """
        platform = self.platform
        if self.oauth is not None or platform is None:
            return None
        try:
            session = self._open(platform)
        except CredentialError:
            self.credential_error = "WV-AUTH-STORE"
            return credential_problem()
        self.oauth, self.credential_error = session, None
        self.studio.options.token_provider = session
        return None

    def _transport(self) -> httpx.AsyncBaseTransport | None:
        factory = self.studio.options.sign_in_transport
        return factory() if factory is not None else None

    # --- status ---------------------------------------------------------------------------------------------------

    def metadata(self) -> dict[str, bool]:
        # A saved platform always supports sign-in; an unavailable credential store is reported and retried.
        supported = self.oauth is not None or self.platform is not None
        return {"configured": self.studio.options.profile is not None, "login_supported": supported}

    async def status(self) -> dict[str, Any]:
        store, document = await self._store_view()
        return {
            **self.metadata(),
            "store": store,
            "profile": self._profile_view(),
            "profiles": self._summaries(document),
            "authentication": await self._authentication(),
            "login": asdict(self.flow),
            "preferences": {"start": (await self._preferences()).start},
        }

    def _allowed_flows(self) -> tuple[SignInFlow, ...]:
        # Without a saved platform (connection file or token) the administrator's choice is unknown.
        return tuple(self.platform.flows) if self.platform is not None else ALL_FLOWS

    def _account(self) -> AccountHint | None:
        return self.platform.account if self.platform is not None else self.session_account

    async def _store_view(self) -> tuple[dict[str, Any], ProfileDocument | None]:
        if self.store is None:
            return {"available": False, "location": None}, None
        view: dict[str, Any] = {"available": False, "location": self.store.location}
        try:
            document = await asyncio.to_thread(self.store.load)
        except ProfileError as error:
            view.update(error_code=error.code, message=error.message)
            return view, None
        view["available"] = True
        return view, document

    def _summaries(self, document: ProfileDocument | None) -> list[dict[str, Any]]:
        # Only names, origins and labels: credential stores of other profiles are never read here.
        if document is None:
            return []
        active = self.platform.name if self.platform is not None else None
        return [
            {
                "name": profile.name,
                "server": profile.server,
                "active": profile.name == active,
                "display_name": profile.display_name,
                "provider_name": profile.provider_name,
                "workspace_label": workspace_label(profile.workspace),
                "account_label": account_label(profile.account),
            }
            for profile in sorted(document.profiles.values(), key=lambda value: (value.name.casefold(), value.name))
        ]

    def _profile_view(self) -> dict[str, Any] | None:
        live = self.studio.options.profile
        if live is None:
            return None
        platform = self.platform
        login = platform.login if platform is not None else self.oauth.config if self.oauth is not None else None
        issuer_view: str | None = None
        if login is not None:
            with suppress(AuthError):
                issuer_view = origin(login.issuer, allow_loopback_http=login.allow_loopback_http)
        if platform is not None:
            workspace = platform.workspace
        elif live.tenant_id and live.project_id and live.environment_id:
            workspace = WorkspaceSelection(
                tenant_id=live.tenant_id, project_id=live.project_id, environment_id=live.environment_id
            )
        else:
            workspace = None
        return {
            "name": live.name,
            "server": live.base_url.rstrip("/"),
            "saved": platform is not None,
            "source": platform.source if platform is not None else None,
            "display_name": platform.display_name if platform is not None else None,
            "provider_name": platform.provider_name if platform is not None else None,
            "provider_id": login.provider_id if login is not None else None,
            "issuer": login.issuer if login is not None else None,
            "issuer_origin": issuer_view,
            "client_id": login.client_id if login is not None else None,
            "scopes": list(login.scopes) if login is not None else [],
            "trusted_endpoint_origins": list(login.trusted_endpoint_origins) if login is not None else [],
            "flows": list(self._allowed_flows()),
            "workspace": workspace_view(workspace),
            "account": account_view(platform.account if platform is not None else None),
            "credential_store": platform.credential_store if platform is not None else live.credential_store,
        }

    async def _authentication(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "authenticated": False,
            "reauthentication_required": True,
            "refresh_available": False,
            "state": "signed_out",
            "expires_at": None,
        }
        if self.oauth is not None:
            try:
                # Reads only the active profile's credential record (a keyring read may prompt).
                current = await asyncio.to_thread(self.oauth.status)
            except CredentialError:
                result["error_code"] = "WV-AUTH-STORE"
            else:
                result.update({key: current.get(key, result[key]) for key in tuple(result)})
        elif self.credential_error is not None:
            result["error_code"] = self.credential_error
        result["account"] = account_view(self._account())
        return result

    # --- preferences ----------------------------------------------------------------------------------------------

    async def _preferences(self) -> StudioPreferences:
        if self.store is None:
            return self.preferences
        try:
            return await asyncio.to_thread(self.store.load_preferences)
        except ProfileError:
            # Damaged or unreadable: the default applies and the file is only replaced on an explicit save.
            return StudioPreferences()

    async def save_preferences(self, request: PreferencesUpdate) -> Response:
        chosen = StudioPreferences(start=request.start)
        if self.store is None:
            self.preferences = chosen
        else:
            try:
                await asyncio.to_thread(self.store.save_preferences, chosen)
            except ProfileError:
                return problem(503, "WV-PROFILE-STORE", PREFERENCES_UNSAVED)
        return JSONResponse({"preferences": {"start": chosen.start}})

    async def _changed(self, **extra: Any) -> JSONResponse:
        return JSONResponse({"session": self.studio.session_payload(True), "connection": await self.status(), **extra})

    # --- switching ------------------------------------------------------------------------------------------------

    async def _switch(self, platform: PlatformProfile | None, session: OAuthSession | None) -> None:
        """Replace the live connection; the caller holds `self.lock`. In-flight results are fenced."""
        await self._cancel()
        self.studio.connection_generation += 1
        self.platform, self.oauth, self.flow, self.credential_error = platform, session, LoginFlow(), None
        self.session_account = None
        self.studio.options.profile = platform.to_studio_profile() if platform is not None else None
        self.studio.options.token_provider = session

    async def _save_and_use(self, candidate: PlatformProfile) -> JSONResponse | None:
        """Save (or refresh) a profile, make it active and switch to it; a problem response on failure."""
        store = self.store
        assert store is not None
        async with self.lock:
            try:
                document = await asyncio.to_thread(store.load)
                existing = _find(document, candidate.name)
                merged = _merge(existing, candidate)
                if merged is None:
                    raise ProfileError("WV-PROFILE-EXISTS")
                session = self._open(merged)
                saved = await asyncio.to_thread(store.save, merged, activate=True, replace=existing is not None)
            except ProfileError as error:
                return profile_problem(error)
            except CredentialError:
                return credential_problem()
            await self._switch(saved, session)
        return None

    # --- discovery and saved platforms ---------------------------------------------------------------------------

    async def discover(self, request: DiscoverRequest) -> Response:
        try:
            server = normalize_server_address(request.server)
            async with asyncio.timeout(DISCOVERY_BUDGET):
                configuration = await fetch_client_configuration(server, transport=self._transport())
                options = require_sign_in(configuration)
                document = await self._document_or_none()
                taken = list(document.profiles) if document is not None else []
                suggested = suggest_name(configuration.display_name, server, taken)
                existing = next(
                    (
                        profile.name
                        for profile in sorted(
                            document.profiles.values() if document is not None else (),
                            key=lambda value: value.name.casefold(),
                        )
                        if profile.server == server
                    ),
                    None,
                )
                entries = await asyncio.gather(*(self._review(option, server, suggested) for option in options))
        except ServerConfigError as error:
            return connect_problem(error)
        except TimeoutError:
            return connect_problem(ServerConfigError("WV-CONNECT-TIMEOUT"))
        return JSONResponse(
            {
                "server": server,
                "display_name": configuration.display_name,
                "api_version": configuration.api_version,
                "suggested_name": suggested,
                "existing_profile": existing,
                "sign_in": entries,
            }
        )

    async def _document_or_none(self) -> ProfileDocument | None:
        if self.store is None:
            return None
        try:
            return await asyncio.to_thread(self.store.load)
        except ProfileError:
            return None

    async def _review(self, option: SignInOption, server: str, account: str) -> dict[str, Any]:
        """One announced option with its provider checks; a failure is reported on the option itself."""
        entry: dict[str, Any] = {
            **option.model_dump(mode="json"),
            "issuer_origin": None,
            "checks": {"browser": False, "device": False},
            "problem": None,
        }
        try:
            entry["issuer_origin"] = issuer_origin(option)
            login = login_config_for(option, server=server, account=account)
            check = await probe_sign_in(login, transport=self._transport())
        except ServerConfigError as error:
            entry["problem"] = option_problem(error)
            return entry
        entry["checks"] = {"browser": check.browser, "device": check.device}
        return entry

    async def create_profile(self, request: ProfileCreate) -> Response:
        if self.store is None:
            return no_store_problem()
        try:
            name = profile_name(request.name)
            server = normalize_server_address(request.server)
            async with asyncio.timeout(DISCOVERY_BUDGET):
                configuration = await fetch_client_configuration(server, transport=self._transport())
        except ProfileError as error:
            return profile_problem(error)
        except ServerConfigError as error:
            return connect_problem(error)
        except TimeoutError:
            return connect_problem(ServerConfigError("WV-CONNECT-TIMEOUT"))
        option = next((value for value in configuration.sign_in if value.provider_id == request.provider_id), None)
        # Pin only what the person reviewed: a changed issuer or client needs a new review.
        if option is None or option.issuer != request.issuer or option.client_id != request.client_id:
            return problem(409, "WV-PROFILE-CHANGED", CHANGED_SETTINGS)
        try:
            login = login_config_for(option, server=server, account=name)
            candidate = PlatformProfile(
                name=name,
                login=login,
                source="server",
                display_name=_label(configuration.display_name),
                provider_name=_label(option.display_name),
                # The administrator's allowed flows; a later review of the same platform refreshes them.
                flows=list(option.flows),
            )
        except ServerConfigError as error:
            return connect_problem(error)
        except ValueError:
            return request_problem("Review the platform details again before saving.")
        failure = await self._save_and_use(candidate)
        return failure if failure is not None else await self._changed()

    async def configure(self, request: ConnectionConfigure) -> Response:
        if self.store is not None and await self._document_or_none() is not None:
            return await self._configure_saved(request)
        # Legacy mode, or the saved platforms file is unreadable: this connection lives in memory only.
        # Validate/create the replacement before cancelling or changing a working profile.
        try:
            replacement = create_session(request.login)
            profile = StudioProfile(name=request.name, base_url=request.login.target)
        except (CredentialError, ValueError, AuthError):
            return problem(
                422, "WV-STUDIO-CONNECTION", "Verify the explicit login configuration and native credential store"
            )
        async with self.lock:
            await self._switch(None, replacement)
            self.studio.options.profile = profile
        return JSONResponse(self.studio.session_payload(True))

    async def _configure_saved(self, request: ConnectionConfigure) -> Response:
        """A reviewed connection file in saved-platforms mode: saved as a `file` profile and activated."""
        try:
            name = profile_name(request.name)
            candidate = PlatformProfile.from_studio_profile(
                StudioProfile(name=name, base_url=request.login.target), request.login, source="file"
            )
        except ProfileError as error:
            return profile_problem(error)
        except (ValueError, AuthError):
            return problem(
                422, "WV-STUDIO-CONNECTION", "Verify the explicit login configuration and native credential store"
            )
        failure = await self._save_and_use(candidate)
        return failure if failure is not None else JSONResponse(self.studio.session_payload(True))

    async def activate(self, request: ProfileSelection) -> Response:
        store = self.store
        if store is None:
            return no_store_problem()
        async with self.lock:
            try:
                selected = await asyncio.to_thread(store.get, request.name)
                session = self._open(selected)
                activated = await asyncio.to_thread(store.activate, selected.name)
                if activated.login != selected.login or activated.credential_file != selected.credential_file:
                    session = self._open(activated)
            except ProfileError as error:
                return profile_problem(error)
            except CredentialError:
                return credential_problem()
            await self._switch(activated, session)
        return await self._changed()

    async def disconnect(self) -> Response:
        """Work locally: no active platform, saved platforms and credentials stay."""
        async with self.lock:
            if self.store is not None:
                try:
                    await asyncio.to_thread(self.store.deactivate)
                except ProfileError as error:
                    return profile_problem(error)
            await self._switch(None, None)
        return await self._changed()

    async def remove(self, request: ProfileRemoval) -> Response:
        store = self.store
        if store is None:
            return no_store_problem()
        async with self.lock:
            try:
                removed = await asyncio.to_thread(store.remove, request.name)
            except ProfileError as error:
                return profile_problem(error)
            if self.platform is not None and self.platform.name == removed.name:
                await self._switch(None, None)
        outcome: dict[str, Any] = {"credentials_removed": False, "remote_revocation": "not_requested"}
        if request.sign_out:
            # Best effort and bounded; the network part runs without holding the connection lock.
            try:
                session = self._open(removed)
                async with asyncio.timeout(2 * removed.login.timeout + 5):
                    result = await session.logout(revoke=True)
                outcome = {
                    "credentials_removed": True,
                    "remote_revocation": result.get("remote_revocation", "unconfirmed"),
                }
            except (CredentialError, AuthError, TimeoutError, OSError):
                outcome = {"credentials_removed": False, "remote_revocation": "unconfirmed"}
        return await self._changed(sign_out=outcome)

    async def logout(self, request: SignOut) -> Response:
        async with self.lock:
            failure = self._reopen()
            if failure is not None:
                return failure
            provider, platform = self.oauth, self.platform
            if provider is None:
                return problem(409, "WV-STUDIO-CONNECTION", "Choose a platform before signing out.")
            await self._cancel()
            self.flow = LoginFlow()
            self.session_account = None
            self.studio.connection_generation += 1
        revocation = "unconfirmed"
        try:
            async with asyncio.timeout(2 * provider.config.timeout + 5):
                result = await provider.logout(revoke=request.revoke)
            revocation = str(result.get("remote_revocation", "unconfirmed"))
        except CredentialError:
            return credential_problem()
        except TimeoutError:
            pass
        if platform is not None and self.store is not None:
            with suppress(ProfileError):
                await asyncio.to_thread(self.store.set_account, platform.name, None)
                if self.platform is not None and self.platform.name == platform.name:
                    self.platform = self.platform.model_copy(update={"account": None})
        return await self._changed(remote_revocation=revocation)

    # --- owned sign-in --------------------------------------------------------------------------------------------

    async def start(self, method: RequestedFlow | None = None, switch_account: bool = False) -> Response:
        async with self.lock:
            failure = self._reopen()
            if failure is not None:
                return failure
            if self.oauth is None:
                return problem(409, "WV-STUDIO-CONNECTION", "Choose a platform before signing in.")
            # Only the flows the administrator allows; "switch account" asks the provider for its sign-in page.
            choice = choose_flow(method, switch_account, self._allowed_flows())
            if isinstance(choice, JSONResponse):
                return choice
            flow, prompt, notice = choice
            if self.task is not None and not self.task.done():
                return JSONResponse(asdict(self.flow))
            config = self.oauth.config
            self.flow = LoginFlow(
                id=str(uuid4()),
                state="starting",
                flow=flow,
                # The same budget `_login` waits for: discovery, the person's sign-in and saving.
                expires_at=time.time() + config.login_timeout + config.timeout,
                notice=notice,
            )
            self.task = asyncio.create_task(
                self._login(self.oauth, self.flow, method=flow, prompt=prompt), name="studio-owned-login"
            )
            return JSONResponse(asdict(self.flow), status_code=202)

    async def _login(
        self,
        provider: OAuthSession,
        flow: LoginFlow,
        *,
        method: RequestedFlow = "auto",
        prompt: Literal["login"] | None = None,
    ) -> None:
        def open_native_browser(uri: str) -> None:
            if self.studio.options.open_login_browser and self.flow is flow and self.oauth is provider:
                # The current trusted URI remains available for manual copy/open.
                with suppress(OSError, webbrowser.Error):
                    webbrowser.open(uri, new=2)

        def instructions(uri: str, code: str) -> None:
            provider.config.trust_endpoint(uri)
            if len(uri) > 8192 or not code or len(code) > 256:
                raise AuthError("WV-AUTH-PROVIDER", 3)
            flow.state, flow.flow, flow.verification_uri, flow.user_code = "awaiting_user", "device", uri, code
            open_native_browser(uri)

        def browser(uri: str) -> None:
            provider.config.trust_endpoint(uri)
            if len(uri) > 8192:
                raise AuthError("WV-AUTH-PROVIDER", 3)
            flow.state, flow.flow, flow.authorization_uri = "awaiting_user", "browser", uri
            open_native_browser(uri)

        try:
            # Include discovery, credential locking and callback cleanup in the overall owned budget.
            async with asyncio.timeout(provider.config.login_timeout + provider.config.timeout):
                result = await provider.login(flow=method, instructions=instructions, browser=browser, prompt=prompt)
            await self._remember_account(provider, result)
            if self.oauth is provider:
                # The new credential may belong to another account: fence results of the previous one.
                self.studio.connection_generation += 1
            flow.state = "authenticated"
        except asyncio.CancelledError:
            flow.state = "cancelled"
            raise
        except AuthError as error:
            flow.state, flow.error_code = "failed", error.code
        except CredentialError:
            flow.state, flow.error_code = "failed", "WV-AUTH-STORE"
        except TimeoutError:
            flow.state, flow.error_code = "failed", "WV-AUTH-EXPIRED"
        except Exception:
            flow.state, flow.error_code = "failed", "WV-AUTH-PROVIDER"
        finally:
            # These are user instructions, not credentials, but retain them only while the flow is active.
            flow.verification_uri = flow.user_code = flow.authorization_uri = None
            flow.expires_at = None

    async def _remember_account(self, provider: OAuthSession, result: Any) -> None:
        """Save the unverified account hint for display and linking help; never used for authorization."""
        platform, store = self.platform, self.store
        if self.oauth is not provider:
            return
        identity = result.get("identity") if isinstance(result, dict) else None
        try:
            hint = AccountHint.model_validate(identity) if isinstance(identity, dict) else None
        except ValidationError:
            hint = None
        if store is None or platform is None:
            # Without a saved platform the hint lives in memory for this connection only.
            self.session_account = hint
            return
        try:
            await asyncio.to_thread(store.set_account, platform.name, hint)
        except ProfileError:
            return
        if self.oauth is provider and self.platform is not None and self.platform.name == platform.name:
            self.platform = self.platform.model_copy(update={"account": hint})

    def login_status(self, identifier: str) -> Response:
        if self.flow.id != identifier:
            return problem(404, "WV-STUDIO-LOGIN", "Login flow unavailable; refresh connection status")
        return JSONResponse(asdict(self.flow))

    async def cancel(self, identifier: str) -> Response:
        async with self.lock:
            if self.flow.id != identifier:
                return problem(404, "WV-STUDIO-LOGIN", "Login flow unavailable; refresh connection status")
            await self._cancel()
            return JSONResponse(asdict(self.flow))

    async def open_login(self, identifier: str) -> Response:
        """Desktop only: open the current trusted sign-in address again in the system browser."""
        if not self.studio.options.open_login_browser:
            return problem(
                409, "WV-STUDIO-BROWSER", "Open the sign-in address shown in Studio in your browser instead."
            )
        flow, provider = self.flow, self.oauth
        if flow.id != identifier:
            return problem(404, "WV-STUDIO-LOGIN", "Login flow unavailable; refresh connection status")
        uri = flow.authorization_uri or flow.verification_uri
        if flow.state != "awaiting_user" or uri is None or provider is None:
            return problem(409, "WV-STUDIO-LOGIN", "This sign-in is no longer waiting. Start signing in again.")
        try:
            provider.config.trust_endpoint(uri)
        except AuthError:
            return problem(409, "WV-STUDIO-LOGIN", "This sign-in is no longer waiting. Start signing in again.")
        opened = False
        with suppress(OSError, webbrowser.Error):
            opened = bool(await asyncio.to_thread(webbrowser.open, uri, new=2))
        return JSONResponse({**asdict(flow), "opened": opened})

    async def _cancel(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            _settle(self.flow)

    async def close(self) -> None:
        async with self.lock:
            await self._cancel()

    # --- identity and workspaces ----------------------------------------------------------------------------------

    def _stale(self, generation: int, platform: PlatformProfile | None, provider: Any) -> bool:
        return (
            generation != self.studio.connection_generation
            or self.platform is not platform
            or self.studio.options.token_provider is not provider
        )

    async def test(self, request: Request) -> Response:
        if self.studio.options.profile is None:
            return problem(409, "WV-STUDIO-CONNECTION", "Choose a platform before checking the connection.")
        if self.platform is not None and self.oauth is None:
            async with self.lock:
                failure = self._reopen()
            if failure is not None:
                return failure
        platform, provider = self.platform, self.studio.options.token_provider
        if platform is None or not isinstance(provider, OAuthSession):
            return await self._legacy_test(request)
        generation = self.studio.connection_generation
        try:
            async with asyncio.timeout(40):
                check = await sign_in.verify_sign_in(platform, provider, transport=self.studio.options.transport)
        except sign_in.SignInError as error:
            if self._stale(generation, platform, provider):
                return self._changed_problem()
            return self._not_linked(platform.login, error.account)
        except (TransportError, TimeoutError):
            return problem(
                502, "WV-STUDIO-UPSTREAM", "Studio could not reach the platform. Check your network, then try again."
            )
        except ContractError:
            return problem(502, "WV-STUDIO-IDENTITY", "Platform discovery returned an incompatible response")
        except WeaveError as error:
            if error.status == 401:
                return problem(401, "WV-AUTH-REQUIRED", "Your sign-in has ended. Sign in again to continue.")
            return problem(error.status, error.code, "The platform could not confirm your account. Try again later.")
        except AuthError as error:
            if error.code == "WV-AUTH-OFFLINE":
                return problem(
                    503,
                    "WV-AUTH-OFFLINE",
                    "Studio could not reach the sign-in service to renew your session. "
                    "Check your network or VPN, then try again.",
                )
            return problem(401, "WV-AUTH-REQUIRED", "Your sign-in has ended. Sign in again to continue.")
        except CredentialError:
            return credential_problem()
        except ValueError:
            return problem(502, "WV-STUDIO-IDENTITY", "Platform discovery returned an incompatible response")
        if self._stale(generation, platform, provider):
            return self._changed_problem()
        revoked = False
        selected = platform.workspace
        if (
            selected is not None
            and not check.truncated
            and not any(
                (option.tenant_id, option.project_id, option.environment_id)
                == (selected.tenant_id, selected.project_id, selected.environment_id)
                for option in check.workspaces
            )
        ):
            revoked = await self._revoke_workspace(generation, platform, provider)
            if not revoked:
                return self._changed_problem()
        return JSONResponse(
            {
                "session": self.studio.session_payload(True),
                "identity": check.identity.model_dump(mode="json"),
                "workspaces": [option.model_dump(mode="json") for option in check.workspaces],
                "truncated": check.truncated,
                "workspace_revoked": revoked,
            }
        )

    async def _revoke_workspace(self, generation: int, platform: PlatformProfile, provider: Any) -> bool:
        """The saved workspace is no longer authorized: forget it here and in the store."""
        async with self.lock:
            if self._stale(generation, platform, provider):
                return False
            if self.store is not None:
                with suppress(ProfileError):
                    await asyncio.to_thread(self.store.set_workspace, platform.name, None)
            self.platform = platform.model_copy(update={"workspace": None})
            self.studio.options.profile = self.platform.to_studio_profile()
            self.studio.connection_generation += 1
        return True

    @staticmethod
    def _not_linked(login: LoginConfig, hint: AccountHint | None) -> JSONResponse:
        """401 `WV-AUTH-NOT-LINKED`: signed in, unknown to the platform. `subject` is null without a hint."""
        account = {
            "provider_id": login.provider_id,
            "issuer": login.issuer,
            "subject": hint.subject if hint is not None else None,
            "display_name": hint.display_name if hint is not None else None,
        }
        return JSONResponse(
            {"status": 401, "code": "WV-AUTH-NOT-LINKED", "message": NOT_LINKED, "account": account},
            status_code=401,
        )

    @staticmethod
    def _changed_problem() -> JSONResponse:
        return problem(409, "WV-STUDIO-CONNECTION-CHANGED", "Connection changed; test the new profile explicitly")

    async def _legacy_test(self, request: Request) -> Response:
        async def empty() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        generation = self.studio.connection_generation
        provider = self.studio.options.token_provider
        discovery = Request({**request.scope, "method": "GET", "query_string": b""}, empty)
        response = await self.studio.bridge(discovery, "/api/v1/identity")
        # Like saved platforms: the provider accepted the person, but the platform does not know them.
        if (
            response.status_code == 401
            and isinstance(provider, OAuthSession)
            and _platform_refused(response)
            and await asyncio.to_thread(_signed_in, provider)
        ):
            if generation != self.studio.connection_generation or self.studio.options.token_provider is not provider:
                return self._changed_problem()
            return self._not_linked(provider.config, self.session_account)
        if response.status_code != 200:
            return response
        try:
            # Use the same typed public contract as the canonical API and SDK.
            from firefly_weave.contracts.identity import IdentityView

            identity = IdentityView.model_validate_json(json.dumps(json.loads(bytes(response.body))))
        except (ValueError, TypeError):
            return problem(502, "WV-STUDIO-IDENTITY", "Platform discovery returned an incompatible response")
        if generation != self.studio.connection_generation:
            return self._changed_problem()
        check = sign_in.check_identity(identity)
        return JSONResponse(
            {
                "session": self.studio.session_payload(True),
                "identity": identity.model_dump(mode="json"),
                "workspaces": [option.model_dump(mode="json") for option in check.workspaces],
                "truncated": check.truncated,
                "workspace_revoked": False,
            }
        )

    async def apply_scope(self, generation: int, selection: WorkspaceSelection) -> Response:
        """Use an authorized workspace; saved platforms remember it with its names."""
        async with self.lock:
            if generation != self.studio.connection_generation:
                return problem(
                    409, "WV-STUDIO-CONNECTION-CHANGED", "Connection changed; choose a scope from the new profile"
                )
            profile = self.studio.options.profile
            if profile is None:
                return problem(409, "WV-STUDIO-CONNECTION", "Choose a platform before choosing a workspace.")
            platform = self.platform
            if platform is not None and self.store is not None:
                try:
                    await asyncio.to_thread(self.store.set_workspace, platform.name, selection)
                except ProfileError as error:
                    return profile_problem(error)
                self.platform = platform.model_copy(update={"workspace": selection})
            self.studio.connection_generation += 1
            self.studio.options.profile = profile.model_copy(
                update={
                    "tenant_id": selection.tenant_id,
                    "project_id": selection.project_id,
                    "environment_id": selection.environment_id,
                }
            )
        return JSONResponse(self.studio.session_payload(True))
