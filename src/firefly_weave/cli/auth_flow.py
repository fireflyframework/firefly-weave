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

"""Sign-in and workspace steps shared by `weave auth setup` and the profile commands.

Everything protocol-related is delegated to the shared SDK (`sdk.sign_in`,
`sdk.auth`, `sdk.server_config`): this module only chooses a flow, shows the
sign-in address or code on stderr, records the account hint on the profile and
turns SDK failures into plain-language command failures. Tokens never reach
any output; authorization stays on the server.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import click

from firefly_weave.cli.auth_console import (
    EXIT_AUTH,
    EXIT_INPUT,
    EXIT_REMOTE,
    AuthCommandError,
    Console,
    input_required,
)
from firefly_weave.cli.progress import progress

if TYPE_CHECKING:
    import httpx

    from firefly_weave.sdk.auth import LoginConfig, OAuthSession
    from firefly_weave.sdk.profiles import AccountHint, PlatformProfile, ProfileStore, WorkspaceSelection
    from firefly_weave.sdk.sign_in import IdentityCheck

Flow = Literal["browser", "device"]
FLOWS: tuple[Flow, ...] = ("browser", "device")
FLOW_NAMES = {"browser": "browser on this computer", "device": "code on another device"}

# Test seam: an HTTPX transport factory for the server, the identity provider and the API (None = network).
TRANSPORT_FACTORY: Callable[[], httpx.AsyncBaseTransport] | None = None

CONNECT_MESSAGES = {
    "WV-CONNECT-ADDRESS": "That does not look like a server address. Enter a host such as weave.example.com.",
    "WV-CONNECT-INSECURE": "Plain http:// is only allowed for this computer (localhost). Use https://.",
    "WV-CONNECT-BLOCKED": "That address is not allowed (link-local, metadata or broadcast addresses are blocked).",
    "WV-CONNECT-UNREACHABLE": "The server could not be reached. Check the address and your network connection.",
    "WV-CONNECT-TLS": "The server's secure connection could not be verified (TLS certificate problem).",
    "WV-CONNECT-TIMEOUT": "The server did not answer in time.",
    "WV-CONNECT-REDIRECT": "The server redirects to another address; enter that address directly if you trust it.",
    "WV-CONNECT-NOT-WEAVE": "That address answered, but not as a Firefly Weave server.",
    "WV-CONNECT-INCOMPATIBLE": "That Firefly Weave server is a version this CLI cannot connect to.",
    "WV-CONNECT-NO-SIGN-IN": (
        "This server does not publish sign-in settings. Ask its administrator for a connection file "
        "and run: weave auth setup --auth-config FILE"
    ),
    "WV-CONNECT-PROVIDER": "The identity provider this server proposes could not be checked.",
}
AUTH_MESSAGES = {
    "WV-AUTH-DENIED": "Sign-in was denied, or the sign-in code expired.",
    "WV-AUTH-EXPIRED": "Sign-in was not completed in time.",
    "WV-AUTH-PROVIDER": "The identity provider did not answer as expected. Try again later.",
    "WV-AUTH-TRUST": "The identity provider's settings are not trusted (insecure address or endpoints on other hosts).",
    "WV-AUTH-DEVICE-UNAVAILABLE": "This identity provider does not offer sign-in with a code; use --flow browser.",
    "WV-AUTH-CALLBACK": "The browser returned an invalid sign-in response. Try again.",
    "WV-AUTH-SUPERSEDED": "Another sign-in or sign-out for this platform happened at the same time. Try again.",
    "WV-AUTH-REQUIRED": "You are not signed in to this platform, or the sign-in can no longer be renewed.",
    "WV-AUTH-OFFLINE": "The identity provider could not be reached to renew the sign-in. Check your connection.",
    "WV-AUTH-TARGET": "The saved sign-in belongs to another server address.",
    "WV-AUTH-INPUT": "The sign-in options are not valid.",
}
FLOW_REFUSED = {
    "browser": "Your administrator allows only sign-in with a code for this platform.",
    "device": "Your administrator allows only browser sign-in for this platform.",
}
SWITCH_WITH_CODE = (
    "This platform allows only sign-in with a code, so the identity provider cannot be asked to offer another account.",
    "When you open the sign-in address below, choose the account you want to use in that browser "
    "(sign out of the current account there first if the page signs you in automatically).",
)


def transport() -> httpx.AsyncBaseTransport | None:
    return TRANSPORT_FACTORY() if TRANSPORT_FACTORY is not None else None


def quote(value: str) -> str:
    return shlex.quote(value)


def printable(value: str) -> str:
    """Server or provider text made safe for a terminal: control characters (escape sequences) become '?'."""
    return "".join(character if character.isprintable() else "?" for character in value)


# --- failures ----------------------------------------------------------------------------------------------------


def account_lines(hint: AccountHint | None, *, intro: str) -> list[str]:
    lines = [intro]
    if hint is None:
        lines.append("  (The identity provider did not share account details; your administrator can look you up.)")
        return lines
    lines += [f"  Identity provider: {hint.issuer}", f"  Provider id:       {hint.provider_id}"]
    lines.append(f"  Subject:           {hint.subject}")
    if hint.display_name:
        lines.append(f"  Name:              {hint.display_name}")
    return lines


def translate(error: BaseException, *, profile: str | None = None) -> AuthCommandError | None:
    """Plain-language failure for an SDK error; None when the error is not a known, value-free kind."""
    from firefly_weave.sdk.auth import AuthError
    from firefly_weave.sdk.credentials import CredentialError
    from firefly_weave.sdk.errors import TransportError, WeaveError
    from firefly_weave.sdk.profiles import ProfileError
    from firefly_weave.sdk.server_config import ServerConfigError
    from firefly_weave.sdk.sign_in import SignInError

    login_hint = f"Sign in with: weave auth login --profile {quote(profile)}" if profile else None
    named = {"profile": profile} if profile else {}
    if isinstance(error, ServerConfigError):
        message = CONNECT_MESSAGES.get(error.code, "The server could not be used.")
        lines: list[str] = []
        if error.code == "WV-CONNECT-REDIRECT" and error.detail:
            lines.append(f"It redirects to: {printable(error.detail)}")
        if error.code == "WV-CONNECT-PROVIDER" and error.detail:
            lines.append(AUTH_MESSAGES.get(error.detail, "The identity provider could not be reached."))
        return AuthCommandError(error.code, message, error.exit_code, details={"detail": error.detail}, lines=lines)
    if isinstance(error, ProfileError):
        lines = []
        if error.code == "WV-PROFILE-NONE":
            lines.append("Connect with: weave auth setup SERVER   (or pass --profile NAME)")
        elif error.code == "WV-PROFILE-NOT-FOUND":
            lines.append("List saved platforms with: weave auth profiles")
        elif error.code == "WV-PROFILE-EXISTS":
            lines.append("Choose another --name, or pass --replace to replace the saved platform.")
        return AuthCommandError(error.code, error.message, error.exit_code, lines=lines)
    if isinstance(error, SignInError):
        if error.code == "WV-AUTH-NOT-LINKED":
            return AuthCommandError(
                error.code,
                "You signed in, but this platform does not recognize your account yet.",
                EXIT_AUTH,
                details={"account": account_json(error.account), **named},
                lines=account_lines(error.account, intro="Ask an administrator to link your account. Share:"),
            )
        return AuthCommandError(error.code, "Sign-in could not be verified.", EXIT_AUTH)
    if isinstance(error, AuthError):
        lines = [login_hint] if login_hint and error.code in {"WV-AUTH-REQUIRED", "WV-AUTH-TARGET"} else []
        return AuthCommandError(
            error.code,
            AUTH_MESSAGES.get(error.code, "Sign-in failed."),
            error.exit_code,
            details=named,
            lines=lines,
        )
    if isinstance(error, CredentialError):
        return AuthCommandError(
            "WV-AUTH-STORE",
            "The credential store is not available or not private.",
            EXIT_INPUT,
            lines=[
                "Unlock the system credential store, or save the platform with a private credential file:",
                "  weave auth setup SERVER --replace --credential-store file --credential-file PATH",
                "  (PATH must be in a folder only you can read, mode 700)",
            ],
        )
    if isinstance(error, TransportError):
        return AuthCommandError("WV-TRANSPORT", "The platform could not be reached.", EXIT_REMOTE)
    if isinstance(error, WeaveError):
        status = int(error.status)
        if status == 401:
            message = "The platform did not accept the sign-in."
            return AuthCommandError(error.code, message, EXIT_AUTH, lines=[login_hint] if login_hint else [])
        if status == 403:
            return AuthCommandError(error.code, "The platform denied this request.", EXIT_AUTH)
        return AuthCommandError(error.code, "The platform request failed.", EXIT_AUTH if status < 500 else EXIT_REMOTE)
    if isinstance(error, ImportError):
        return AuthCommandError(
            "WV-AUTH-CONFIG", "Signing in needs the client extra: pip install 'firefly-weave[client]'.", EXIT_INPUT
        )
    if isinstance(error, OSError | ValueError):
        return AuthCommandError("WV-AUTH-CONFIG", "The local configuration or input is not valid.", EXIT_INPUT)
    return None


# --- sessions and flows ------------------------------------------------------------------------------------------


def open_session(profile: PlatformProfile) -> OAuthSession:
    """The profile's OAuth session (native or explicit file store); raises CredentialError when unavailable."""
    from firefly_weave.sdk.sign_in import session_for

    return session_for(profile, transport_factory=TRANSPORT_FACTORY)


def local_status(session: OAuthSession) -> dict[str, Any] | None:
    """Best-effort local credential status, used to report state after a failure."""
    try:
        return session.status()
    except Exception:
        return None


def gui_browser_available() -> bool:
    """A browser window can plausibly open here: not over SSH, a display on Linux, a runnable browser."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def launch_browser(url: str, *, open_browser: bool) -> bool:
    """Open the sign-in address in the system browser; a seam for tests (never logs the address)."""
    if not open_browser:
        return False
    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


def require_allowed_flow(
    requested: str, allowed: Sequence[str], *, switch_account: bool, profile: str | None = None
) -> None:
    """Refuse an explicit `--flow` the administrator does not allow (`WV-AUTH-FLOW`, exit 2), before any network use.

    Switching account is the exception: it uses whichever flow is allowed (see `choose_flow`).
    `profile` is None during setup, before the platform has a saved name.
    """
    wanted = {"browser": "browser", "pkce": "browser", "device": "device"}.get(requested)
    if wanted is None or wanted in allowed or switch_account:
        return
    other = "device" if wanted == "browser" else "browser"
    details: dict[str, Any] = {"allowed": list(allowed)}
    if profile is not None:
        details["profile"] = profile
    raise AuthCommandError(
        "WV-AUTH-FLOW",
        FLOW_REFUSED[wanted],
        EXIT_INPUT,
        details=details,
        lines=[f"Sign in with --flow {other}, or leave out --flow."],
    )


def choose_flow(
    requested: str,
    *,
    switch_account: bool = False,
    no_browser: bool = False,
    allowed: Sequence[str] | None = None,
) -> Flow:
    """`auto` is the browser when one can open here, otherwise a code on another device.

    Switching account uses the browser (the provider shows its sign-in page again); a platform that
    allows only codes switches with a code, and the caller explains how to pick the account.
    """
    permitted = set(FLOWS if allowed is None else allowed)
    if switch_account:
        if requested == "device" and "browser" in permitted:
            raise input_required("--flow browser", "Switching account needs the browser sign-in; remove --flow device.")
        if "browser" in permitted:
            return "browser"
        if "device" in permitted:
            return "device"
        raise AuthCommandError("WV-AUTH-INPUT", "This platform allows no sign-in method to switch account with.", 2)
    if requested in ("browser", "pkce"):
        flow: Flow = "browser"
    elif requested == "device":
        flow = "device"
    elif not no_browser and "browser" in permitted and gui_browser_available():
        flow = "browser"
    else:
        flow = "device" if "device" in permitted else "browser"
    if flow not in permitted:
        if flow == "device":
            raise AuthCommandError("WV-AUTH-DEVICE-UNAVAILABLE", AUTH_MESSAGES["WV-AUTH-DEVICE-UNAVAILABLE"], 2)
        raise AuthCommandError("WV-AUTH-INPUT", "This platform does not allow browser sign-in; use --flow device.", 2)
    return flow


def available_flows(login: LoginConfig) -> list[str]:
    """The flows the identity provider serves to this client (discovery with the sign-in trust rules)."""
    from firefly_weave.sdk.server_config import probe_sign_in

    check = asyncio.run(probe_sign_in(login, transport=transport()))
    flows: list[str] = [flow for flow in FLOWS if getattr(check, flow)]
    if not flows:
        raise AuthCommandError(
            "WV-CONNECT-PROVIDER", "The identity provider offers no sign-in method for this client.", EXIT_REMOTE
        )
    return flows


def account_hint(identity: Any) -> AccountHint | None:
    from pydantic import ValidationError

    from firefly_weave.sdk.profiles import AccountHint

    if not isinstance(identity, dict):
        return None
    try:
        return AccountHint.model_validate(identity)
    except ValidationError:
        return None


def sign_in(
    console: Console,
    store: ProfileStore,
    profile: PlatformProfile,
    session: OAuthSession,
    *,
    flow: Flow,
    open_browser: bool,
    switch_account: bool = False,
) -> tuple[PlatformProfile, dict[str, Any]]:
    """Run the sign-in with the address or code on stderr and a spinner while waiting; save the account hint."""
    stack = ExitStack()

    def waiting(label: str) -> None:
        stack.enter_context(progress(label))

    def show_address(url: str) -> Any:
        if open_browser:
            console.say("Opening your browser to sign in. If it does not open, use this address:")
        else:
            console.say("Open this address in a browser on this computer to sign in:")
        console.say(f"  {printable(url)}")
        waiting("Waiting for you to finish signing in (Ctrl+C cancels)")
        return asyncio.to_thread(launch_browser, url, open_browser=open_browser)

    def show_code(uri: str, code: str) -> None:
        console.say("To sign in, open this address in a browser on any device:")
        console.say(f"  {printable(uri)}")
        console.say("and enter this code:")
        console.say("  " + click.style(printable(code), bold=True))
        waiting("Waiting for you to approve the sign-in (Ctrl+C cancels)")

    with stack:
        result = asyncio.run(
            session.login(
                flow=flow,
                instructions=show_code,
                browser=show_address,
                # A code sign-in has no account prompt; the person chooses the account in the browser.
                prompt="login" if switch_account and flow == "browser" else None,
            )
        )
    # Replace the hint on every sign-in: an absent hint must not keep showing a previous account.
    profile = store.set_account(profile.name, account_hint(result.get("identity")))
    return profile, result


# --- workspaces --------------------------------------------------------------------------------------------------


def read_workspaces(profile: PlatformProfile, session: OAuthSession) -> IdentityCheck:
    """Live identity and authorized workspaces (refreshes the sign-in as needed)."""
    from firefly_weave.sdk.sign_in import verify_sign_in

    return asyncio.run(verify_sign_in(profile, session, transport=transport()))


def workspace_flags(tenant: UUID | None, project: UUID | None, environment: UUID | None) -> bool:
    """True when all three were given; a partial selection names the missing flag."""
    given = {"--tenant": tenant, "--project": project, "--environment": environment}
    if not any(given.values()):
        return False
    missing = [flag for flag, value in given.items() if value is None]
    if missing:
        raise input_required(missing[0], f"A workspace needs --tenant, --project and --environment; add {missing[0]}.")
    return True


def select_workspace(
    console: Console,
    check: IdentityCheck,
    *,
    tenant: UUID | None,
    project: UUID | None,
    environment: UUID | None,
    current: WorkspaceSelection | None = None,
) -> WorkspaceSelection | None:
    """Pick from the authorized list by flags, the only option, or an interactive numbered picker."""
    from firefly_weave.sdk.profiles import WorkspaceSelection

    if workspace_flags(tenant, project, environment):
        for option in check.workspaces:
            if (option.tenant_id, option.project_id, option.environment_id) == (tenant, project, environment):
                return option.selection()
        if check.truncated:
            # The server listed only part of the workspaces; it still authorizes every request itself.
            assert tenant is not None and project is not None and environment is not None
            return WorkspaceSelection(tenant_id=tenant, project_id=project, environment_id=environment)
        raise AuthCommandError(
            "WV-AUTH-WORKSPACE",
            "This account has no access to that tenant, project and environment.",
            EXIT_AUTH,
            lines=["List the workspaces you can use with: weave auth workspace"],
        )
    if not check.workspaces:
        return None
    if len(check.workspaces) == 1:
        only = check.workspaces[0]
        console.say(f"Using the only workspace available: {printable(only.label)}")
        return only.selection()
    # Names come from the platform: never let them carry terminal escape sequences.
    labels = [printable(option.label) for option in check.workspaces]
    console.say("Workspaces you can use (Tenant / Project / Environment):")
    if current is not None:
        console.say(f"Currently selected: {workspace_label(current)}")
    index = console.choose("Workspace number", labels, flag="--tenant/--project/--environment")
    return check.workspaces[index].selection()


# --- presentation ------------------------------------------------------------------------------------------------


def account_json(hint: AccountHint | None) -> dict[str, Any] | None:
    return hint.model_dump(mode="json") if hint is not None else None


def account_label(hint: AccountHint | None) -> str:
    if hint is None:
        return "unknown account"
    return hint.display_name or hint.subject


def workspace_json(selection: WorkspaceSelection | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {**selection.model_dump(mode="json"), "label": workspace_label(selection)}


def workspace_label(selection: WorkspaceSelection | None, *, empty: str = "-") -> str:
    if selection is None:
        return empty
    return " / ".join(
        name or str(identifier)
        for name, identifier in (
            (selection.tenant_name, selection.tenant_id),
            (selection.project_name, selection.project_id),
            (selection.environment_name, selection.environment_id),
        )
    )


def credentials_label(profile: PlatformProfile) -> str:
    if profile.credential_store == "file" and profile.credential_file is not None:
        return f"file:{profile.credential_file}"
    return "system credential store"


def identity_provider(login: LoginConfig) -> str:
    """The identity provider origin a person reviews (the issuer host)."""
    from firefly_weave.sdk.auth import AuthError, origin

    try:
        return origin(login.issuer, allow_loopback_http=login.allow_loopback_http)
    except AuthError:
        return login.issuer


def expiry_label(expires_at: Any) -> str:
    if not isinstance(expires_at, int | float):
        return ""
    return datetime.fromtimestamp(expires_at).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def flows_label(flows: Sequence[str]) -> str:
    return ", ".join(FLOW_NAMES.get(flow, flow) for flow in flows) or "none"
