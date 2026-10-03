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

"""`weave auth setup`: connect to a platform in four steps (server, review, sign in, workspace).

The server only proposes its identity provider; the person reviews the
provider host once and the saved profile pins it. A connection file from an
operator (`--auth-config`) replaces discovery. The profile holds no secrets;
credentials go to the system credential store or an explicit private file.
Every question has a flag, so the same setup runs unattended.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from firefly_weave.cli import auth_console, auth_flow
from firefly_weave.cli.auth_console import EXIT_AUTH, EXIT_INPUT, AuthCommandError, Console, input_required

if TYPE_CHECKING:
    from firefly_weave.contracts.client_configuration import ClientConfiguration, SignInOption
    from firefly_weave.sdk.auth import LoginConfig
    from firefly_weave.sdk.profiles import PlatformProfile, ProfileStore, WorkspaceSelection

MAX_CONNECTION_FILE = 65536
SLUG = re.compile(r"[^a-z0-9._-]+")


@dataclass
class SetupState:
    """What setup has already changed, so a failure or Ctrl+C reports it precisely."""

    profile: str | None = None
    server: str | None = None
    saved: bool = False
    authenticated: bool = False
    workspace: dict[str, Any] | None = None
    next: list[str] = field(default_factory=list)

    def details(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "server": self.server,
            "saved": self.saved,
            "authenticated": self.authenticated,
            "workspace": self.workspace,
        }

    def lines(self) -> list[str]:
        if not self.saved or self.profile is None:
            return ["Nothing was saved."]
        name = auth_flow.quote(self.profile)
        if not self.authenticated:
            return [
                f"Platform '{self.profile}' is saved, but you are not signed in.",
                f"Sign in later with: weave auth login --profile {name}",
            ]
        return [
            f"Platform '{self.profile}' is saved and you are signed in, but no workspace was chosen.",
            f"Choose one later with: weave auth workspace --profile {name}",
        ]

    def cancelled(self) -> AuthCommandError:
        details, lines = self.details(), self.lines()
        return AuthCommandError("WV-AUTH-CANCELLED", "Cancelled.", EXIT_AUTH, details=details, lines=lines)

    def decorate(self, error: AuthCommandError) -> AuthCommandError:
        if not self.saved:
            return AuthCommandError(
                error.code,
                error.message,
                error.exit_code,
                details={**self.details(), **error.details},
                lines=[*error.lines, "Nothing was saved."],
            )
        return AuthCommandError(
            error.code,
            error.message,
            error.exit_code,
            details={**self.details(), **error.details},
            lines=[*error.lines, *self.lines()],
        )


def run_setup(options: dict[str, Any]) -> None:
    console = auth_console.console_for(options["output"])
    state = SetupState()
    auth_console.run(
        console,
        lambda: _setup(console, state, options),
        on_cancel=state.cancelled,
        on_failure=state.decorate,
        profile=None,
    )


def load_login_config(path: Path) -> LoginConfig:
    """Read an operator connection file (exact `LoginConfig` JSON, at most 64 KiB)."""
    from pydantic import ValidationError

    from firefly_weave.sdk.auth import AuthError, LoginConfig

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONNECTION_FILE + 1)
        if len(raw) > MAX_CONNECTION_FILE:
            raise ValueError("Connection file exceeds its bound")
        return LoginConfig.model_validate_json(raw)
    except (OSError, ValueError, ValidationError, AuthError):
        raise AuthCommandError(
            "WV-AUTH-CONFIG",
            "The connection file cannot be read or is not a valid sign-in configuration.",
            EXIT_INPUT,
        ) from None


def _connection_file_server(login: LoginConfig) -> str:
    """The file's API origin under the same address rules as a typed server (no link-local or metadata hosts)."""
    from firefly_weave.sdk.server_config import ServerConfigError, normalize_server_address

    try:
        server = normalize_server_address(login.target)
    except ServerConfigError as error:
        code: str | None = error.code
    else:
        if server == login.target:
            return server
        code = None
    raise AuthCommandError(
        "WV-AUTH-CONFIG",
        "The server address in the connection file is not allowed or not an exact origin "
        "(for example https://weave.example.com).",
        EXIT_INPUT,
        details={"detail": code},
    )


def _rebind(login: LoginConfig, name: str) -> LoginConfig:
    from firefly_weave.sdk.auth import LoginConfig

    # The profile name is the credential account, so one profile owns one credential record.
    return LoginConfig.model_validate({**login.model_dump(), "account": name})


def suggest_name(label: str | None, server: str, taken: set[str], *, reuse: bool = False) -> str:
    """A valid, readable profile name: the server display name or host as a slug, unique ignoring case."""
    from firefly_weave.sdk.profiles import ProfileError, profile_name

    base = ""
    for source in (label or "", urlsplit(server).hostname or ""):
        base = SLUG.sub("-", source.lower()).strip("-._")[:60]
        try:
            base = profile_name(base)
            break
        except ProfileError:
            base = ""
    base = base or "weave"
    if reuse:
        return base
    candidate, number = base, 2
    while candidate.casefold() in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def _choose_name(
    console: Console, store: ProfileStore, suggested: str, given: str | None, replace: bool
) -> tuple[str, PlatformProfile | None]:
    """The profile name and the saved profile it replaces (if any); replacing is always explicit."""
    from firefly_weave.sdk.profiles import ProfileError, profile_name

    existing = {key.casefold(): value for key, value in store.load().profiles.items()}

    def resolve(name: str) -> tuple[str, PlatformProfile | None] | None:
        current = existing.get(name.casefold())
        if current is None:
            return name, None
        if replace:
            return current.name, current
        if not console.interactive:
            raise ProfileError(
                "WV-PROFILE-EXISTS", f"A saved platform is already named '{current.name}'; pass --replace."
            )
        question = f"A saved platform is already named '{current.name}'. Replace it?"
        return (current.name, current) if console.confirm(question, flag="--replace") else None

    if given is not None or not console.interactive:
        chosen = resolve(profile_name(given if given is not None else suggested))
        if chosen is not None:
            return chosen
    while True:
        answer = console.ask("Name for this platform", flag="--name", default=suggested)
        try:
            chosen = resolve(profile_name(answer))
        except ProfileError as error:
            if error.code != "WV-PROFILE-NAME":
                raise
            console.say(error.message)
            continue
        if chosen is not None:
            return chosen


def _discover(console: Console, address: str | None) -> tuple[str, ClientConfiguration, list[SignInOption]]:
    from firefly_weave.sdk.server_config import (
        ServerConfigError,
        fetch_client_configuration,
        normalize_server_address,
        require_sign_in,
    )

    while True:
        if address is None:
            address = console.ask("Server address (for example weave.example.com)", flag="SERVER")
        try:
            server = normalize_server_address(address)
            console.say(f"Contacting {server} ...")
            configuration = asyncio.run(fetch_client_configuration(server, transport=auth_flow.transport()))
            return server, configuration, require_sign_in(configuration)
        except ServerConfigError as error:
            failure = auth_flow.translate(error)
            assert failure is not None
            if not console.interactive:
                raise failure from None
            console.say(failure.message)
            for line in failure.lines:
                console.say(line)
            console.say("Enter another address, or press Ctrl+C to stop.")
            address = None


def _pick_option(console: Console, options: list[SignInOption], provider: str | None) -> SignInOption:
    from firefly_weave.sdk.server_config import issuer_origin

    if provider is not None:
        for option in options:
            if option.provider_id == provider:
                return option
        raise input_required(
            "--provider",
            "No sign-in option has that provider id. Available: " + ", ".join(o.provider_id for o in options) + ".",
        )
    if len(options) == 1:
        return options[0]
    console.say("This server offers several ways to sign in:")
    labels = [
        auth_flow.printable(f"{o.display_name} ({issuer_origin(o)}) [--provider {o.provider_id}]") for o in options
    ]
    return options[console.choose("Sign-in option number", labels, flag="--provider")]


def _private_folder(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700


def _validate_flags(o: dict[str, Any]) -> None:
    workspace = auth_flow.workspace_flags(o["tenant"], o["project"], o["environment"])
    if workspace and o["skip_workspace"]:
        raise input_required("--skip-workspace", "Use either --skip-workspace or --tenant/--project/--environment.")
    if workspace and o["no_login"]:
        raise input_required("--no-login", "Choosing a workspace needs a sign-in; remove --no-login.")
    if o["credential_store"] == "file":
        if o["credential_file"] is None:
            raise input_required("--credential-file", "The file credential store needs --credential-file PATH.")
        if os.name != "posix":
            raise AuthCommandError(
                "WV-AUTH-STORE",
                "The file credential store is available on macOS and Linux only; use the system credential store.",
                EXIT_INPUT,
            )
        o["credential_file"] = Path(o["credential_file"]).absolute()
        if not _private_folder(o["credential_file"].parent):
            raise AuthCommandError(
                "WV-AUTH-STORE",
                "The credential file must be in an existing folder that only you can read (mode 700).",
                EXIT_INPUT,
            )
    elif o["credential_file"] is not None:
        raise input_required("--credential-store file", "--credential-file needs --credential-store file.")


def _setup(console: Console, state: SetupState, o: dict[str, Any]) -> None:
    from firefly_weave.sdk.auth import AuthError
    from firefly_weave.sdk.profiles import PlatformProfile, ProfileStore
    from firefly_weave.sdk.server_config import (
        issuer_origin,
        login_config_for,
        normalize_server_address,
        probe_sign_in,
    )

    _validate_flags(o)
    store = ProfileStore()

    # Step 1: server.
    console.step(1, "Server")
    configuration: ClientConfiguration | None = None
    option: SignInOption | None = None
    imported: LoginConfig | None = None
    if o["auth_config"] is not None:
        imported = load_login_config(o["auth_config"])
        server = _connection_file_server(imported)
        if o["server"] is not None and normalize_server_address(o["server"]) != server:
            raise input_required(
                "SERVER", "SERVER must match the server address in the connection file (or leave it out)."
            )
        console.say(f"Using the connection file for {server}.")
        options: list[SignInOption] = []
    else:
        server, configuration, options = _discover(console, o["server"])
        found = auth_flow.printable(configuration.display_name or "a Firefly Weave server")
        console.say(f"Found {found} at {server}.")
    state.server = server

    # Step 2: review and trust.
    console.step(2, "Review")
    if imported is None:
        option = _pick_option(console, options, o["provider"])
        provider_origin = issuer_origin(option)
        label = configuration.display_name if configuration is not None else None
    else:
        if o["provider"] is not None and o["provider"] != imported.provider_id:
            raise input_required("--provider", "--provider does not match the provider in the connection file.")
        provider_origin = auth_flow.identity_provider(imported)
        label = None
    taken = {key.casefold() for key in store.load().profiles}
    suggested = suggest_name(label, server, taken, reuse=bool(o["replace"]))

    def login_for(account: str) -> LoginConfig:
        if option is not None:
            return login_config_for(option, server=server, account=account)
        assert imported is not None
        return _rebind(imported, account)

    provisional = login_for(suggested)
    allowed = list(option.flows) if option is not None else ["browser", "device"]
    if not o["no_login"]:
        # The administrator's choice is known before the identity provider is asked anything.
        auth_flow.require_allowed_flow(o["flow"], allowed, switch_account=False)
    console.say(f"Checking the identity provider at {provider_origin} ...")
    check = asyncio.run(probe_sign_in(provisional, transport=auth_flow.transport()))
    methods = [flow for flow in allowed if getattr(check, flow)]
    console.say(f"  Platform:          {auth_flow.printable(label or server)} ({server})")
    if option is not None:
        console.say(f"  Sign-in provider:  {auth_flow.printable(option.display_name)}")
    else:
        console.say(f"  Connection file:   {o['auth_config']}")
        console.say(f"  Provider id:       {provisional.provider_id}")
    console.say(f"  Identity provider: {provider_origin}")
    if provisional.trusted_endpoint_origins:
        console.say("  Also trusted:      " + ", ".join(provisional.trusted_endpoint_origins))
    console.say(f"  Sign-in methods:   {auth_flow.flows_label(methods)}")
    console.say(f"  Login client:      {provisional.client_id} (scopes: {' '.join(provisional.scopes)})")
    if not methods:
        raise AuthCommandError(
            "WV-CONNECT-PROVIDER",
            "The identity provider offers no sign-in method this platform allows.",
            3,
        )
    # Refuse an impossible --flow before anything is saved.
    flow = (
        None if o["no_login"] else auth_flow.choose_flow(o["flow"], no_browser=bool(o["no_browser"]), allowed=methods)
    )
    name, replaced = _choose_name(console, store, suggested, o["name"], bool(o["replace"]))
    login = login_for(name)
    console.say(f"  Profile name:      {name}" + (" (replaces the saved platform)" if replaced else ""))
    if o["yes"]:
        console.say("Trust confirmed with --yes.")
    elif not console.confirm(f"Trust {provider_origin} to sign you in to {server}?", flag="--yes", default=False):
        raise AuthCommandError("WV-AUTH-CANCELLED", "The identity provider was not confirmed.", EXIT_AUTH)
    # Identical sign-in settings and credential store mean the same stored credentials (and their account).
    kept = replaced if replaced is not None and _same_credentials(replaced, login, o) else None
    profile = PlatformProfile(
        name=name,
        login=login,
        source="server" if option is not None else "file",
        display_name=configuration.display_name if configuration is not None else None,
        provider_name=option.display_name if option is not None else None,
        credential_store=o["credential_store"],
        credential_file=o["credential_file"],
        account=kept.account if kept is not None else None,
        # The administrator's allowed flows (server settings); a connection file allows both.
        flows=list(option.flows) if option is not None else ["browser", "device"],
    )
    # Fail before saving when the credential store cannot be used at all.
    session = auth_flow.open_session(profile)
    profile = store.save(profile, activate=True, replace=replaced is not None)
    state.profile, state.saved = profile.name, True
    if replaced is not None and kept is None:
        _forget(replaced)
    console.say(
        f"Saved platform '{profile.name}' to {store.location} "
        "(server address, identity provider and workspace choice only; no passwords or tokens)."
    )

    # Step 3: sign in.
    console.step(3, "Sign in")
    authenticated = False
    if flow is None:
        # A replaced platform with the same credentials may still be signed in; report the real state.
        status = auth_flow.local_status(session) if kept is not None else None
        authenticated = state.authenticated = bool(status and status.get("authenticated"))
        console.say("Skipped (--no-login)." + (" You are still signed in." if authenticated else ""))
    else:
        try:
            profile, result = auth_flow.sign_in(
                console, store, profile, session, flow=flow, open_browser=not o["no_browser"]
            )
        except AuthError:
            status = auth_flow.local_status(session)
            state.authenticated = bool(status and status.get("authenticated"))
            raise
        authenticated = state.authenticated = bool(result.get("authenticated"))
        console.say(f"Signed in as {auth_flow.account_label(profile.account)}.")

    # Step 4: workspace.
    console.step(4, "Workspace")
    notice: str | None = None
    if o["no_login"]:
        console.say("Skipped (--no-login)." if authenticated else "Skipped: sign in first.")
    elif o["skip_workspace"]:
        console.say("Skipped (--skip-workspace).")
    else:
        identity = auth_flow.read_workspaces(profile, session)
        selection = auth_flow.select_workspace(
            console, identity, tenant=o["tenant"], project=o["project"], environment=o["environment"]
        )
        if selection is None:
            notice = "WV-AUTH-NO-ACCESS"
            for line in auth_flow.account_lines(
                profile.account,
                intro="Your account has no access to any workspace on this platform yet. "
                "Ask an administrator for access and share:",
            ):
                console.say(line)
        else:
            profile = store.set_workspace(profile.name, selection)
            state.workspace = auth_flow.workspace_json(profile.workspace)
            console.say(f"Workspace: {auth_flow.workspace_label(profile.workspace)}")
    _summary(console, store, profile, authenticated=authenticated, notice=notice, skipped=bool(o["skip_workspace"]))


def _same_credentials(replaced: PlatformProfile, login: LoginConfig, o: dict[str, Any]) -> bool:
    return (
        replaced.login.binding == login.binding
        and replaced.credential_store == o["credential_store"]
        and replaced.credential_file == o["credential_file"]
    )


def _forget(replaced: PlatformProfile) -> None:
    """Best effort: drop the replaced profile's local credentials (they were bound to other settings)."""
    try:
        session = auth_flow.open_session(replaced)
        asyncio.run(session.logout(revoke=False))
    except Exception:
        pass


def _next_steps(profile: PlatformProfile, *, authenticated: bool, notice: str | None) -> list[str]:
    name = auth_flow.quote(profile.name)
    if not authenticated:
        return [f"weave auth login --profile {name}"]
    if profile.workspace is None:
        if notice is not None:
            return [f"weave auth workspace --profile {name}   (after an administrator grants access)"]
        return [f"weave auth workspace --profile {name}"]
    return ["weave studio", f"weave auth status --profile {name} --check"]


def _summary(
    console: Console,
    store: ProfileStore,
    profile: PlatformProfile,
    *,
    authenticated: bool,
    notice: str | None,
    skipped: bool,
) -> None:
    active = store.load().active == profile.name
    steps = _next_steps(profile, authenticated=authenticated, notice=notice)
    credentials = auth_flow.credentials_label(profile)
    payload: dict[str, Any] = {
        "profile": profile.name,
        "server": profile.server,
        "active": active,
        "authenticated": authenticated,
        "account": auth_flow.account_json(profile.account) if authenticated else None,
        "workspace": auth_flow.workspace_json(profile.workspace),
        "saved": {"profiles_file": store.location, "credentials": credentials},
        "next": steps,
    }
    if notice is not None:
        payload["notice"] = notice
    workspace = _workspace_text(profile.workspace, notice=notice, skipped=skipped)
    text = [
        f"Platform '{profile.name}' is set up" + (" and active." if active else "."),
        f"  Server:      {profile.server}",
        f"  Signed in:   {'yes, as ' + auth_flow.account_label(profile.account) if authenticated else 'no'}",
        f"  Workspace:   {workspace}",
        f"  Saved to:    {store.location} (no passwords or tokens)",
        f"  Credentials: {credentials}",
        "Next:",
        *(f"  {step}" for step in steps),
    ]
    console.result(payload, text)


def _workspace_text(selection: WorkspaceSelection | None, *, notice: str | None, skipped: bool) -> str:
    if selection is not None:
        return auth_flow.workspace_label(selection)
    if notice is not None:
        return "none (this account has no access yet)"
    return "not chosen" if skipped else "none"
