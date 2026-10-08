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

"""Thin command adapters over the shared typed public SDK.

A command targets the platform given explicitly (`--base-url`/`WEAVE_BASE_URL`
or `--auth-config`, the alpha6 contract) or else a saved platform profile
(`--profile`, `WEAVE_PROFILE` or the active one) with its sign-in and workspace.
"""

import json
import os
import shlex
from pathlib import Path
from typing import Any, NamedTuple

import click
from pydantic import BaseModel, TypeAdapter

NO_TARGET = "No platform is selected. Run 'weave auth setup SERVER' to connect, or pass --base-url and --tenant."


def machine_result(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = json.loads(value.model_dump_json(by_alias=True))
    elif isinstance(value, list):
        value = [json.loads(v.model_dump_json(by_alias=True)) if isinstance(v, BaseModel) else v for v in value]
    click.echo(json.dumps(value, sort_keys=True, ensure_ascii=False))


class Target(NamedTuple):
    """Where a remote command goes: API origin, credential provider, scope and the profile (profile mode)."""

    base_url: str
    provider: Any
    scope: Any
    profile: str | None = None


class TargetProblem(Exception):
    """The command cannot tell which platform or workspace to use; the message says how to fix it."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _explicit_target(operation: Any, options: dict[str, Any]) -> Target:
    """Alpha6 behavior: explicit origin, `WEAVE_ACCESS_TOKEN` or a connection file, flags for the scope."""
    from firefly_weave.contracts.access import Scope

    def environment_token() -> str:
        return os.environ.get("WEAVE_ACCESS_TOKEN", "")

    provider: Any = environment_token
    if options.get("auth_config"):
        from firefly_weave.cli.auth import session_from_options

        provider = session_from_options(options)
    base_url = options.get("base_url") or (provider.config.target if options.get("auth_config") else None)
    if not isinstance(base_url, str):
        raise click.UsageError("Missing option '--base-url'.")
    for flag, key, needed in (
        ("--tenant", "tenant", True),
        ("--project", "project", "{project}" in operation.path),
        ("--environment", "environment", "{environment}" in operation.path),
    ):
        if needed and options.get(key) is None:
            raise click.UsageError(f"Missing option '{flag}'.")
    scope = Scope(
        tenant_id=options["tenant"], project_id=options.get("project"), environment_id=options.get("environment")
    )
    return Target(base_url, provider, scope)


def _profile_target(operation: Any, options: dict[str, Any]) -> Target:
    """Server, sign-in and workspace from a saved profile; flags override the workspace from their level down."""
    from firefly_weave.contracts.access import Scope
    from firefly_weave.sdk.profiles import ProfileError, ProfileStore

    try:
        profile = ProfileStore().resolve(options.get("profile"))
    except ProfileError as error:
        if error.code == "WV-PROFILE-NONE":
            raise TargetProblem(NO_TARGET) from None
        if error.code == "WV-PROFILE-NOT-FOUND":
            raise TargetProblem("No saved platform has that name. List them with 'weave auth profiles'.") from None
        raise TargetProblem(error.message) from None
    saved = profile.workspace
    tenant, project, environment = options.get("tenant"), options.get("project"), options.get("environment")
    if tenant is None and project is None and environment is None and saved is not None:
        tenant, project, environment = saved.tenant_id, saved.project_id, saved.environment_id
    elif tenant is None and saved is not None:
        # A deeper override keeps the saved levels above it, never a mix across tenants or projects.
        tenant = saved.tenant_id
        if project is None:
            project = saved.project_id
    # Name the profile: it may not be the active one that a bare 'weave auth workspace' would change.
    choose = f"weave auth workspace --profile {shlex.quote(profile.name)}"
    if tenant is None:
        raise TargetProblem(f"Platform '{profile.name}' has no workspace selected. Run '{choose}' or pass --tenant.")
    for flag, value, needed in (
        ("--project", project, "{project}" in operation.path),
        ("--environment", environment, "{environment}" in operation.path),
    ):
        if needed and value is None:
            raise TargetProblem(f"This command needs {flag}: choose a workspace with '{choose}' or pass {flag}.")
    from firefly_weave.sdk.sign_in import session_for

    scope = Scope(tenant_id=tenant, project_id=project, environment_id=environment)
    return Target(profile.server, session_for(profile), scope, profile.name)


def resolve_target(ctx: click.Context, operation: Any, options: dict[str, Any]) -> Target:
    """Explicit mode when `--base-url`/`WEAVE_BASE_URL` or `--auth-config` is given, else profile mode.

    A `--profile` typed on the command line wins over an inherited `WEAVE_BASE_URL`;
    typing both `--profile` and `--base-url`/`--auth-config` is a usage error.
    """
    from click.core import ParameterSource

    typed_profile = options.get("profile") is not None and (
        ctx.get_parameter_source("profile") is ParameterSource.COMMANDLINE
    )
    typed_base = options.get("base_url") is not None and (
        ctx.get_parameter_source("base_url") is ParameterSource.COMMANDLINE
    )
    if typed_profile and (typed_base or options.get("auth_config")):
        raise click.UsageError("Use either --profile or --base-url/--auth-config.")
    if options.get("auth_config") or typed_base or (options.get("base_url") is not None and not typed_profile):
        return _explicit_target(operation, options)
    if options.get("base_url") is not None:
        # The ignored WEAVE_BASE_URL platform owns any inherited WEAVE_*_ID scope: never send it to the profile.
        inherited = {
            key: None
            for key in ("tenant", "project", "environment")
            if ctx.get_parameter_source(key) is ParameterSource.ENVIRONMENT
        }
        options = {**options, **inherited}
    return _profile_target(operation, options)


def command(operation_id: str, name: str | None = None) -> click.Command:
    from firefly_weave.contracts.surface import OPERATIONS

    operation = OPERATIONS[operation_id]

    def execute(**options: Any) -> None:
        import asyncio

        from firefly_weave.sdk.auth import AuthError
        from firefly_weave.sdk.client import WeaveClient
        from firefly_weave.sdk.credentials import CredentialError
        from firefly_weave.sdk.errors import WeaveError

        ctx = click.get_current_context()
        target: Target | None = None
        notices: list[str] = []

        async def invoke(base_url: str, provider: Any, scope: Any) -> Any:
            identifier = options.get("identifier")
            if operation_id == "retention.apply":
                selected = options.get("plan_id")
                if selected is not None and identifier is not None and selected != identifier:
                    raise ValueError("Conflicting plan selection")
                identifier = selected or identifier
                if identifier is None:
                    raise ValueError("A plan identifier is required")

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
            async with WeaveClient(base_url, provider, scope) as sdk:
                if operation_id == "retention.apply":
                    assert identifier is not None
                    plan = await sdk.read_retention_plan(identifier)
                    if plan.id != identifier:
                        raise ValueError("Returned plan does not match selection")
                    # Keep stdout as the final machine result; preview before the mutation.
                    click.echo(plan.model_dump_json(), err=True)
                result = await sdk.invoke(
                    operation_id,
                    identifier=identifier,
                    state_id=options.get("state_id"),
                    collection=options.get("collection"),
                    body=body,
                    revision=options.get("revision"),
                    idempotency_key=options.get("idempotency_key"),
                    query=query or None,
                )
                if operation_id.startswith("connections."):
                    from firefly_weave.cli.connections import plain_http_notice

                    notice = await plain_http_notice(sdk, operation_id, identifier, result)
                    if notice is not None:
                        notices.append(notice)
                return result

        try:
            target = resolve_target(ctx, operation, options)
            result = asyncio.run(invoke(target.base_url, target.provider, target.scope))
            machine_result(result)
            # Standard output stays the JSON result; a plain-HTTP connection adds one line on standard error.
            for notice in notices:
                click.echo(notice, err=True)
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
            message = "Authentication failed"
            if target is not None and target.profile is not None and error.exit_code != 3:
                # Exit 3 means the identity provider was unreachable: signing in again would not help.
                message += f"; sign in with 'weave auth login --profile {shlex.quote(target.profile)}'"
            machine_result({"code": error.code, "message": message, "status": 401, "diagnostics": []})
            raise click.exceptions.Exit(error.exit_code) from None
        except TargetProblem as problem:
            machine_result({"code": "WV-CLI-CONFIG", "message": problem.message, "status": 422, "diagnostics": []})
            raise click.exceptions.Exit(2) from None
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
        click.Option(
            ["--profile"], envvar="WEAVE_PROFILE", help="Saved platform (default: the active one; see weave auth)."
        ),
        click.Option(["--base-url"], envvar="WEAVE_BASE_URL", help="API origin; selects explicit mode."),
        click.Option(
            ["--tenant"], type=click.UUID, envvar="WEAVE_TENANT_ID", help="Tenant id (default: the profile workspace)."
        ),
        click.Option(
            ["--project"],
            type=click.UUID,
            envvar="WEAVE_PROJECT_ID",
            help="Project id (default: the profile workspace).",
        ),
        click.Option(
            ["--environment"],
            type=click.UUID,
            envvar="WEAVE_ENVIRONMENT_ID",
            help="Environment id (default: the profile workspace).",
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
                    ["workflows", "actions", "connectors", "decision-tables", "drafts"]
                    if operation.method == "GET"
                    else ["workflows", "actions", "connectors", "decision-tables"]
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
for operation_id in (
    "compiler.compile",
    "compiler.validate",
    "compiler.evaluate_decision",
    "catalog.read",
    "capabilities.read",
    "language.read",
    "schemas.read",
):
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
