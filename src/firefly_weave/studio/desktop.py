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

"""Owned native sidecar: a ready-only loopback bootstrap and parent-bound lifetime."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from firefly_weave.cli.auth import session_from_options
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions, StudioProfile

if TYPE_CHECKING:
    from firefly_weave.sdk.auth import OAuthSession


class DesktopServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, options: StudioOptions) -> None:
        super().__init__(config)
        self.options = options

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            # stdout is a private pipe owned by the native launcher, never a URL or a log.
            sys.stdout.write(
                json.dumps(
                    {
                        "type": "weave-desktop-ready",
                        "version": 1,
                        "origin": self.options.origin,
                        "pairing_code": self.options.pairing_code,
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


def packaged_assets() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return root / "studio-assets"


def profile_session(profile_path: Path) -> tuple[StudioProfile, OAuthSession]:
    if profile_path.stat().st_size > 65536:
        raise ValueError("Oversized profile")
    profile = StudioProfile.model_validate_json(profile_path.read_bytes())
    if not profile.auth_config:
        raise ValueError("A saved OAuth profile is required")
    provider = session_from_options(
        {
            "auth_config": profile.auth_config,
            "credential_store": profile.credential_store,
            "credential_file": profile.credential_file,
        }
    )
    if provider.config.target != profile.base_url.rstrip("/"):
        raise ValueError("Profile authentication target mismatch")
    return profile, provider


def run_login(profile_path: Path) -> None:
    _, provider = profile_session(profile_path)

    async def acquire() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        def parent_control() -> None:
            for line in sys.stdin:
                if line.strip() == "STOP":
                    break
            if task is not None and not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)

        threading.Thread(target=parent_control, name="studio-login-parent", daemon=True).start()

        def instructions(uri: str, code: str) -> None:
            provider.config.trust_endpoint(uri)
            with suppress(OSError, webbrowser.Error):
                webbrowser.open(uri, new=2)
            sys.stdout.write(json.dumps({"type": "weave-desktop-login", "uri": uri, "code": code}) + "\n")
            sys.stdout.flush()

        await provider.login(flow="device", instructions=instructions)
        sys.stdout.write('{"type":"weave-desktop-login-complete"}\n')
        sys.stdout.flush()

    asyncio.run(acquire())


def run_desktop(profile_path: Path | None, assets: Path) -> None:
    profile = None
    provider = None
    if profile_path is not None:
        profile, provider = profile_session(profile_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        options = StudioOptions(
            origin=f"http://127.0.0.1:{port}",
            assets=assets,
            profile=profile,
            token_provider=provider,
            open_login_browser=True,
        )
        app = make_studio_app(options)
        server = DesktopServer(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                proxy_headers=False,
                log_level="critical",
                loop="asyncio",
                http="h11",
            ),
            options,
        )

        # EOF closes an orphaned sidecar even if the native launcher crashes.
        def parent_control() -> None:
            for line in sys.stdin:
                if line.strip() == "STOP":
                    break
            server.should_exit = True

        threading.Thread(target=parent_control, name="studio-parent-control", daemon=True).start()
        asyncio.run(server.serve(sockets=[listener]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Studio desktop host")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--assets", type=Path, default=packaged_assets())
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    try:
        if args.login:
            if args.profile is None:
                raise ValueError("Choose a profile before signing in")
            run_login(args.profile)
        else:
            run_desktop(args.profile, args.assets)
    except (Exception, asyncio.CancelledError):
        # Configuration/library errors can contain credentials or private profile contents.
        sys.stderr.write("Studio host could not start. Check the selected profile and matching application bundle.\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
