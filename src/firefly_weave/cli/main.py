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

"""Click entry point for offline authoring commands."""

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
from firefly_weave.cli.operations import incident, run
from firefly_weave.cli.remote import family, machine_result, remote
from firefly_weave.cli.runs import runs
from firefly_weave.cli.schedule import schedule
from firefly_weave.cli.schema import schema
from firefly_weave.cli.triggers import triggers
from firefly_weave.cli.workers import workers
from firefly_weave.cli.workflow import workflow


class OfflineGroup(click.Group):
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
        except click.ClickException:
            # Click errors may echo arbitrary option values; keep machine/local errors value-free.
            machine = "--output=json" in arguments or any(
                arguments[i : i + 2] == ["--output", "json"] for i in range(len(arguments))
            )
            if arguments and arguments[0] in {
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


@click.group(cls=OfflineGroup)
def cli() -> None:
    """Firefly Weave offline authoring tools."""


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
