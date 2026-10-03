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

"""Click entry point for authoring, remote operations, and deployment."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import click

from firefly_weave import __version__
from firefly_weave.cli import EXIT_USAGE, emit_result, error_result
from firefly_weave.cli.admin import admin
from firefly_weave.cli.auth import auth
from firefly_weave.cli.connections import connections
from firefly_weave.cli.connectors import connector
from firefly_weave.cli.debug import simulate
from firefly_weave.cli.definitions import definitions
from firefly_weave.cli.deploy import worker
from firefly_weave.cli.email import email
from firefly_weave.cli.files import files
from firefly_weave.cli.human_tasks import human_assignments, human_groups, human_tasks
from firefly_weave.cli.onboarding import DOCS_URL, docs, init
from firefly_weave.cli.operations import incident, run
from firefly_weave.cli.platform import platform
from firefly_weave.cli.remote import family, machine_result, remote
from firefly_weave.cli.runs import runs
from firefly_weave.cli.schedule import schedule
from firefly_weave.cli.schema import schema
from firefly_weave.cli.studio import StudioLaunchError, studio
from firefly_weave.cli.triggers import triggers
from firefly_weave.cli.workers import workers
from firefly_weave.cli.workflow import workflow

# Sampled from assets/weave-logo.svg, including its diagonal underpass mask.
# Regenerate with: python scripts/render_cli_logo.py
_LOGO = """  ###    ###        ###    ###
 #####   ####      ####   #####
  #####  #####    #####  #####
   #####  #####  #####  #####
    #####  #### #####  #####
     #####  ## #####  #####
      #####   #####  #####
       ##### #####  #####
        ######### ######
         ####### ######
          #####  #####
           ###    ###
            #      #"""


LEGACY_AUTH_COMMANDS = frozenset({"login", "status", "logout"})


def _human_auth(arguments: Sequence[str], machine: bool) -> bool:
    """A text-mode `weave auth` invocation; the alpha6 `--auth-config` login/status/logout stay JSON-only."""
    if machine or arguments[:1] != ["auth"]:
        return False
    connection_file = any(value == "--auth-config" or value.startswith("--auth-config=") for value in arguments)
    return not (connection_file and arguments[1:2] and arguments[1] in LEGACY_AUTH_COMMANDS)


class OfflineGroup(click.Group):
    """Present human help while preserving value-free machine error contracts."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if formatter.width >= 60:
            for index, line in enumerate(_LOGO.splitlines()):
                title = "   Firefly Weave" if index == 1 else ""
                formatter.write(f"{line:<32}{title}".rstrip() + "\n")
        else:
            formatter.write("Firefly Weave\n")
        formatter.write("\n")
        super().format_help(ctx, formatter)
        with formatter.section("Quick start"):
            formatter.write_dl(
                [
                    (f"{ctx.command_path} init hello-weave", "Create an offline starter; see its README."),
                    (f"{ctx.command_path} workflow validate workflow.yaml", "Check a local definition."),
                    (f"{ctx.command_path} workflow compile --help", "Compile with a pinned catalog."),
                    (f"{ctx.command_path} platform setup", "Set up a local platform from a matching checkout."),
                    (f"{ctx.command_path} docs platform", "Learn the platform services and deployment choices."),
                    (f"{ctx.command_path} auth setup", "Connect to a platform, sign in, and choose a workspace."),
                    (f"{ctx.command_path} definitions --help", "Publish and activate definitions."),
                    (f"{ctx.command_path} worker deploy --help", "Deploy a worker to an existing platform."),
                    (f"{ctx.command_path} help COMMAND", "Explore any command without running it."),
                ]
            )
        formatter.write_paragraph()
        formatter.write_text(f"Documentation: {DOCS_URL}")

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sections = (
            ("Start here", ("init", "platform", "studio", "docs")),
            ("Author locally", ("workflow", "schema", "connector")),
            ("Human work and email", ("human-tasks", "human-assignments", "human-groups", "email")),
            (
                "Connect to a platform",
                ("auth", "definitions", "connections", "run", "runs", "incident", "schedule", "workers"),
            ),
            (
                "Integrations and delivery",
                (
                    "triggers",
                    "broker-triggers",
                    "source-bindings",
                    "subscriptions",
                    "deliveries",
                    "teams-references",
                    "whatsapp-statuses",
                    "provider-sources",
                    "provider-receipts",
                ),
            ),
            ("Deploy and administer", ("worker", "admin", "retention", "compatibility", "remote")),
            ("Help and version", ("help", "version")),
        )
        summaries = {
            "human-tasks": "Find, claim, and complete assigned human work.",
            "human-assignments": "Bind workflow assignments to people or groups.",
            "human-groups": "Manage the people eligible for task assignments.",
            "email": "Read conversations, manage incoming mail, and send replies.",
            "studio": "Open the visual workflow editor and task inbox.",
            "init": "Create a safe offline workflow starter.",
            "platform": "Set up, start, and use a local development platform.",
            "docs": "Find docs for platform startup and next steps.",
            "workflow": "Validate, compile, and simulate local workflows.",
            "schema": "Inspect and export definition schemas.",
            "connector": "Import OpenAPI, build no-code HTTP actions, or package connectors.",
            "auth": "Connect to a platform, sign in, and choose a workspace.",
            "definitions": "Publish and activate workflows and actions.",
            "connections": "Configure and test integration connections.",
            "files": "Upload, download and inspect workflow files.",
            "run": "Cancel or retry runs; replay exported history.",
            "runs": "Start and inspect workflow runs.",
            "incident": "Inspect and resolve workflow incidents.",
            "schedule": "Manage scheduled runs and view occurrences.",
            "workers": "Register workers and manage their releases.",
            "triggers": "Configure webhook triggers and schedules.",
            "broker-triggers": "Start workflows from broker messages.",
            "source-bindings": "Inspect or revoke connection source bindings.",
            "subscriptions": "Subscribe external systems to workflow events.",
            "deliveries": "Inspect and retry outgoing event deliveries.",
            "teams-references": "Manage saved Teams conversation references.",
            "whatsapp-statuses": "Inspect WhatsApp message delivery status.",
            "provider-sources": "Configure incoming messaging platform events.",
            "provider-receipts": "Inspect and retry received platform events.",
            "worker": "Package and deploy workers to an existing platform.",
            "admin": "Run database migrations and local administration.",
            "retention": "Preview and apply data retention plans.",
            "compatibility": "Check stored definitions for compatibility.",
            "remote": "Compile through the API and inspect its catalog.",
            "help": "Read help for any command or subcommand.",
            "version": "Show installed and workflow language versions.",
        }
        commands = {name: self.get_command(ctx, name) for name in self.list_commands(ctx)}
        for heading, names in (*sections, ("Other commands", tuple(commands))):
            rows = []
            for name in names:
                command = commands.pop(name, None)
                if command is not None and not command.hidden:
                    rows.append((name, summaries.get(name, command.get_short_help_str())))
            if rows:
                with formatter.section(heading):
                    formatter.write_dl(rows)

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(args) if args is not None else sys.argv[1:]
        try:
            result = super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.ClickException as error:
            if isinstance(error, StudioLaunchError):
                error.show()
                if standalone_mode:
                    raise SystemExit(error.exit_code) from None
                raise
            # Click errors may echo arbitrary option values; keep machine/local errors value-free.
            machine = "--output=json" in arguments or any(
                arguments[i : i + 2] == ["--output", "json"] for i in range(len(arguments))
            )
            if _human_auth(arguments, machine):
                # Profile-mode `weave auth` takes names, server addresses and ids, never secrets, so
                # click's own message (with usage and the --help hint) is safe and more helpful.
                error.show()
            elif arguments and arguments[0] in {
                "remote",
                "auth",
                "definitions",
                "connections",
                "runs",
                "workers",
                "triggers",
                "broker-triggers",
                "source-bindings",
            }:
                machine_result(
                    {
                        "code": "WV-CLI-USAGE",
                        "message": "Invalid invocation; use --help for supported arguments",
                        "status": 422,
                        "diagnostics": [],
                    }
                )
            else:
                emit_result(
                    error_result("WV-CLI-USAGE", "Invalid invocation; use --help for supported arguments."),
                    "json" if machine else "text",
                )
            result = EXIT_USAGE
        if standalone_mode:
            raise SystemExit(result or 0)
        return result


@click.group(cls=OfflineGroup, invoke_without_command=True)
@click.version_option(__version__, prog_name="Firefly Weave", message="%(prog)s %(version)s")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Firefly Weave: workflow orchestration and integration tools.

    Author and validate locally; connect to a running platform for execution.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("help")
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_command(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for a command, such as help workflow compile."""
    target = ctx.find_root()
    for name in command_path:
        if not isinstance(target.command, click.Group):
            raise click.UsageError("Invalid help command path.")
        command = target.command.get_command(target, name)
        if command is None or command.hidden:
            raise click.UsageError("Invalid help command path.")
        # Construct help contexts directly: never invoke commands or parse credentials.
        target = click.Context(command, info_name=name, parent=target)
    click.echo(target.get_help())


@cli.command()
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def version(output: str) -> None:
    """Print installed compiler and language compatibility."""
    if output == "json":
        from firefly_weave.compiler.canonical import canonical_bytes

        click.echo(
            canonical_bytes(
                {"version": __version__, "apiVersion": "weave/v1alpha1", "irVersion": "weave/ir-v1alpha1"}
            ).decode()
        )
    else:
        click.echo(f"Firefly Weave {__version__} (weave/v1alpha1; weave/ir-v1alpha1)")


cli.add_command(init)
cli.add_command(docs)
cli.add_command(platform)
cli.add_command(studio)
cli.add_command(human_tasks)
cli.add_command(human_assignments)
cli.add_command(human_groups)
cli.add_command(email)
cli.add_command(files)
workflow.add_command(simulate)
cli.add_command(workflow)
cli.add_command(schema)
cli.add_command(connector)
cli.add_command(worker)
cli.add_command(admin)
cli.add_command(auth)
cli.add_command(run)
cli.add_command(incident)
cli.add_command(schedule)
for public_group in (
    remote,
    definitions,
    connections,
    runs,
    workers,
    triggers,
    family("retention", "retention"),
    family("compatibility", "compatibility"),
    family("teams-references", "teams_references"),
    family("whatsapp-statuses", "whatsapp_statuses"),
    family("provider-sources", "provider_sources"),
    family("provider-receipts", "provider_receipts"),
    family("broker-triggers", "broker_triggers"),
    family("source-bindings", "source_bindings"),
    family("subscriptions", "subscriptions"),
    family("deliveries", "deliveries"),
):
    cli.add_command(public_group)

if __name__ == "__main__":
    cli()
