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

"""Small, explicit steps to operate an owned local development platform."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from firefly_weave.cli import EXIT_USAGE, emit_result, error_result
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.sdk import platform as lifecycle


def _call(operation: Callable[[], Any], output: str, render: Callable[[dict[str, Any]], None] | None = None) -> Any:
    try:
        value = operation()
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, lifecycle.PlatformError)
            else (
                "Platform operation failed. Check the checkout, Docker context, private directory, and retained logs."
            )
        )
        emit_result(error_result("WV-CLI-PLATFORM", message), output)
        raise SystemExit(EXIT_USAGE) from None
    if value is None:
        return None
    if output == "json":
        click.echo(canonical_bytes(value).decode())
    elif render is not None:
        render(value)
    elif "receipt" in value:
        receipt = value["receipt"]
        click.echo(f"Workflow: {receipt['status']}")
        click.echo("Output: " + json.dumps(receipt["output"], sort_keys=True))
        click.echo(f"Run: {receipt['run_id']}")
        if value.get("existing"):
            click.echo("Reused the saved result; no new workflow run was created.")
    else:
        for key, item in value.items():
            if key not in {"source_sha256", "engine", "endpoint", "ok"}:
                click.echo(f"{key.replace('_', ' ').capitalize()}: {item}")
    return value


@click.group()
@click.option(
    "--directory",
    type=click.Path(path_type=Path),
    default=Path(".local/platform"),
    show_default=True,
    help="Private installation directory; reuse this exact path for every command.",
)
@click.pass_context
def platform(ctx: click.Context, directory: Path) -> None:
    """Set up and run a local API with PostgreSQL and Keycloak.

    Requires a matching source checkout, uv, and local Docker with Compose.
    Start with setup; then start holds the API in the foreground. In another
    terminal, run demo to save your first real workflow execution, then user
    to create a development person who can sign in to Studio and the CLI.
    To run built-in HTTP connector actions, use integrations and secret.
    """
    ctx.obj = directory.absolute()


@platform.command()
@click.option("--source", type=click.Path(path_type=Path), default=Path("."), show_default=True)
@click.option("--context", help="Named local Docker context; default: current context.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def doctor(source: Path, context: str | None, output: str) -> None:
    """Check prerequisites without creating files or changing services."""
    _call(lambda: lifecycle.doctor(source, context), output)


@platform.command()
@click.option("--source", type=click.Path(path_type=Path), default=Path("."), show_default=True)
@click.option("--context", help="Named local Docker context; default: current context.")
@click.option("--subnet", help="Optional unused RFC1918 IPv4 Docker subnet, for exhausted default pools.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def setup(directory: Path, source: Path, context: str | None, subnet: str | None, output: str) -> None:
    """Create an isolated runtime, owned dependencies, and verified local identity.

    Creates fresh databases and private credentials once. Downloads locked
    dependencies and container images as needed. Existing installations are
    never overwritten. Data remains retained after stop.
    """
    from firefly_weave.cli.progress import progress

    def operation() -> dict[str, Any]:
        with progress("Checking local platform prerequisites", enabled=output == "text") as update:
            return lifecycle.setup(directory, source, context, subnet=subnet, progress=update)

    _call(operation, output)
    if output == "text":
        prefix = "weave platform --directory " + shlex.quote(str(directory))
        click.echo("Next, keep the API running in this terminal:")
        click.echo("  " + prefix + " start")
        click.echo("In another terminal, run your first workflow:")
        click.echo("  " + prefix + " demo")
        click.echo("Then create a person who can sign in to Studio and the CLI:")
        click.echo("  " + prefix + " user --username YOUR_NAME")


@platform.command()
@click.pass_obj
def start(directory: Path) -> None:
    """Resume dependencies and run the API here; Ctrl-C stops only the API.

    Reuses the existing runtime and identity. Never migrates or provisions again.
    """
    click.echo("Starting the local API. Keep this terminal open; Ctrl-C stops the API.")
    _call(lambda: lifecycle.start(directory, click.echo), "text")


@platform.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def status(directory: Path, output: str) -> None:
    """Show saved setup stage, API readiness, identity readiness, and URLs."""
    _call(lambda: lifecycle.status(directory), output)


@platform.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def stop(directory: Path, output: str) -> None:
    """Stop only this installation's dependencies after Ctrl-C stops its API.

    Keeps containers, volumes, databases, credentials, and workflow data.
    """
    _call(lambda: lifecycle.stop(directory), output)


@platform.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def demo(directory: Path, output: str) -> None:
    """Create a scoped example and save one real successful workflow run.

    Requires a ready API in another terminal. Repeating this command reads the
    saved receipt; it does not recreate the tenant, grants, workflow, or run.
    """
    _call(lambda: lifecycle.demo(directory), output)
    if output == "text":
        click.echo(f"Saved receipt: {directory / 'first-run.json'}")
        click.echo("Next, create a person who can sign in to this workspace:")
        click.echo("  weave platform --directory " + shlex.quote(str(directory)) + " user --username YOUR_NAME")
        click.echo("To run built-in HTTP connector actions in this workspace:")
        click.echo("  weave platform --directory " + shlex.quote(str(directory)) + " integrations enable")


@platform.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def token(directory: Path, output: str) -> None:
    """Refresh the verified local host token in its private file.

    Prints only the path. The token can be used with the local API and /docs.
    """
    _call(lambda: lifecycle.token(directory), output)


def _show_person(value: dict[str, Any]) -> None:
    def level(scope: dict[str, str]) -> str:
        return "environment" if "environment_id" in scope else "project" if "project_id" in scope else "tenant"

    click.echo("Created a local sign-in account and linked it to a new person in the demo workspace.")
    click.echo(f"Username: {value['username']}")
    click.echo(f"Password: {value['password']}")
    click.echo(value["warning"] + " Copy it now; it cannot be shown again.")
    click.echo("Roles: " + ", ".join(f"{grant['role']} (demo {level(grant['scope'])})" for grant in value["grants"]))
    click.echo("Next, connect to this platform and sign in with that account:")
    click.echo("  " + value["next"][0])
    click.echo("Then open Studio:")
    click.echo("  " + value["next"][1])


@platform.command()
@click.option(
    "--username",
    required=True,
    help="New sign-in name: 3 to 64 lowercase letters and digits, optionally joined by '.', '_', or '-'.",
)
@click.option(
    "--role",
    "roles",
    multiple=True,
    type=click.Choice(lifecycle.PERSON_ROLES),
    metavar="ROLE",
    help="Grant this role in the demo workspace instead of the defaults; repeat for several roles. "
    "One of: " + ", ".join(lifecycle.PERSON_ROLES) + ".",
)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def user(directory: Path, username: str, roles: tuple[str, ...], output: str) -> None:
    """Create a person who can sign in to this local platform (development only).

    Creates an account in the owned Keycloak with a generated password, a
    linked person, and grants in the demo workspace. Requires completed setup,
    the demo workspace, and a running API. The password is printed once and
    never saved; an existing username is never reset.

    Default roles are the smallest set to author, publish, activate, run,
    inspect runs, and work on human tasks: developer on the demo project, and
    deployer, operator, viewer, and task_participant on the demo environment.
    """
    _call(lambda: lifecycle.user(directory, username, roles), output, _show_person)


@platform.group()
def integrations() -> None:
    """Run built-in HTTP connector actions on this local platform (development only).

    enable publishes the installed weave-http@2.0.0 Connector, registers this
    runtime's local build as its release, and grants a dedicated native
    principal; restart the API to apply it. Only the declarative HTTP executor
    runs locally, with the usual egress rules: HTTPS origins listed on each
    connection, no private networks.
    """


def _show_integrations(value: dict[str, Any]) -> None:
    click.echo(f"Connector: {value['connector']} (version ID {value['connector_version_id']})")
    click.echo(f"Local release: {value['release_id']}")
    click.echo("Activations of workflows using these actions pin connector_release_ids:")
    click.echo("  " + json.dumps(value["connector_release_ids"]))
    if value["restart_required"]:
        click.echo("Restart the API to run connector actions: press Ctrl-C in its terminal, then run:")
    else:
        click.echo("Already enabled. If the API started before enabling, restart it with:")
    click.echo("  " + value["next"][0])
    click.echo("Store a secret value for a connection handle (prompted, never echoed):")
    click.echo("  " + value["next"][1])
    click.echo("Connections that use secret handles also need a grant for the local release:")
    click.echo("  " + value["next"][2])


@integrations.command("enable")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def integrations_enable(directory: Path, output: str) -> None:
    """Enable built-in HTTP connector actions in the demo environment.

    Requires the demo workspace and a running API. Repeating it reuses what
    already exists and creates nothing new.
    """
    _call(lambda: lifecycle.integrations_enable(directory), output, _show_integrations)


@integrations.command("disable")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def integrations_disable(directory: Path, output: str) -> None:
    """Stop running connector actions locally after the next restart.

    Works without a running API. Server records are kept; enable reuses them.
    """
    _call(lambda: lifecycle.integrations_disable(directory), output, lambda value: click.echo(value["message"]))


def _show_grant(value: dict[str, Any]) -> None:
    click.echo(f"Granted the local release access to connection revision {value['connection_revision_id']}:")
    for capability in value["capabilities"]:
        click.echo("  " + capability)


@integrations.command("grant")
@click.option("--connection", required=True, help="Connection revision ID returned by connection creation.")
@click.option(
    "--access",
    multiple=True,
    required=True,
    type=click.Choice(["read", "write"]),
    help="Allow read actions, write actions, or both (repeat the option). Grant write only when needed.",
)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def integrations_grant(directory: Path, connection: str, access: tuple[str, ...], output: str) -> None:
    """Let the local release use one connection's secret handles.

    Needed only for connections with secretRef handles. Connections without
    secrets need no grant. Requires enabled integrations and a running API.
    """
    _call(lambda: lifecycle.integrations_grant(directory, connection, access), output, _show_grant)


@platform.group()
def secret() -> None:
    """Store development secret values for connection handles (development only).

    Values live in owner-only files under this installation and are granted to
    the demo environment by handle. Connections reference the handle, never the
    value. Values are read from a hidden prompt or standard input, never from
    arguments or environment variables, and are never printed.
    """


def _interactive_stdin() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _show_secret(value: dict[str, Any]) -> None:
    click.echo(f"Handle: {value['handle']}")
    click.echo(value["message"])


@secret.command("set")
@click.option("--handle", required=True, help="Handle name that connections reference in secretRef.")
@click.option(
    "--value-stdin",
    is_flag=True,
    help="Read the value from piped standard input instead of a hidden prompt; one trailing newline is "
    "removed. A terminal is refused because it would show the value.",
)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def secret_set(directory: Path, handle: str, value_stdin: bool, output: str) -> None:
    """Create or replace the value behind a handle in the demo environment.

    A new handle applies after the API restarts; a replaced value applies at
    the next credential use.
    """

    def operation() -> dict[str, Any]:
        lifecycle.secret_preflight(directory, handle)
        if value_stdin:
            if _interactive_stdin():
                # A terminal would echo the typed value; only the hidden prompt keeps it off screen.
                raise lifecycle.PlatformError(
                    "Standard input is a terminal and would show the value. Omit --value-stdin to use the "
                    "hidden prompt, or pipe the value; nothing was stored."
                )
            stream = getattr(sys.stdin, "buffer", None)
            if stream is None:
                raise lifecycle.PlatformError("Standard input is unavailable; nothing was stored.")
            raw = stream.read(lifecycle.SECRET_VALUE_LIMIT + 3)
        else:
            try:
                raw = click.prompt("Secret value", hide_input=True, confirmation_prompt=True, err=True).encode()
            except click.Abort:
                raise lifecycle.PlatformError("No secret value was entered; nothing was stored.") from None
        return lifecycle.secret_set(directory, handle, raw)

    _call(operation, output, _show_secret)


def _show_handles(value: dict[str, Any]) -> None:
    if not value["handles"]:
        click.echo("No local secret handles are stored.")
    for handle in value["handles"]:
        click.echo(handle)


@secret.command("list")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def secret_list(directory: Path, output: str) -> None:
    """List stored handle names; values are never shown."""
    _call(lambda: lifecycle.secret_list(directory), output, _show_handles)


@secret.command("remove")
@click.option("--handle", required=True)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def secret_remove(directory: Path, handle: str, output: str) -> None:
    """Delete the value behind a handle; a restart withdraws the handle."""
    _call(lambda: lifecycle.secret_remove(directory, handle), output, _show_secret)
