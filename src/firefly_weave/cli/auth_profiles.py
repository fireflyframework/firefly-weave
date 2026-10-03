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

"""Profile-mode `weave auth` commands: login, status, logout, profiles, use, remove and workspace.

A command acts on `--profile NAME` (or `WEAVE_PROFILE`), else on the active
profile. Listing profiles never touches the credential store; `status --all`
reads it for every profile only when asked. Sign-out clears the account hint
and, by default, asks the identity provider to revoke the refresh token.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import click

from firefly_weave.cli import auth_console, auth_flow
from firefly_weave.cli.auth_console import EXIT_AUTH, EXIT_REMOTE, AuthCommandError, Console, input_required

if TYPE_CHECKING:
    from firefly_weave.sdk.profiles import PlatformProfile, ProfileStore


def _store() -> ProfileStore:
    from firefly_weave.sdk.profiles import ProfileStore

    return ProfileStore()


def _command(options: dict[str, Any], body: Callable[[Console], None], *, profile: str | None = None) -> None:
    console = auth_console.console_for(options.get("output") or "text")

    def selected() -> str | None:
        # The resolved profile (also the active one) names the next command in failure guidance.
        return options.get("_resolved") or profile or options.get("profile_name")

    auth_console.run(console, lambda: body(console), profile=selected)


def describe(profile: PlatformProfile, *, active: bool, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Nonsecret profile view; the credential state is added only when it was read."""
    value: dict[str, Any] = {
        "profile": profile.name,
        "server": profile.server,
        "active": active,
        "display_name": profile.display_name,
        "provider_id": profile.login.provider_id,
        "provider_name": profile.provider_name,
        "identity_provider": auth_flow.identity_provider(profile.login),
        "source": profile.source,
        "account": auth_flow.account_json(profile.account),
        "workspace": auth_flow.workspace_json(profile.workspace),
        "credentials": auth_flow.credentials_label(profile),
    }
    if status is not None:
        for key in ("authenticated", "state", "expires_at", "refresh_available", "reauthentication_required"):
            value[key] = status.get(key)
    return value


def _usable(status: dict[str, Any]) -> bool:
    return bool(status.get("authenticated") or status.get("refresh_available"))


def _sign_in_text(status: dict[str, Any] | None, profile: PlatformProfile, *, verified: bool = False) -> str:
    if status is None:
        return "unknown (credential store unavailable)"
    who = auth_flow.account_label(profile.account) if profile.account else None
    suffix = (f" as {who}" if who else "") + (", verified with the platform" if verified else "")
    if status.get("authenticated"):
        until = auth_flow.expiry_label(status.get("expires_at"))
        renews = ", renews automatically" if status.get("refresh_available") else ""
        return f"signed in{suffix}" + (f" (until {until}{renews})" if until else "")
    if status.get("refresh_available"):
        return f"signed in{suffix} (renews on next use)"
    return {"expired": "expired", "in_progress": "sign-in in progress"}.get(str(status.get("state")), "signed out")


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return ["".join(cell.ljust(width + 2) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows]


def _resolve(store: ProfileStore, options: dict[str, Any]) -> PlatformProfile:
    profile = store.resolve(options.get("profile_name"))
    options["_resolved"] = profile.name
    return profile


# --- login / status / logout -------------------------------------------------------------------------------------


def login(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        store = _store()
        profile = _resolve(store, options)
        switch_account = bool(options["switch_account"])
        # The administrator's allowed flows come first: a refused --flow never reaches the network.
        auth_flow.require_allowed_flow(
            options["flow"], profile.flows, switch_account=switch_account, profile=profile.name
        )
        session = auth_flow.open_session(profile)
        console.say(f"Signing in to '{profile.name}' ({profile.server}).")
        # Check the provider first: a sign-in attempt replaces the current sign-in, so an unreachable
        # provider or an unavailable flow must fail before it starts.
        offered = auth_flow.available_flows(profile.login)
        allowed = [flow for flow in profile.flows if flow in offered]
        if not allowed:
            raise AuthCommandError(
                "WV-CONNECT-PROVIDER",
                "The identity provider offers no sign-in method this platform allows.",
                EXIT_REMOTE,
                details={"profile": profile.name, "allowed": list(profile.flows)},
            )
        flow = auth_flow.choose_flow(
            options["flow"], switch_account=switch_account, no_browser=bool(options["no_browser"]), allowed=allowed
        )
        if switch_account and flow == "device":
            for line in auth_flow.SWITCH_WITH_CODE:
                console.say(line)
        try:
            profile, _ = auth_flow.sign_in(
                console,
                store,
                profile,
                session,
                flow=flow,
                open_browser=not options["no_browser"],
                switch_account=switch_account,
            )
        except BaseException as error:
            raise _after_failed_login(error, store, profile, session) from None
        status = session.status()
        active = store.load().active == profile.name
        console.result(
            describe(profile, active=active, status=status),
            [f"Signed in to '{profile.name}' ({profile.server}) as {auth_flow.account_label(profile.account)}."],
        )

    _command(options, body)


def _after_failed_login(
    error: BaseException, store: ProfileStore, profile: PlatformProfile, session: Any
) -> BaseException:
    """Report the real state after a failed or cancelled sign-in (an attempt replaces the old sign-in)."""
    status = auth_flow.local_status(session)
    authenticated = bool(status and status.get("authenticated"))
    if not authenticated:
        with contextlib.suppress(Exception):
            store.set_account(profile.name, None)
    name = auth_flow.quote(profile.name)
    state = [] if authenticated else [f"You are not signed in to '{profile.name}'. Try again with:"]
    lines = [*state, f"  weave auth login --profile {name}"] if state else []
    details = {"profile": profile.name, "authenticated": authenticated}
    if isinstance(error, KeyboardInterrupt | asyncio.CancelledError | click.Abort):
        return AuthCommandError("WV-AUTH-CANCELLED", "Cancelled.", EXIT_AUTH, details=details, lines=lines)
    mapped = error if isinstance(error, AuthCommandError) else auth_flow.translate(error, profile=profile.name)
    if mapped is None:
        return error
    return AuthCommandError(
        mapped.code,
        mapped.message,
        mapped.exit_code,
        details={**mapped.details, **details},
        lines=[*mapped.lines, *lines],
    )


def status(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        store = _store()
        if options["all"]:
            if options["check"]:
                raise input_required("--check", "--check verifies one platform at a time; remove --all or --check.")
            _status_all(console, store)
            return
        profile = _resolve(store, options)
        session = auth_flow.open_session(profile)
        state = session.status()
        active = store.load().active == profile.name
        value = describe(profile, active=active, status=state)
        text = [
            f"Platform:          {profile.name}" + (" (active)" if active else ""),
            f"Server:            {profile.server}",
            f"Identity provider: {value['identity_provider']}",
            f"Sign-in:           {_sign_in_text(state, profile)}",
            f"Workspace:         {auth_flow.workspace_label(profile.workspace, empty='none selected')}",
            f"Credentials:       {value['credentials']}",
        ]
        exit_code = 0 if _usable(state) else EXIT_AUTH
        if options["check"] and _usable(state):
            value, text = _check(console, store, profile, session, value, text)
        elif options["check"]:
            text.append("Not checked with the platform: sign in first.")
        if exit_code:
            text.append(f"Sign in with: weave auth login --profile {auth_flow.quote(profile.name)}")
        console.result(value, text)
        if exit_code:
            raise click.exceptions.Exit(exit_code)

    _command(options, body)


def _check(
    console: Console,
    store: ProfileStore,
    profile: PlatformProfile,
    session: Any,
    value: dict[str, Any],
    text: list[str],
) -> tuple[dict[str, Any], list[str]]:
    console.say(f"Checking your sign-in with {profile.server} ...")
    identity = auth_flow.read_workspaces(profile, session)
    refreshed = describe(profile, active=value["active"], status=session.status())
    options = [option.model_dump(mode="json") for option in identity.workspaces]
    selected = profile.workspace
    available: bool | None = None
    if selected is not None:
        wanted = (selected.tenant_id, selected.project_id, selected.environment_id)
        available = identity.truncated or any(
            (o.tenant_id, o.project_id, o.environment_id) == wanted for o in identity.workspaces
        )
    refreshed.update(
        identity={"principal_id": str(identity.identity.principal_id), "kind": identity.identity.kind},
        workspaces=options,
        workspace_available=available,
    )
    if identity.code is not None:
        refreshed["notice"] = identity.code
    lines = [line for line in text if not line.startswith("Sign-in:")]
    lines.insert(3, f"Sign-in:           {_sign_in_text(session.status(), profile, verified=True)}")
    lines.append(f"Workspaces you can use: {len(options)}" + (" (list truncated)" if identity.truncated else ""))
    if available is False:
        name = auth_flow.quote(profile.name)
        lines.append(f"The selected workspace is no longer available. Run: weave auth workspace --profile {name}")
    if identity.code is not None:
        lines += auth_flow.account_lines(
            profile.account, intro="This account has no access to any workspace yet. Ask an administrator, sharing:"
        )
    return refreshed, lines


def _status_all(console: Console, store: ProfileStore) -> None:
    from firefly_weave.sdk.credentials import CredentialError

    document = store.load()
    values: list[dict[str, Any]] = []
    rows = [["", "NAME", "SERVER", "SIGN-IN", "WORKSPACE"]]
    for name in sorted(document.profiles, key=str.casefold):
        profile = document.profiles[name]
        state: dict[str, Any] | None
        try:
            state = auth_flow.open_session(profile).status()
        except CredentialError:
            state = None
        value = describe(profile, active=document.active == name, status=state)
        if state is None:
            value["error_code"] = "WV-AUTH-STORE"
        values.append(value)
        rows.append(
            [
                "*" if document.active == name else "",
                name,
                profile.server,
                _sign_in_text(state, profile),
                auth_flow.workspace_label(profile.workspace),
            ]
        )
    text = _table(rows) if values else ["No saved platforms. Connect with: weave auth setup SERVER"]
    console.result(values, text)


def logout(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        store = _store()
        profile = _resolve(store, options)
        revoke = True if options["revoke"] is None else bool(options["revoke"])
        session = auth_flow.open_session(profile)
        result = asyncio.run(session.logout(revoke=revoke))
        profile = store.set_account(profile.name, None)
        outcome = str(result.get("remote_revocation"))
        remote = {
            "confirmed": "the identity provider revoked the sign-in",
            "unconfirmed": "the identity provider could not confirm revocation",
            # Revocation needs a stored refresh token; without one there was nothing to revoke.
            "not_requested": "there was no sign-in to revoke" if revoke else "remote revocation not requested",
        }.get(outcome, outcome)
        value = {
            "profile": profile.name,
            "server": profile.server,
            "logged_out": True,
            "remote_revocation": outcome,
            "account": None,
        }
        console.result(value, [f"Signed out of '{profile.name}' ({profile.server}); {remote}."])

    _command(options, body)


# --- profiles / use / remove / workspace -------------------------------------------------------------------------


def profiles(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        document = _store().load()
        values = [
            describe(document.profiles[name], active=document.active == name)
            for name in sorted(document.profiles, key=str.casefold)
        ]
        if not values:
            console.result([], ["No saved platforms. Connect with: weave auth setup SERVER"])
            return
        rows = [["", "NAME", "SERVER", "WORKSPACE", "ACCOUNT"]]
        for value in values:
            profile = document.profiles[value["profile"]]
            rows.append(
                [
                    "*" if value["active"] else "",
                    profile.name,
                    profile.server,
                    auth_flow.workspace_label(profile.workspace),
                    auth_flow.account_label(profile.account) if profile.account else "-",
                ]
            )
        console.result(values, [*_table(rows), "", "* active platform. Switch with: weave auth use NAME"])

    _command(options, body)


def use(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        profile = _store().activate(options["name"])
        console.result(
            {"profile": profile.name, "server": profile.server, "active": True},
            [f"Now using '{profile.name}' ({profile.server})."],
        )

    _command(options, body, profile=options["name"])


def remove(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        store = _store()
        profile = store.get(options["name"])
        keep = bool(options["keep_credentials"])
        if not options["yes"]:
            question = f"Remove the saved platform '{profile.name}' ({profile.server})" + (
                "?" if keep else " and sign out of it?"
            )
            if not console.confirm(question, flag="--yes", default=False):
                raise AuthCommandError(
                    "WV-AUTH-CANCELLED", "Cancelled.", EXIT_AUTH, details={"profile": profile.name, "removed": False}
                )
        outcome = "not_requested"
        if not keep:
            from firefly_weave.sdk.credentials import CredentialError

            # Credentials first: when they cannot be removed, the profile stays so nothing is orphaned.
            try:
                result = asyncio.run(auth_flow.open_session(profile).logout(revoke=True))
            except CredentialError:
                raise AuthCommandError(
                    "WV-AUTH-STORE",
                    "The saved credentials could not be removed, so the platform was kept.",
                    2,
                    details={"profile": profile.name, "removed": False},
                    lines=["Remove it anyway (keeping any stored credentials) with --keep-credentials."],
                ) from None
            outcome = str(result.get("remote_revocation"))
        store.remove(profile.name)
        active = store.load().active
        value = {
            "profile": profile.name,
            "removed": True,
            "credentials": "kept" if keep else "removed",
            "remote_revocation": outcome,
            "active": active,
        }
        kept = " Its credentials were kept." if keep else " You are signed out of it."
        text = [f"Removed '{profile.name}'.{kept}"]
        if active is None:
            text.append("No platform is active now. Choose one with: weave auth use NAME")
        console.result(value, text)

    _command(options, body, profile=options["name"])


def workspace(options: dict[str, Any]) -> None:
    def body(console: Console) -> None:
        store = _store()
        profile = _resolve(store, options)
        flags = auth_flow.workspace_flags(options["tenant"], options["project"], options["environment"])
        if options["clear"]:
            if flags:
                raise input_required("--clear", "Use either --clear or --tenant/--project/--environment.")
            profile = store.set_workspace(profile.name, None)
        else:
            if not flags and not console.interactive:
                raise input_required("--tenant/--project/--environment")
            session = auth_flow.open_session(profile)
            identity = auth_flow.read_workspaces(profile, session)
            if not identity.workspaces and not flags:
                raise AuthCommandError(
                    "WV-AUTH-NO-ACCESS",
                    "Your account has no access to any workspace on this platform yet.",
                    EXIT_AUTH,
                    details={"profile": profile.name, "account": auth_flow.account_json(profile.account)},
                    lines=auth_flow.account_lines(profile.account, intro="Ask an administrator for access, sharing:"),
                )
            selection = auth_flow.select_workspace(
                console,
                identity,
                tenant=options["tenant"],
                project=options["project"],
                environment=options["environment"],
                current=profile.workspace,
            )
            profile = store.set_workspace(profile.name, selection)
        value = {
            "profile": profile.name,
            "server": profile.server,
            "workspace": auth_flow.workspace_json(profile.workspace),
        }
        label = auth_flow.workspace_label(profile.workspace)
        if profile.workspace is not None:
            console.result(value, [f"Workspace for '{profile.name}': {label}."])
        else:
            console.result(value, [f"Cleared the workspace of '{profile.name}'."])

    _command(options, body)
