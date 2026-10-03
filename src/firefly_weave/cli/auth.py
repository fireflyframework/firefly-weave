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

"""`weave auth`: connect to platforms, sign in and choose workspaces; never imports server settings or a database.

Saved platforms (profiles) are the default. Any `--auth-config FILE`
invocation of `login`, `status` or `logout` keeps the alpha6 contract: JSON
output, the same options and the same exit codes.
"""

from pathlib import Path
from typing import Any

import click

from firefly_weave.cli.remote import machine_result

PROFILE_HELP = "Saved platform to use (default: the active one)."
OUTPUT_HELP = "text (default) or json: one JSON document on stdout; guidance and prompts go to stderr."


def session_from_options(options: dict[str, Any]) -> Any:
    from firefly_weave.sdk.auth import LoginConfig, OAuthSession
    from firefly_weave.sdk.credentials import FileCredentialStore, NativeCredentialStore

    path = options["auth_config"]
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Configuration exceeds bound")
    config = LoginConfig.model_validate_json(raw)
    if options.get("credential_store", "native") == "file":
        selected = options.get("credential_file")
        if selected is None:
            raise ValueError("File fallback must be explicit")
        store: Any = FileCredentialStore(selected)
    else:
        if options.get("credential_file") is not None:
            raise ValueError("File fallback must be explicit")
        store = NativeCredentialStore()
    return OAuthSession(config, store)


class _Ordered(click.Group):
    """List commands in the order a person uses them (setup first), not alphabetically."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)


@click.group(cls=_Ordered, invoke_without_command=True)
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Connect to a platform, sign in, and choose a workspace.

    Start with 'weave auth setup SERVER'. Saved platforms hold no passwords or
    tokens: credentials stay in the system credential store (or an explicit
    private file). Commands use --profile NAME, WEAVE_PROFILE, or the active
    platform. With --auth-config FILE, login, status and logout keep their
    connection-file behavior and JSON output.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _legacy(ctx: click.Context, name: str, options: dict[str, Any]) -> None:
    """The alpha6 `--auth-config` contract: JSON only, same keys (additions only) and exit codes."""
    import asyncio

    from firefly_weave.sdk.auth import AuthError
    from firefly_weave.sdk.credentials import CredentialError

    if options.get("output") == "text":
        raise click.UsageError("--auth-config produces JSON output only.")
    for option in ("profile_name", "all", "check"):
        if options.get(option) and ctx.get_parameter_source(option) is click.core.ParameterSource.COMMANDLINE:
            raise click.UsageError("--auth-config cannot be combined with saved-platform options.")
    options = {**options, "credential_store": options.get("credential_store") or "native"}
    try:
        session = session_from_options(options)
        if name == "login":
            flow = options["flow"]
            prompt = None
            if options.get("switch_account"):
                if flow == "device":
                    raise click.UsageError("--switch-account needs the browser flow.")
                flow, prompt = "pkce", "login"
            from firefly_weave.cli import auth_flow

            opens = not options.get("no_browser")

            def browser(url: str) -> Any:
                # alpha6 opened the system browser silently; --no-browser prints the address instead.
                if not opens:
                    click.echo(f"Open {url}", err=True)
                return asyncio.to_thread(auth_flow.launch_browser, url, open_browser=opens)

            result = asyncio.run(
                session.login(
                    flow=flow,
                    instructions=lambda uri, code: click.echo(f"Open {uri} and enter {code}", err=True),
                    browser=browser,
                    prompt=prompt,
                )
            )
        elif name == "logout":
            result = asyncio.run(session.logout(revoke=bool(options["revoke"])))
        else:
            result = session.status()
        machine_result(result)
        if name == "status" and not result["authenticated"]:
            raise click.exceptions.Exit(1)
    except AuthError as error:
        machine_result({"code": error.code, "authenticated": False})
        raise click.exceptions.Exit(error.exit_code) from None
    except (OSError, ValueError, ImportError, CredentialError):
        machine_result({"code": "WV-AUTH-CONFIG", "authenticated": False})
        raise click.exceptions.Exit(2) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        machine_result({"code": "WV-AUTH-CANCELLED", "authenticated": False})
        raise click.exceptions.Exit(1) from None


def _profile_mode_only(options: dict[str, Any]) -> None:
    for option in ("credential_store", "credential_file"):
        if options.get(option) is not None:
            raise click.UsageError("--credential-store and --credential-file apply with --auth-config only.")


def _connection_file_options(command: Any) -> Any:
    command = click.option("--credential-file", type=click.Path(path_type=Path), help="With --auth-config.")(command)
    command = click.option(
        "--credential-store", type=click.Choice(["native", "file"]), default=None, help="With --auth-config."
    )(command)
    command = click.option(
        "--auth-config",
        type=click.Path(exists=True, path_type=Path),
        help="Use a connection file instead of a saved platform (JSON output only).",
    )(command)
    return command


@auth.command("setup")
@click.argument("server", required=False)
@click.option("--name", help="Name for the saved platform (default: suggested from the server).")
@click.option("--provider", help="Sign-in option to use when the server offers several (provider id).")
@click.option(
    "--auth-config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Import an operator connection file instead of asking the server.",
)
@click.option(
    "--flow",
    type=click.Choice(["auto", "browser", "device"]),
    default="auto",
    show_default=True,
    help="auto: the browser when one can open here, otherwise a code on another device.",
)
@click.option("--no-browser", is_flag=True, help="Print the sign-in address instead of opening a browser.")
@click.option("--no-login", is_flag=True, help="Save the platform without signing in.")
@click.option("--tenant", type=click.UUID, help="Workspace tenant id (with --project and --environment).")
@click.option("--project", type=click.UUID, help="Workspace project id.")
@click.option("--environment", type=click.UUID, help="Workspace environment id.")
@click.option("--skip-workspace", is_flag=True, help="Do not choose a workspace now.")
@click.option("--yes", is_flag=True, help="Confirm the identity provider without asking.")
@click.option("--replace", is_flag=True, help="Replace a saved platform with the same name.")
@click.option(
    "--credential-store",
    type=click.Choice(["native", "file"]),
    default="native",
    show_default=True,
    help="Where credentials are kept: the system credential store or an explicit private file.",
)
@click.option("--credential-file", type=click.Path(path_type=Path), help="Credential file for --credential-store file.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", show_default=True, help=OUTPUT_HELP)
def setup(**options: Any) -> None:
    """Connect to a platform: server, review, sign in, workspace.

    Asks only in an interactive terminal; every answer also has a flag, so it
    runs unattended. Example: weave auth setup weave.example.com
    """
    from firefly_weave.cli.auth_setup import run_setup

    run_setup(options)


@auth.command("login")
@click.option("--profile", "profile_name", envvar="WEAVE_PROFILE", help=PROFILE_HELP)
@click.option(
    "--flow",
    type=click.Choice(["auto", "browser", "device", "pkce"]),
    default="auto",
    show_default=True,
    help="auto: the browser when one can open here, otherwise a code (with --auth-config: device when offered).",
)
@click.option("--switch-account", is_flag=True, help="Sign in with another account (the provider asks again).")
@click.option("--no-browser", is_flag=True, help="Print the sign-in address instead of opening a browser.")
@_connection_file_options
@click.option("--output", type=click.Choice(["text", "json"]), default=None, help=OUTPUT_HELP)
@click.pass_context
def login(ctx: click.Context, /, **options: Any) -> None:
    """Sign in to the selected platform."""
    if options["auth_config"] is not None:
        _legacy(ctx, "login", options)
        return
    _profile_mode_only(options)
    from firefly_weave.cli.auth_profiles import login as run

    run(options)


@auth.command("status")
@click.option("--profile", "profile_name", envvar="WEAVE_PROFILE", help=PROFILE_HELP)
@click.option("--all", "all", is_flag=True, help="Show every saved platform (reads the credential store for each).")
@click.option("--check", is_flag=True, help="Verify the sign-in and workspaces with the platform.")
@_connection_file_options
@click.option("--output", type=click.Choice(["text", "json"]), default=None, help=OUTPUT_HELP)
@click.pass_context
def status(ctx: click.Context, /, **options: Any) -> None:
    """Show the sign-in state; exits 1 when not signed in."""
    if options["auth_config"] is not None:
        _legacy(ctx, "status", options)
        return
    _profile_mode_only(options)
    from firefly_weave.cli.auth_profiles import status as run

    run(options)


@auth.command("logout")
@click.option("--profile", "profile_name", envvar="WEAVE_PROFILE", help=PROFILE_HELP)
@click.option(
    "--revoke/--no-revoke",
    default=None,
    help="Ask the identity provider to revoke the sign-in (default for saved platforms; with --auth-config, "
    "only when --revoke is given).",
)
@_connection_file_options
@click.option("--output", type=click.Choice(["text", "json"]), default=None, help=OUTPUT_HELP)
@click.pass_context
def logout(ctx: click.Context, /, **options: Any) -> None:
    """Sign out and remove the local credentials."""
    if options["auth_config"] is not None:
        _legacy(ctx, "logout", options)
        return
    _profile_mode_only(options)
    from firefly_weave.cli.auth_profiles import logout as run

    run(options)


@auth.command("profiles")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", show_default=True, help=OUTPUT_HELP)
def profiles(**options: Any) -> None:
    """List saved platforms ('*' marks the active one); never reads credentials."""
    from firefly_weave.cli.auth_profiles import profiles as run

    run(options)


@auth.command("use")
@click.argument("name")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", show_default=True, help=OUTPUT_HELP)
def use(**options: Any) -> None:
    """Make a saved platform the active one."""
    from firefly_weave.cli.auth_profiles import use as run

    run(options)


@auth.command("remove")
@click.argument("name")
@click.option("--keep-credentials", is_flag=True, help="Keep the stored credentials (default: sign out first).")
@click.option("--yes", is_flag=True, help="Remove without asking.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", show_default=True, help=OUTPUT_HELP)
def remove(**options: Any) -> None:
    """Forget a saved platform and, by default, sign out of it."""
    from firefly_weave.cli.auth_profiles import remove as run

    run(options)


@auth.command("workspace")
@click.option("--profile", "profile_name", envvar="WEAVE_PROFILE", help=PROFILE_HELP)
@click.option("--tenant", type=click.UUID, help="Tenant id (with --project and --environment).")
@click.option("--project", type=click.UUID, help="Project id.")
@click.option("--environment", type=click.UUID, help="Environment id.")
@click.option("--clear", is_flag=True, help="Forget the selected workspace.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text", show_default=True, help=OUTPUT_HELP)
def workspace(**options: Any) -> None:
    """Choose the tenant, project and environment remote commands use."""
    from firefly_weave.cli.auth_profiles import workspace as run

    run(options)
