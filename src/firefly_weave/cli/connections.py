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

"""Connection revision command adapters.

``connections create`` keeps ``--request FILE`` as the raw path and adds guided
flags for the built-in ``weave-http@2.0.0`` connector: the request is built and
checked locally with secret *handles* only, and the connector version ID is
resolved from the project's published connectors. ``--base-url`` stays the
platform address, so the called API's origin is ``--api-url``. Commands whose
request is optional on the wire (``connections test``) no longer need a
``--request`` file.
"""

from typing import Any

import click

from firefly_weave.cli.http_actions import FlowProblem, InlineRequest, run_remote
from firefly_weave.cli.remote import family, machine_result
from firefly_weave.compiler.canonical import canonical_bytes

HTTP_CONNECTOR = "weave-http@2.0.0"
AUTH_KINDS = ("none", "api-key", "basic", "bearer", "machine-token")
GUIDED = (
    "name",
    "connector",
    "connector_version_id",
    "api_url",
    "auth",
    "auth_header",
    "client_id",
    "token_endpoint",
    "scopes",
    "secrets",
    "allow",
)

connections = family("connections", "connections")


def _request_option(command: click.Command, help_text: str) -> None:
    for param in command.params:
        if isinstance(param, click.Option) and param.name == "request_path":
            param.required = False
            param.help = help_text


def _optional_request(command: click.Command) -> None:
    """Send the empty request when the operation accepts one, instead of demanding a ``{}`` file."""
    from firefly_weave.contracts.surface import OPERATIONS

    operation = OPERATIONS["connections." + (command.name or "").replace("-", "_")]
    original = command.callback
    if operation.request_required or original is None:
        return
    _request_option(command, "JSON request file (optional: an empty request is sent without it).")

    def callback(**options: Any) -> Any:
        if options.get("request_path") is None:
            options["request_path"] = InlineRequest(b"{}")
        return original(**options)

    command.callback = callback


def _secrets(values: tuple[str, ...]) -> dict[str, str]:
    handles: dict[str, str] = {}
    for value in values:
        slot, separator, handle = value.partition("=")
        if not separator or not slot or not handle or slot in handles:
            raise click.UsageError("Use --secret SLOT=HANDLE once per slot, such as --secret api_key=pets-api-key.")
        handles[slot] = handle
    return handles


def _auth(kind: str, header: str | None, client_id: str | None, endpoint: str | None, scopes: tuple[str, ...]) -> Any:
    auth: dict[str, Any] = {"kind": kind}
    if header is not None:
        auth["header"] = header
    if client_id is not None:
        auth["client_id"] = client_id
    if endpoint is not None:
        auth["endpoint"] = endpoint
    if scopes:
        auth["scopes"] = list(scopes)
    return auth


async def _connector_version(sdk: Any) -> Any:
    """The published weave-http@2.0.0 Connector version ID in the selected project."""
    name, version = HTTP_CONNECTOR.split("@")
    cursor: str | None = None
    for _ in range(20):
        query: dict[str, str | int] = {"limit": 100}
        if cursor:
            query["cursor"] = cursor
        page = await sdk.invoke("definitions.list", collection="connectors", query=query)
        for item in page.items:
            if getattr(item, "name", None) == name and getattr(item, "version", None) == version:
                return item.id
        cursor = page.next_cursor
        if not cursor:
            break
    raise FlowProblem(
        "WV-CONNECTION-CONNECTOR",
        "weave-http@2.0.0 is not published in this project; publish the manifest shown by "
        "'weave connector descriptor weave-http-v2', or pass --connector-version-id",
        404,
    )


async def plain_http_notice(sdk: Any, operation_id: str, identifier: Any, result: Any) -> str | None:
    """The "Not encrypted" line for a connection answer; a test answer carries only the flag, so read the origin."""
    from firefly_weave.contracts.connectors import ConnectionRevision, plain_text_warning

    if operation_id == "connections.test":
        if getattr(result, "encrypted", None) is not False:
            return None
        result = await sdk.invoke("connections.read", identifier=identifier)
    return plain_text_warning(result.config) if isinstance(result, ConnectionRevision) else None


def _guided_create(raw: click.Command) -> click.Command:
    """``connections create`` with the raw ``--request`` path and guided weave-http@2.0.0 flags."""
    from firefly_weave.contracts.connectors import ConnectionRequest, plain_text_warning

    original = raw.callback
    assert original is not None

    def callback(**options: Any) -> Any:
        from click.core import ParameterSource

        from firefly_weave.sdk.http_actions import HttpActionError, build_connection_request

        ctx = click.get_current_context()
        guided = {key: options.pop(key) for key in GUIDED}
        given = {key for key in GUIDED if ctx.get_parameter_source(key) is not ParameterSource.DEFAULT}
        if options.get("request_path") is not None:
            if given:
                raise click.UsageError("Use either --request or the guided flags.")
            return original(**options)
        if not {"name", "api_url"} <= given:
            raise click.UsageError("Pass --request FILE, or the guided flags --name and --api-url.")
        if guided["connector"] != HTTP_CONNECTOR:
            raise click.UsageError("Guided flags support weave-http@2.0.0; use --request for other connectors.")
        auth = _auth(
            guided["auth"], guided["auth_header"], guided["client_id"], guided["token_endpoint"], guided["scopes"]
        )
        handles = _secrets(guided["secrets"])

        def request(identifier: Any) -> ConnectionRequest:
            body = build_connection_request(
                guided["name"], guided["api_url"], auth, handles, identifier, guided["allow"]
            )
            return ConnectionRequest.model_validate_json(canonical_bytes(body))

        try:
            # Check everything locally before any network call; the version ID may be resolved below.
            checked = request(guided["connector_version_id"] or "00000000-0000-0000-0000-000000000000")
        except HttpActionError as error:
            machine_result(
                {
                    "code": "WV-CONNECTION-INPUT",
                    "message": "The connection settings are invalid; see diagnostics",
                    "status": 422,
                    "diagnostics": [d.model_dump(by_alias=True, mode="json") for d in error.diagnostics],
                }
            )
            raise click.exceptions.Exit(2) from None

        async def create(sdk: Any) -> Any:
            identifier = guided["connector_version_id"] or await _connector_version(sdk)
            return await sdk.invoke("connections.create", body=request(identifier))

        machine_result(run_remote(ctx, "connections.create", options, create))
        warning = plain_text_warning(checked.config)
        if warning is not None:
            # Plain HTTP is allowed but never silent; standard output stays the JSON result.
            click.echo(warning, err=True)
        return None

    _request_option(raw, "Raw ConnectionRequest JSON file; or use the guided weave-http@2.0.0 flags.")
    guided_params: list[click.Parameter] = [
        click.Option(["--name"], help="Connection name, such as pets."),
        click.Option(
            ["--connector"], default=HTTP_CONNECTOR, show_default=True, help="Guided flags support only this."
        ),
        click.Option(["--connector-version-id"], type=click.UUID, help="Skip resolving the published connector."),
        click.Option(
            ["--api-url"],
            help="The called API's origin, such as https://api.example.com; http:// works but is not encrypted "
            "(base paths go in the Action).",
        ),
        click.Option(["--auth"], type=click.Choice(AUTH_KINDS), default="none", show_default=True),
        click.Option(["--auth-header"], help="Header that carries the API key (api-key auth)."),
        click.Option(["--client-id"], help="OAuth client ID (machine-token auth); not a secret."),
        click.Option(["--token-endpoint"], help="OAuth token endpoint (machine-token auth); allowed automatically."),
        click.Option(["--scope", "scopes"], multiple=True, help="OAuth scope (machine-token auth). Repeatable."),
        click.Option(
            ["--secret", "secrets"],
            multiple=True,
            help="SLOT=HANDLE: an operator-provided secret handle, never the secret value. Repeatable.",
        ),
        click.Option(["--allow"], multiple=True, help="Another literal HTTPS or HTTP origin to allow. Repeatable."),
    ]
    return click.Command(
        "create",
        params=[*raw.params, *guided_params],
        callback=callback,
        help=(
            (raw.help or "")
            + "\n\nGuided weave-http@2.0.0 example: --name pets --api-url https://api.example.com --auth api-key "
            "--auth-header X-API-Key --secret api_key=pets-api-key. The API origin (and a machine-token endpoint) "
            "is allowed automatically; secrets are referenced by handle only."
        ),
        short_help="Create a connection revision (raw request or guided weave-http flags).",
    )


connections.add_command(_guided_create(connections.commands["create"]))
_optional_request(connections.commands["test"])
