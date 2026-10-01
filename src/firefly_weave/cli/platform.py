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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from firefly_weave.cli import EXIT_USAGE, emit_result, error_result
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.sdk import platform as lifecycle


def _call(operation: Callable[[], Any], output: str) -> Any:
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
    terminal, run demo to save your first real workflow execution.
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


@platform.command()
@click.pass_obj
def start(directory: Path) -> None:
    """Resume dependencies and run the API here; Ctrl-C stops only the API.

    Reuses the existing runtime and identity. Never migrates or provisions again.
    """
    click.echo("Starting the local API. Keep this terminal open; Ctrl-C stops the API.")
    _call(lambda: lifecycle.start(directory), "text")


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


@platform.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_obj
def token(directory: Path, output: str) -> None:
    """Refresh the verified local host token in its private file.

    Prints only the path. The token can be used with the local API and /docs.
    """
    _call(lambda: lifecycle.token(directory), output)
