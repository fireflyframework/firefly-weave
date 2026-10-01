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

"""Explicit client login; this module never imports server settings or a database."""

from pathlib import Path
from typing import Any

import click

from firefly_weave.cli.remote import machine_result


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


@click.group()
def auth() -> None:
    """Device/PKCE login, local status and credential logout."""


def auth_command(name: str) -> None:
    def execute(**options: Any) -> None:
        import asyncio

        from firefly_weave.sdk.auth import AuthError
        from firefly_weave.sdk.credentials import CredentialError

        try:
            session = session_from_options(options)
            if name == "login":
                result = asyncio.run(
                    session.login(
                        flow=options["flow"],
                        instructions=lambda uri, code: click.echo(f"Open {uri} and enter {code}", err=True),
                    )
                )
            elif name == "logout":
                result = asyncio.run(session.logout(revoke=options["revoke"]))
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

    params: list[click.Parameter] = [
        click.Option(["--auth-config"], required=True, type=click.Path(exists=True, path_type=Path)),
        click.Option(["--credential-store"], type=click.Choice(["native", "file"]), default="native"),
        click.Option(["--credential-file"], type=click.Path(path_type=Path)),
        click.Option(["--output"], type=click.Choice(["json"]), default="json"),
    ]
    if name == "login":
        params.append(click.Option(["--flow"], type=click.Choice(["auto", "device", "pkce"]), default="auto"))
    if name == "logout":
        params.append(click.Option(["--revoke"], is_flag=True))
    auth.add_command(click.Command(name, callback=execute, params=params))


for name in ("login", "logout", "status"):
    auth_command(name)
