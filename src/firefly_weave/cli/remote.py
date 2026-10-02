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

"""Thin command adapters over the shared typed public SDK."""

import json
import os
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel, TypeAdapter


def machine_result(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = json.loads(value.model_dump_json(by_alias=True))
    elif isinstance(value, list):
        value = [json.loads(v.model_dump_json(by_alias=True)) if isinstance(v, BaseModel) else v for v in value]
    click.echo(json.dumps(value, sort_keys=True, ensure_ascii=False))


def command(operation_id: str, name: str | None = None) -> click.Command:
    from firefly_weave.contracts.surface import OPERATIONS

    operation = OPERATIONS[operation_id]

    def execute(**options: Any) -> None:
        import asyncio

        from firefly_weave.contracts.access import Scope
        from firefly_weave.sdk.auth import AuthError
        from firefly_weave.sdk.client import WeaveClient
        from firefly_weave.sdk.credentials import CredentialError
        from firefly_weave.sdk.errors import WeaveError

        async def invoke() -> Any:
            identifier = options.get("identifier")
            if operation_id == "retention.apply":
                selected = options.get("plan_id")
                if selected is not None and identifier is not None and selected != identifier:
                    raise ValueError("Conflicting plan selection")
                identifier = selected or identifier
                if identifier is None:
                    raise ValueError("A plan identifier is required")

            def environment_token() -> str:
                return os.environ.get("WEAVE_ACCESS_TOKEN", "")

            provider: Any = environment_token
            if options.get("auth_config"):
                from firefly_weave.cli.auth import session_from_options

                provider = session_from_options(options)
            scope = Scope(
                tenant_id=options["tenant"],
                project_id=options.get("project"),
                environment_id=options.get("environment"),
            )
            body = None
            if options.get("request_path"):
                path = options["request_path"]
                with path.open("rb") as stream:
                    raw = stream.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError("Request bound exceeded")
                body = TypeAdapter(operation.request).validate_json(raw)
            query = {}
            for key in (
                "cursor",
                "limit",
                "after",
                "message_id",
                "business_key",
                "correlation_key",
                "status",
                "include_archived",
            ):
                if options.get(key) is not None:
                    query[key] = str(options[key]).lower() if isinstance(options[key], bool) else options[key]
            async with WeaveClient(options["base_url"], provider, scope) as sdk:
                if operation_id == "retention.apply":
                    assert identifier is not None
                    plan = await sdk.read_retention_plan(identifier)
                    if plan.id != identifier:
                        raise ValueError("Returned plan does not match selection")
                    # Keep stdout as the final machine result; preview before the mutation.
                    click.echo(plan.model_dump_json(), err=True)
                return await sdk.invoke(
                    operation_id,
                    identifier=identifier,
                    state_id=options.get("state_id"),
                    collection=options.get("collection"),
                    body=body,
                    revision=options.get("revision"),
                    idempotency_key=options.get("idempotency_key"),
                    query=query or None,
                )

        try:
            result = asyncio.run(invoke())
            machine_result(result)
            if operation_id in {"compiler.compile", "compiler.validate"}:
                if not result.validation_ok:
                    for diagnostic in result.diagnostics:
                        click.echo(diagnostic.model_dump_json(by_alias=True), err=True)
                    raise click.exceptions.Exit(1)
            elif operation_id == "runs.replay" and result.status != "consistent":
                raise click.exceptions.Exit(1)
        except WeaveError as error:
            machine_result(error.problem)
            for diagnostic in error.diagnostics:
                click.echo(diagnostic.model_dump_json(by_alias=True), err=True)
            raise click.exceptions.Exit(1 if error.status < 500 else 3) from None
        except AuthError as error:
            machine_result({"code": error.code, "message": "Authentication failed", "status": 401, "diagnostics": []})
            raise click.exceptions.Exit(error.exit_code) from None
        except (ValueError, OSError, ImportError, CredentialError):
            machine_result(
                {
                    "code": "WV-CLI-CONFIG",
                    "message": "Invalid local configuration or request; install the client extra and check inputs",
                    "status": 422,
                    "diagnostics": [],
                }
            )
            raise click.exceptions.Exit(2) from None

    params: list[click.Parameter] = [
        click.Option(["--base-url"], envvar="WEAVE_BASE_URL", required=True),
        click.Option(["--tenant"], type=click.UUID, envvar="WEAVE_TENANT_ID", required=True),
        click.Option(["--project"], type=click.UUID, envvar="WEAVE_PROJECT_ID", required="{project}" in operation.path),
        click.Option(
            ["--environment"],
            type=click.UUID,
            envvar="WEAVE_ENVIRONMENT_ID",
            required="{environment}" in operation.path,
        ),
        click.Option(["--output"], type=click.Choice(["json"]), default="json"),
        click.Option(["--auth-config"], type=click.Path(path_type=Path)),
        click.Option(["--credential-store"], type=click.Choice(["native", "file"]), default="native"),
        click.Option(["--credential-file"], type=click.Path(path_type=Path)),
    ]
    if operation_id.startswith("whatsapp_statuses."):
        params.append(click.Option(["--source", "identifier"], type=click.UUID, required=True))
        if operation_id == "whatsapp_statuses.read":
            params.append(click.Option(["--message-id"], required=True))
        else:
            params.append(click.Option(["--state-id"], type=click.UUID, required=True))
    elif "{identifier}" in operation.path:
        params.append(click.Argument(["identifier"], type=click.UUID, required=operation_id != "retention.apply"))
        if operation_id == "retention.apply":
            params.append(
                click.Option(
                    ["--plan-id"], type=click.UUID, help="Read and display this immutable plan before applying it."
                )
            )
    if "{collection}" in operation.path:
        params.append(
            click.Option(
                ["--collection"],
                type=click.Choice(
                    ["workflows", "actions", "connectors", "drafts"]
                    if operation.method == "GET"
                    else ["workflows", "actions", "connectors"]
                ),
                required=True,
            )
        )
    if operation.request is not None:
        params.append(
            click.Option(["--request", "request_path"], type=click.Path(exists=True, path_type=Path), required=True)
        )
    if operation.revision != "none":
        params.append(
            click.Option(["--revision"], type=click.IntRange(1, 9999999999), required=operation.revision == "required")
        )
    if operation.idempotency:
        params.append(click.Option(["--idempotency-key"], required=True))
    if operation.page or operation_id in {"runs.history", "runs.export", "runs.replay"}:
        params.append(
            click.Option(
                ["--limit"],
                type=click.IntRange(1, 1000 if operation_id in {"runs.export", "runs.replay"} else 100),
                default=1000 if operation_id in {"runs.export", "runs.replay"} else 50,
            )
        )
        if operation.page or operation_id == "runs.history":
            params.append(click.Option(["--cursor"]))
    if operation_id == "runs.list":
        params += [
            click.Option(["--business-key"]),
            click.Option(["--correlation-key"]),
            click.Option(
                ["--status"],
                type=click.Choice(
                    ["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"]
                ),
            ),
            click.Option(["--include-archived"], is_flag=True),
        ]
    return click.Command(
        name or operation_id.split(".")[-1].replace("_", "-"),
        params=params,
        callback=execute,
        help=operation.id + ": " + operation.capability,
    )


def family(name: str, prefix: str, *, include: tuple[str, ...] = ()) -> click.Group:
    from firefly_weave.contracts.surface import OPERATIONS

    group = click.Group(name, help="Typed public " + name + " operations; JSON stdout.")
    for operation_id in OPERATIONS:
        if operation_id.startswith(prefix + ".") or operation_id in include:
            group.add_command(command(operation_id))
    return group


remote = click.Group("remote", help="Authenticated compilation and server contracts.")
for operation_id in ("compiler.compile", "compiler.validate", "catalog.read", "capabilities.read", "schemas.read"):
    remote.add_command(command(operation_id, operation_id.split(".")[0] if operation_id.endswith(".read") else None))


access = click.Group("access", help="Explicitly authorized tenant, project and grant administration.")
for operation_id, name in (
    ("admin.tenant", "create-tenant"),
    ("admin.grant", "grant"),
    ("projects.create", "create-project"),
    ("environments.create", "create-environment"),
    ("environments.read", "environment"),
):
    access.add_command(command(operation_id, name))
access.add_command(family("principals", "principals"))
access.add_command(family("members", "members"))
remote.add_command(access)
