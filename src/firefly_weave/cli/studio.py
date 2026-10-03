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

"""Launch an explicitly paired, loopback-only browser Studio without Node.js, using saved platforms."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from uuid import UUID

import click


class StudioLaunchError(click.ClickException):
    """Value-free, actionable launcher failures safe for the root CLI to display."""


def is_profile_file(value: str) -> bool:
    """A legacy profile file names a path or a `.json` file; anything else is a saved platform name."""
    separators = {"/", os.sep} | ({os.altsep} if os.altsep else set())
    return any(separator in value for separator in separators) or value.lower().endswith(".json")


@click.group(invoke_without_command=True)
@click.option(
    "--profile",
    envvar="WEAVE_STUDIO_PROFILE",
    metavar="NAME|FILE",
    help=(
        "Saved platform to use (it becomes the active one), or a legacy Studio profile JSON file. "
        "Omit to reconnect to the active saved platform, or to work locally."
    ),
)
@click.option(
    "--assets",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Development build directory containing index.html.",
)
@click.option("--port", type=click.IntRange(1024, 65535), default=8766, show_default=True)
@click.option("--no-browser", is_flag=True, help="Print the local address without opening a browser.")
@click.option("--token-env", help="Explicit environment variable holding an API token; needs a legacy profile file.")
@click.pass_context
def studio(
    ctx: click.Context, profile: str | None, assets: Path | None, port: int, no_browser: bool, token_env: str | None
) -> None:
    """Draw workflows and work with a local or remote platform in your browser."""
    if ctx.invoked_subcommand:
        return
    try:
        import socket
        import threading
        import webbrowser

        import uvicorn

        from firefly_weave.cli.auth import session_from_options
        from firefly_weave.sdk.profiles import ProfileError, ProfileStore
        from firefly_weave.studio.assets import find_assets
        from firefly_weave.studio.host import make_studio_app
        from firefly_weave.studio.service import StudioOptions, StudioProfile

        selected = None
        provider = None
        store = None
        path = Path(profile) if profile and is_profile_file(profile) else None
        if profile and path is None:
            try:
                store = ProfileStore()
                store.get(profile)
            except ProfileError as error:
                if error.code in {"WV-PROFILE-NOT-FOUND", "WV-PROFILE-NAME"} and Path(profile).is_file():
                    # Earlier releases accepted any existing file; keep that when no saved platform has the name.
                    path, store = Path(profile), None
                else:
                    hint = (
                        " List saved platforms with: weave auth profiles"
                        if error.code == "WV-PROFILE-NOT-FOUND"
                        else ""
                    )
                    raise StudioLaunchError(error.message + hint) from None
        if path is not None:
            if not path.is_file():
                raise StudioLaunchError("Studio profile file not found; check the --profile path")
            if path.stat().st_size > 65536:
                raise ValueError("Studio profile exceeds size limit")
            selected = StudioProfile.model_validate_json(path.read_bytes())
            if selected.auth_config:
                provider = session_from_options(
                    {
                        "auth_config": selected.auth_config,
                        "credential_store": selected.credential_store,
                        "credential_file": selected.credential_file,
                    }
                )
            elif token_env:

                def environment_token() -> str:
                    return os.environ.get(token_env, "")

                provider = environment_token
            else:
                raise ValueError("Connected Studio needs auth_config in its profile or an explicit --token-env")
        elif token_env:
            raise ValueError("--token-env requires a legacy Studio profile file")
        elif store is not None and profile:
            try:
                store.activate(profile)
            except ProfileError as error:
                raise StudioLaunchError(error.message) from None
        else:
            try:
                store = ProfileStore()
            except ProfileError as error:
                click.echo(
                    f"Saved platforms are unavailable: {error.message} Studio starts for local authoring.", err=True
                )
                store = None
        options = StudioOptions(
            origin=f"http://127.0.0.1:{port}",
            assets=assets or find_assets(),
            profile=selected,
            token_provider=provider,
            profile_store=store,
        )
        app = make_studio_app(options)
        connection = app.state.studio_connection
        if connection.store_error is not None:
            click.echo(
                f"Saved platforms could not be read: {connection.store_error.message} "
                "Studio starts for local authoring.",
                err=True,
            )
        # Own the listening socket before showing a URL or starting a browser.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            active = options.profile
            click.echo(
                "Firefly Weave Studio · "
                + (f"Platform: {active.name}" if active else "Local authoring (no platform selected)")
            )
            click.echo(f"Open {options.origin}")
            click.echo(f"Pairing code: {options.pairing_code}")
            click.echo("Enter this code in the browser. Press Ctrl+C to stop Studio.")
            timer = None
            if not no_browser:
                timer = threading.Timer(1, lambda: webbrowser.open(options.origin))
                timer.daemon = True
                timer.start()
            try:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False, log_level="warning"
                    )
                )
                server.run(sockets=[listener])
            finally:
                if timer:
                    timer.cancel()
    except ImportError:
        raise StudioLaunchError(
            "Studio requires the studio extra: install firefly-weave[studio] for your release"
        ) from None
    except (OSError, ValueError) as error:
        # Pydantic input errors can include profile fields; keep file contents private.
        from pydantic import ValidationError

        message = (
            "Invalid Studio profile; check the documented non-secret fields"
            if isinstance(error, ValidationError)
            else str(error)
        )
        raise StudioLaunchError(message) from None


@studio.command("install")
@click.option("--bundle", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sha256", required=True, help="Published SHA-256 checksum of the matching Studio ZIP.")
def install(bundle: Path, sha256: str) -> None:
    """Verify and install a matching release bundle; Node.js is not required."""
    from firefly_weave.studio.assets import install_bundle

    try:
        path = install_bundle(bundle, sha256)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise StudioLaunchError(str(error)) from None
    click.echo(f"Studio installed at {path}. Start it with: weave studio")


@studio.command("configure")
@click.option("--auth-config", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--name", default="My platform", show_default=True)
@click.option("--tenant", type=click.UUID)
@click.option("--project", type=click.UUID)
@click.option("--environment", type=click.UUID)
@click.option("--credential-store", type=click.Choice(["native", "file"]), default="native")
@click.option("--credential-file", type=click.Path(dir_okay=False, path_type=Path))
def configure(
    auth_config: Path,
    output: Path,
    name: str,
    tenant: UUID | None,
    project: UUID | None,
    environment: UUID | None,
    credential_store: str,
    credential_file: Path | None,
) -> None:
    """Create a non-secret Studio profile from your existing CLI login configuration.

    The API origin comes from that login configuration. Existing files are never
    replaced. Log in with weave auth login before connecting Studio.
    """
    from firefly_weave.sdk.auth import AuthError, LoginConfig
    from firefly_weave.studio.service import StudioProfile

    try:
        with auth_config.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        login = LoginConfig.model_validate_json(raw)
        if (credential_store == "file") != (credential_file is not None):
            raise StudioLaunchError("Use --credential-store file and --credential-file together")
        profile = StudioProfile(
            name=name,
            base_url=login.target,
            auth_config=auth_config.resolve(),
            tenant_id=tenant,
            project_id=project,
            environment_id=environment,
            credential_store=credential_store,
            credential_file=credential_file.resolve() if credential_file else None,
        )
        # Exclusive creation also rejects a pre-existing symlink to another file.
        with output.open("x", encoding="utf-8") as stream:
            stream.write(profile.model_dump_json(indent=2, exclude_none=True) + "\n")
    except FileExistsError:
        raise StudioLaunchError("Profile already exists; choose a new --output path") from None
    except (OSError, ValueError, AuthError):
        raise StudioLaunchError(
            "Cannot create profile; check the login configuration, scope IDs and output path"
        ) from None
    click.echo(f"Studio profile saved: {output.resolve()}")
    click.echo("Launch with weave studio --profile followed by this path.")
