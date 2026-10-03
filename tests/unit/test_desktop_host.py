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

"""Native child readiness, isolated loopback session and owned shutdown."""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


@pytest.mark.parametrize("parent_eof", [False, True])
async def test_sidecar_emits_readiness_only_after_server_and_stops_on_parent_command(tmp_path: Path, parent_eof: bool):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Studio</title>")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "firefly_weave.studio.desktop",
        "--assets",
        str(assets),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        # Saved platforms come from this private directory, never the developer's own profiles.
        env={**os.environ, "WEAVE_CONFIG_HOME": str(tmp_path / "config")},
    )
    try:
        assert child.stdout is not None
        raw = await asyncio.wait_for(child.stdout.readline(), timeout=15)
        bootstrap = json.loads(raw)
        assert bootstrap["type"] == "weave-desktop-ready"
        assert bootstrap["version"] == 1
        assert len(raw) < 1024
        origin = bootstrap["origin"]
        parsed = urlsplit(origin)
        assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
        assert not parsed.query and not parsed.fragment and not parsed.username
        assert bootstrap["pairing_code"] not in origin
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            assert (await client.get("/studio/session")).json()["paired"] is False
            assert (await client.get("/", headers={"Host": "evil.example"})).status_code == 403
            paired = await client.post(
                "/studio/session", json={"code": bootstrap["pairing_code"]}, headers={"Origin": origin}
            )
            assert paired.status_code == 200
            assert (await client.get("/studio/session")).json()["paired"] is True
            status = (await client.get("/studio/connection")).json()
            assert status["store"] == {"available": True, "location": str(tmp_path / "config" / "profiles.json")}
            assert status["configured"] is False
            unpaired = await client.delete(
                "/studio/session", headers={"Origin": origin, "X-Weave-CSRF": paired.json()["csrfToken"]}
            )
            assert unpaired.status_code == 204
            # Desktop pairing is reusable: the native shell's reload pairs again with the same code.
            again = await client.post(
                "/studio/session", json={"code": bootstrap["pairing_code"]}, headers={"Origin": origin}
            )
            assert again.status_code == 200
            assert again.json()["csrfToken"] != paired.json()["csrfToken"]
        assert child.stdin is not None
        if parent_eof:
            child.stdin.close()
        else:
            child.stdin.write(b"STOP\n")
            await child.stdin.drain()
        assert await asyncio.wait_for(child.wait(), timeout=5) == 0
    finally:
        if child.returncode is None:
            child.terminate()
            await child.wait()


async def test_invalid_profile_never_publishes_launch_url(tmp_path: Path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"name": "secret-canary-name", "base_url": "http://evil.example"}))
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "firefly_weave.studio.desktop",
        "--profile",
        str(profile),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=15)
    assert child.returncode != 0
    assert not stdout
    assert b"secret-canary-name" not in stderr
    assert b"evil.example" not in stderr


@pytest.mark.parametrize("open_failure", [False, True])
def test_native_login_validates_browser_destination_and_never_emits_tokens(monkeypatch, capsys, open_failure):
    from types import SimpleNamespace

    from firefly_weave.studio import desktop

    events = []

    async def login(*, flow, instructions):
        assert flow == "device"
        instructions("https://id.example/activate", "ABCD-1234")
        return "access-token-canary"

    config = SimpleNamespace(trust_endpoint=lambda uri: events.append(("trusted", uri)))
    provider = SimpleNamespace(config=config, login=login)
    monkeypatch.setattr(desktop, "profile_session", lambda path: (None, provider))

    def open_browser(uri, new):
        events.append(("opened", uri))
        if open_failure:
            raise OSError("Browser unavailable")

    monkeypatch.setattr(desktop.webbrowser, "open", open_browser)
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    desktop.run_login(Path("profile.json"))
    output = capsys.readouterr().out
    assert "access-token-canary" not in output
    assert [json.loads(line)["type"] for line in output.splitlines()] == [
        "weave-desktop-login",
        "weave-desktop-login-complete",
    ]
    assert events == [("trusted", "https://id.example/activate"), ("opened", "https://id.example/activate")]


def test_native_login_stops_on_parent_cancel(monkeypatch, capsys):
    from io import StringIO
    from types import SimpleNamespace

    from firefly_weave.studio import desktop

    async def login(**kwargs):
        await asyncio.sleep(60)
        raise AssertionError("Parent cancellation was ignored")

    monkeypatch.setattr(desktop, "profile_session", lambda path: (None, SimpleNamespace(login=login)))
    monkeypatch.setattr(desktop.sys, "stdin", StringIO("STOP\n"))
    with pytest.raises(asyncio.CancelledError):
        desktop.run_login(Path("profile.json"))
    assert not capsys.readouterr().out


def test_native_login_rejects_untrusted_destination_before_browser_or_output(monkeypatch, capsys):
    from types import SimpleNamespace

    from firefly_weave.studio import desktop

    def reject(uri):
        raise ValueError("Untrusted destination")

    async def login(*, flow, instructions):
        instructions("https://untrusted.example", "PUBLIC")

    provider = SimpleNamespace(config=SimpleNamespace(trust_endpoint=reject), login=login)
    monkeypatch.setattr(desktop, "profile_session", lambda path: (None, provider))
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda *args, **kwargs: opened.append(args))
    with pytest.raises(ValueError, match="Untrusted"):
        desktop.run_login(Path("profile.json"))
    assert not opened
    assert not capsys.readouterr().out


@pytest.mark.parametrize("legacy", [False, True])
def test_desktop_uses_saved_platforms_unless_a_profile_file_is_given(tmp_path, monkeypatch, legacy):
    from types import SimpleNamespace

    from firefly_weave.sdk.profiles import ProfileStore
    from firefly_weave.studio import desktop
    from firefly_weave.studio.service import StudioProfile

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Studio</title>")
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(tmp_path / "config"))
    captured = []

    class Server:
        def __init__(self, config, options):
            captured.append(options)

        async def serve(self, sockets):
            return None

    monkeypatch.setattr(desktop, "DesktopServer", Server)
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    profile = StudioProfile(name="Legacy", base_url="https://api.example")
    monkeypatch.setattr(desktop, "profile_session", lambda path: (profile, lambda: "token"))
    desktop.run_desktop(tmp_path / "profile.json" if legacy else None, assets)
    (options,) = captured
    # The pairing code never leaves the native shell, so a reload may pair again in both modes.
    assert options.open_login_browser is True and options.reusable_pairing is True
    if legacy:
        assert options.profile_store is None and options.profile is profile
    else:
        assert isinstance(options.profile_store, ProfileStore)
        assert options.profile_store.path == tmp_path / "config" / "profiles.json"
        assert options.reusable_pairing is True and options.profile is None


def test_desktop_starts_offline_when_the_store_location_is_unusable(tmp_path, monkeypatch):
    from firefly_weave.studio import desktop

    monkeypatch.setenv("WEAVE_CONFIG_HOME", "relative/config")
    assert desktop.saved_platforms() is None


async def test_frozen_smoke_never_reads_the_developers_saved_platforms(tmp_path, monkeypatch, capsys):
    import runpy

    from firefly_weave.sdk.auth import LoginConfig
    from firefly_weave.sdk.profiles import PlatformProfile, ProfileStore

    tmp_path.chmod(0o700)
    root = Path(__file__).resolve().parents[2]
    # The developer's own configuration has an active platform; the smoke must not see it.
    developer = tmp_path / "developer-config"
    login = LoginConfig(
        provider_id="acme",
        issuer="https://login.example/realms/acme",
        client_id="weave-cli",
        target="https://weave.example",
        account="prod",
    )
    store = ProfileStore(developer / "profiles.json")
    store.save(PlatformProfile(name="prod", login=login, source="server"), activate=True)
    before = store.path.read_bytes()
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(developer))
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><html><title>Studio</title></html>")
    # Stands in for the frozen host: the smoke clears PYTHONPATH, so the wrapper sets the source tree.
    host = tmp_path / "weave-studio-host"
    host.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{root / "src"}" exec "{sys.executable}" -m firefly_weave.studio.desktop --assets "{assets}"\n'
    )
    host.chmod(0o700)
    check = runpy.run_path(str(root / "desktop/scripts/smoke_sidecar.py"))["check"]
    await check(host)
    assert "passed" in capsys.readouterr().out
    assert store.path.read_bytes() == before and sorted(p.name for p in developer.iterdir()) == [
        "profiles.json",
        "profiles.json.lock",
    ]
