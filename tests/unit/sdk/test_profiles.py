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

"""Shared platform profiles: private atomic storage, locking, bounds and legacy conversion."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.sdk import profiles
from firefly_weave.sdk.auth import LoginConfig
from firefly_weave.sdk.profiles import (
    AccountHint,
    PlatformProfile,
    ProfileDocument,
    ProfileError,
    ProfileStore,
    StudioPreferences,
    WorkspaceSelection,
    default_config_dir,
    profile_name,
)

SRC = Path(__file__).resolve().parents[3] / "src"


def login(name="prod", target="https://weave.example", **changes):
    return LoginConfig(
        provider_id="acme",
        issuer="https://login.example/realms/acme",
        client_id="weave-cli",
        target=target,
        account=name,
        scopes=("openid",),
        **changes,
    )


def profile(name="prod", **changes):
    return PlatformProfile(name=name, login=login(name), source="server", **changes)


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    return ProfileStore(tmp_path / "config" / "profiles.json")


def code(failure):
    return failure.value.code


# --- configuration directory -------------------------------------------------------------------------------------


def test_config_home_override_must_be_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(tmp_path / "weave"))
    assert default_config_dir() == tmp_path / "weave"
    assert ProfileStore().path == tmp_path / "weave" / "profiles.json"
    monkeypatch.setenv("WEAVE_CONFIG_HOME", "relative/weave")
    with pytest.raises(ProfileError) as failure:
        default_config_dir()
    assert code(failure) == "WV-PROFILE-STORE"
    with pytest.raises(ProfileError):
        ProfileStore()


def test_platform_default_directories(monkeypatch, tmp_path):
    monkeypatch.delenv("WEAVE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "sys", SimpleNamespace(platform="darwin"))
    assert default_config_dir() == tmp_path / "Library" / "Application Support" / "Firefly Weave"
    monkeypatch.setattr(profiles, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_config_dir() == tmp_path / "xdg" / "firefly-weave"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-xdg")
    assert default_config_dir() == tmp_path / ".config" / "firefly-weave"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert default_config_dir() == tmp_path / ".config" / "firefly-weave"
    monkeypatch.setenv("WEAVE_CONFIG_HOME", "")
    assert default_config_dir() == tmp_path / ".config" / "firefly-weave"


def test_windows_default_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("WEAVE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "os", windows_os())
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert default_config_dir() == tmp_path / "Roaming" / "Firefly Weave"
    monkeypatch.delenv("APPDATA")
    assert default_config_dir() == tmp_path / "AppData" / "Roaming" / "Firefly Weave"


# --- names and models --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["prod", "Acme EU-1", "a.b_c", "7", "x" * 64, "Local Keycloak dev"])
def test_valid_profile_names(name):
    assert profile_name(name) == name
    assert profile(name).name == name


@pytest.mark.parametrize(
    "name", ["", " lead", "trail ", "-dash", ".dot", "x" * 65, "ünicode", "a/b", "a\nb", "a\tb", "a:b", None]
)
def test_invalid_profile_names(name, store):
    with pytest.raises(ProfileError) as failure:
        profile_name(name)
    assert code(failure) == "WV-PROFILE-NAME"
    with pytest.raises(ProfileError) as failure:
        store.get(name)
    assert code(failure) == "WV-PROFILE-NAME"


def test_profile_model_pins_account_store_and_hint():
    with pytest.raises(ValidationError):
        PlatformProfile(name="prod", login=login("other"), source="server")
    with pytest.raises(ValidationError):
        profile(credential_store="file")
    with pytest.raises(ValidationError):
        profile(credential_file=Path("/tmp/tokens.json"))
    with pytest.raises(ValidationError):
        profile(credential_store="file", credential_file=Path("relative.json"))
    with pytest.raises(ValidationError):
        profile(unknown=True)
    with pytest.raises(ValidationError):
        profile(created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        profile(account=AccountHint(subject="s", issuer="https://evil.example", provider_id="acme"))
    with pytest.raises(ValidationError):
        AccountHint(subject="x" * 256, issuer="https://login.example/realms/acme", provider_id="acme")
    with pytest.raises(ValidationError):
        AccountHint(subject="bad\nsubject", issuer="https://login.example/realms/acme", provider_id="acme")
    hinted = profile(
        account=AccountHint(
            subject="s", display_name="Ada", issuer="https://login.example/realms/acme", provider_id="acme"
        )
    )
    assert hinted.server == "https://weave.example" and hinted.account.display_name == "Ada"
    filed = profile(credential_store="file", credential_file=Path("/var/weave/tokens.json"))
    assert filed.credential_file == Path("/var/weave/tokens.json")
    stamped = profile(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert stamped.created_at.tzinfo is not None
    with pytest.raises(ValidationError):
        stamped.name = "other"


def test_profiles_remember_the_sign_in_flows_an_administrator_allows(store):
    assert profile().flows == ["browser", "device"]
    assert profile(flows=["device"]).flows == ["device"]
    for invalid in ([], ["browser", "browser"], ["password"], ["browser", "device", "browser"]):
        with pytest.raises(ValidationError):
            profile(flows=invalid)
    store.save(profile(flows=["browser"]), activate=True)
    assert store.get("prod").flows == ["browser"]
    hint = AccountHint(subject="s", issuer="https://login.example/realms/acme", provider_id="acme")
    assert store.set_account("prod", hint).flows == ["browser"]
    assert json.loads(store.path.read_text())["profiles"]["prod"]["flows"] == ["browser"]
    # Profiles saved before flows existed allow both flows.
    document = json.loads(store.path.read_text())
    del document["profiles"]["prod"]["flows"]
    store.path.write_text(json.dumps(document))
    assert store.active().flows == ["browser", "device"]


def test_document_keys_and_active_are_consistent():
    with pytest.raises(ValidationError):
        ProfileDocument(profiles={"other": profile()})
    with pytest.raises(ValidationError):
        ProfileDocument(active="missing", profiles={"prod": profile()})
    with pytest.raises(ValidationError):
        ProfileDocument(profiles={"prod": profile(), "PROD": profile("PROD")})
    with pytest.raises(ValidationError):
        ProfileDocument(version=2)


# --- store behavior ----------------------------------------------------------------------------------------------


def test_round_trip_activation_and_errors(store, tmp_path):
    assert store.load() == ProfileDocument()
    assert store.names() == [] and store.active() is None
    assert not (tmp_path / "config").exists(), "Reading must not create the store"
    with pytest.raises(ProfileError) as failure:
        store.resolve()
    assert code(failure) == "WV-PROFILE-NONE"
    saved = store.save(profile(), activate=True)
    assert store.get("prod") == saved and store.active() == saved and store.resolve() == saved
    assert stat_mode(tmp_path / "config") == 0o700 and stat_mode(store.path) == 0o600
    store.save(profile("staging"))
    assert store.names() == ["prod", "staging"] and store.active().name == "prod"
    with pytest.raises(ProfileError) as failure:
        store.save(profile())
    assert code(failure) == "WV-PROFILE-EXISTS"
    with pytest.raises(ProfileError) as failure:
        store.save(profile("Prod"), replace=True)
    assert code(failure) == "WV-PROFILE-EXISTS"
    replaced = store.save(profile(display_name="Acme"), replace=True)
    assert replaced.created_at == saved.created_at and replaced.updated_at >= saved.updated_at
    assert replaced.display_name == "Acme"
    assert store.activate("staging").name == "staging" and store.resolve().name == "staging"
    assert store.resolve("prod").name == "prod"
    for operation in (
        lambda: store.get("missing"),
        lambda: store.activate("missing"),
        lambda: store.remove("missing"),
        lambda: store.resolve("missing"),
        lambda: store.set_workspace("missing", None),
        lambda: store.set_account("missing", None),
    ):
        with pytest.raises(ProfileError) as failure:
            operation()
        assert code(failure) == "WV-PROFILE-NOT-FOUND"
    assert store.remove("staging").name == "staging"
    assert store.active() is None and store.names() == ["prod"]
    store.activate("prod")
    store.deactivate()
    assert store.active() is None and store.names() == ["prod"]
    store.deactivate()


def test_workspace_and_account_updates(store):
    store.save(profile(), activate=True)
    selection = WorkspaceSelection(
        tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4(), tenant_name="Acme", project_name="Ops"
    )
    assert store.set_workspace("prod", selection).workspace == selection
    hint = AccountHint(
        subject="sub", display_name="ada", issuer="https://login.example/realms/acme", provider_id="acme"
    )
    assert store.set_account("prod", hint).account == hint
    reloaded = store.get("prod")
    assert reloaded.workspace == selection and reloaded.account == hint
    with pytest.raises(ProfileError) as failure:
        store.set_account("prod", AccountHint(subject="s", issuer="https://evil.example", provider_id="acme"))
    assert code(failure) == "WV-PROFILE-ACCOUNT" and store.get("prod").account == hint
    with pytest.raises(ProfileError) as failure:
        store.set_account(
            "prod", AccountHint(subject="s", issuer="https://login.example/realms/acme", provider_id="other")
        )
    assert code(failure) == "WV-PROFILE-ACCOUNT"
    cleared = store.set_account("prod", None)
    assert cleared.account is None and store.set_workspace("prod", None).workspace is None
    raw = store.path.read_text()
    for forbidden in ("access_token", "refresh_token", "secret"):
        assert forbidden not in raw


def test_lookups_ignore_letter_case_and_keep_the_stored_name(store):
    store.save(profile("Prod"))
    store.save(profile("staging"), activate=True)
    assert store.get("prod").name == "Prod" and store.get("PROD").name == "Prod"
    assert store.resolve("pRoD").name == "Prod"
    assert store.activate("PROD").name == "Prod"
    assert store.load().active == "Prod"
    hint = AccountHint(subject="sub", issuer="https://login.example/realms/acme", provider_id="acme")
    assert store.set_account("prod", hint).account == hint
    selection = WorkspaceSelection(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    assert store.set_workspace("PROD", selection).workspace == selection
    assert store.names() == ["Prod", "staging"], "Updates must never add a case variant"
    removed = store.remove("prod")
    assert removed.name == "Prod" and removed.workspace == selection
    assert store.names() == ["staging"] and store.active() is None
    with pytest.raises(ProfileError) as failure:
        store.get("prod")
    assert code(failure) == "WV-PROFILE-NOT-FOUND"
    assert store.get("STAGING").name == "staging"


def test_permissions_are_tightened(store, tmp_path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    store.save(profile())
    store.path.chmod(0o644)
    directory.chmod(0o755)
    assert store.get("prod").name == "prod"
    assert stat_mode(directory) == 0o700 and stat_mode(store.path) == 0o600
    store.path.chmod(0o640)
    store.save(profile("staging"))
    assert stat_mode(store.path) == 0o600


def test_symlinks_and_hard_links_are_refused(store, tmp_path):
    store.save(profile())
    real = tmp_path / "elsewhere.json"
    real.write_bytes(store.path.read_bytes())
    real.chmod(0o600)
    original = store.path.read_bytes()
    store.path.unlink()
    store.path.symlink_to(real)
    for operation in (store.load, lambda: store.save(profile("staging"))):
        with pytest.raises(ProfileError) as failure:
            operation()
        assert code(failure) == "WV-PROFILE-STORE"
    assert real.read_bytes() == original
    store.path.unlink()
    os.link(real, store.path)
    with pytest.raises(ProfileError):
        store.load()
    store.path.unlink()
    target = tmp_path / "target-dir"
    target.mkdir(mode=0o700)
    linked = tmp_path / "linked-dir"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProfileError) as failure:
        ProfileStore(linked / "profiles.json").save(profile())
    assert code(failure) == "WV-PROFILE-STORE" and not (target / "profiles.json").exists()
    lock = store.path.parent / "profiles.json.lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to(tmp_path / "lock-target")
    with pytest.raises(ProfileError):
        store.save(profile("staging"))
    assert not (tmp_path / "lock-target").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"version": 2, "active": null, "profiles": {}}',
        b'{"version": 1, "active": null, "profiles": {}, "access_token": "x"}',
        b'{"version": 1, "active": "ghost", "profiles": {}}',
        b"\xff\xfe",
    ],
)
def test_corrupt_store_is_reported_and_never_overwritten(store, content):
    store.save(profile())
    store.path.write_bytes(content)
    with pytest.raises(ProfileError) as failure:
        store.load()
    assert code(failure) == "WV-PROFILE-STORE"
    with pytest.raises(ProfileError):
        store.save(profile("staging"))
    with pytest.raises(ProfileError):
        store.remove("prod")
    assert store.path.read_bytes() == content


@pytest.mark.parametrize(
    "field, value",
    [("issuer", "http://login.example/realms/acme"), ("target", "http://weave.example"), ("account", "other")],
)
def test_edited_login_settings_are_a_damaged_store_not_an_auth_failure(store, field, value):
    store.save(profile(), activate=True)
    document = json.loads(store.path.read_text())
    document["profiles"]["prod"]["login"][field] = value
    content = json.dumps(document).encode()
    store.path.write_bytes(content)
    for operation in (store.load, store.active, lambda: store.save(profile("staging")), lambda: store.remove("prod")):
        with pytest.raises(ProfileError) as failure:
            operation()
        assert code(failure) == "WV-PROFILE-STORE"
    assert store.path.read_bytes() == content


def test_size_and_count_bounds(store):
    store.save(profile())
    store.path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ProfileError) as failure:
        store.load()
    assert code(failure) == "WV-PROFILE-STORE"
    store.path.unlink()
    for index in range(64):
        store.save(profile(f"p{index}"))
    with pytest.raises(ProfileError) as failure:
        store.save(profile("one-too-many"))
    assert code(failure) == "WV-PROFILE-STORE" and len(store.names()) == 64
    assert store.save(profile("p0", display_name="Still replaceable"), replace=True).display_name


def test_failed_write_keeps_previous_document_and_no_temporary(store, monkeypatch):
    store.save(profile())
    before = store.path.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(profiles.os, "replace", broken)
    with pytest.raises(ProfileError) as failure:
        store.save(profile("staging"))
    assert code(failure) == "WV-PROFILE-STORE"
    monkeypatch.undo()
    assert store.path.read_bytes() == before
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["profiles.json", "profiles.json.lock"]


def test_store_path_must_be_absolute():
    with pytest.raises(ProfileError):
        ProfileStore(Path("relative/profiles.json"))


def test_concurrent_threads_never_lose_updates(store):
    names = [f"thread-{index}" for index in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda name: store.save(profile(name)), names))
    assert store.names() == sorted(names)


def test_concurrent_processes_never_lose_updates(store):
    script = """
import sys
from pathlib import Path
from firefly_weave.sdk.auth import LoginConfig
from firefly_weave.sdk.profiles import PlatformProfile, ProfileStore
store = ProfileStore(Path(sys.argv[1]))
for index in range(5):
    name = f"{sys.argv[2]}-{index}"
    login = LoginConfig(provider_id="acme", issuer="https://login.example", client_id="weave-cli",
                        target="https://weave.example", account=name, scopes=("openid",))
    store.save(PlatformProfile(name=name, login=login, source="manual"))
"""
    store.path.parent.mkdir(mode=0o700)
    environment = {**os.environ, "PYTHONPATH": str(SRC)}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store.path), f"process{index}"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(4)
    ]
    for child in children:
        output, error = child.communicate(timeout=60)
        assert child.returncode == 0 and not output, error.decode()
    assert len(store.names()) == 20


def test_lock_wait_is_bounded(store, monkeypatch):
    store.save(profile())
    held, release = threading.Event(), threading.Event()

    def holder():
        with store._transaction():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        monkeypatch.setattr(profiles, "LOCK_TIMEOUT", 0.1)
        with pytest.raises(ProfileError) as failure:
            store.save(profile("staging"))
        assert code(failure) == "WV-PROFILE-STORE"
    finally:
        release.set()
        thread.join(5)
    assert store.save(profile("staging")).name == "staging"


# --- Windows code path (simulated; no native Windows execution is claimed) ---------------------------------------


def windows_os():
    return SimpleNamespace(**{**{key: getattr(os, key) for key in dir(os) if not key.startswith("__")}, "name": "nt"})


@pytest.fixture
def fake_msvcrt(monkeypatch):
    calls, held = [], set()
    contention = {"remaining": 0}

    def locking(descriptor, mode, size):
        assert size == 1 and os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        calls.append(mode)
        if mode == module.LK_UNLCK:
            held.discard(descriptor)
            return
        if contention["remaining"] or held:
            contention["remaining"] = max(0, contention["remaining"] - 1)
            raise OSError(13, "locked")
        held.add(descriptor)

    module = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setitem(sys.modules, "msvcrt", module)
    monkeypatch.setattr(profiles, "os", windows_os())
    return module, calls, contention


def test_windows_store_locks_with_msvcrt_and_replaces_atomically(fake_msvcrt, tmp_path):
    module, calls, contention = fake_msvcrt
    store = ProfileStore(tmp_path / "Firefly Weave" / "profiles.json")
    contention["remaining"] = 2
    store.save(profile(), activate=True)
    assert calls == [module.LK_NBLCK, module.LK_NBLCK, module.LK_NBLCK, module.LK_UNLCK]
    assert store.active().name == "prod"
    assert json.loads(store.path.read_text())["active"] == "prod"
    store.remove("prod")
    assert store.names() == [] and calls[-1] == module.LK_UNLCK
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["profiles.json", "profiles.json.lock"]


def test_windows_store_refuses_symlinks(fake_msvcrt, tmp_path):
    store = ProfileStore(tmp_path / "Firefly Weave" / "profiles.json")
    store.save(profile())
    target = tmp_path / "target.json"
    target.write_bytes(store.path.read_bytes())
    store.path.unlink()
    store.path.symlink_to(target)
    with pytest.raises(ProfileError) as failure:
        store.load()
    assert code(failure) == "WV-PROFILE-STORE"
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "Firefly Weave", target_is_directory=True)
    with pytest.raises(ProfileError):
        ProfileStore(linked / "profiles.json").load()


def test_windows_replace_retries_while_a_reader_holds_the_file(fake_msvcrt, tmp_path):
    store = ProfileStore(tmp_path / "Firefly Weave" / "profiles.json")
    store.save(profile())
    attempts, original = [], profiles.os.replace

    def busy(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "in use")
        original(source, target)

    profiles.os.replace = busy
    store.save(profile("staging"))
    assert len(attempts) == 3 and store.names() == ["prod", "staging"]
    profiles.os.replace = lambda *_: (_ for _ in ()).throw(PermissionError(13, "in use"))
    with pytest.raises(ProfileError):
        store.save(profile("third"))
    profiles.os.replace = original
    assert store.names() == ["prod", "staging"]
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["profiles.json", "profiles.json.lock"]


def test_windows_lock_wait_is_bounded(fake_msvcrt, tmp_path, monkeypatch):
    _, _, contention = fake_msvcrt
    store = ProfileStore(tmp_path / "Firefly Weave" / "profiles.json")
    contention["remaining"] = 10_000
    monkeypatch.setattr(profiles, "LOCK_TIMEOUT", 0.05)
    with pytest.raises(ProfileError) as failure:
        store.save(profile())
    assert code(failure) == "WV-PROFILE-STORE"


# --- Studio preferences ------------------------------------------------------------------------------------------


def test_studio_preferences_live_privately_next_to_the_profiles(store, tmp_path):
    preferences = store.path.with_name("studio.json")
    assert store.load_preferences() == StudioPreferences() and store.load_preferences().start == "ask"
    assert not store.path.parent.exists(), "Reading preferences never creates the configuration folder"
    saved = store.save_preferences(StudioPreferences(start="local"))
    assert saved.start == "local" and store.load_preferences().start == "local"
    assert json.loads(preferences.read_text()) == {"version": 1, "start": "local"}
    assert stat_mode(preferences) == 0o600 and stat_mode(preferences.parent) == 0o700
    assert not store.path.exists(), "Preferences never create or change the saved platforms"
    preferences.chmod(0o644)
    store.save_preferences(StudioPreferences(start="ask"))
    assert stat_mode(preferences) == 0o600 and store.load_preferences().start == "ask"
    assert sorted(path.name for path in preferences.parent.iterdir()) == ["profiles.json.lock", "studio.json"]
    with pytest.raises(ValidationError):
        StudioPreferences(start="remote")


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"version": 2, "start": "ask"}',
        b'{"version": 1, "start": "remote"}',
        b'{"version": 1, "start": "local", "token": "x"}',
        b" " * (16 * 1024 + 1),
        b"\xff\xfe",
    ],
)
def test_damaged_preferences_are_reported_and_only_an_explicit_save_rewrites_them(store, content):
    preferences = store.path.with_name("studio.json")
    store.save_preferences(StudioPreferences(start="local"))
    preferences.write_bytes(content)
    for _ in range(2):
        with pytest.raises(ProfileError) as failure:
            store.load_preferences()
        assert code(failure) == "WV-PROFILE-STORE"
    store.save(profile())
    assert preferences.read_bytes() == content
    store.save_preferences(StudioPreferences(start="ask"))
    assert store.load_preferences().start == "ask"


def test_preference_links_are_refused_and_never_followed(store, tmp_path):
    store.save_preferences(StudioPreferences())
    preferences = store.path.with_name("studio.json")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"version": 1, "start": "local"}')
    elsewhere.chmod(0o600)
    preferences.unlink()
    preferences.symlink_to(elsewhere)
    with pytest.raises(ProfileError) as failure:
        store.load_preferences()
    assert code(failure) == "WV-PROFILE-STORE"
    # An explicit save replaces the link itself; the link target is never written.
    store.save_preferences(StudioPreferences(start="ask"))
    assert not preferences.is_symlink() and store.load_preferences().start == "ask"
    assert elsewhere.read_text() == '{"version": 1, "start": "local"}'


def test_windows_preferences_use_the_same_private_store(fake_msvcrt, tmp_path):
    store = ProfileStore(tmp_path / "Firefly Weave" / "profiles.json")
    assert store.load_preferences().start == "ask"
    store.save_preferences(StudioPreferences(start="local"))
    assert store.load_preferences().start == "local"
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["profiles.json.lock", "studio.json"]


# --- legacy Studio profiles --------------------------------------------------------------------------------------


def test_legacy_studio_profile_round_trip():
    from firefly_weave.studio.service import StudioProfile

    tenant, project, environment = uuid4(), uuid4(), uuid4()
    legacy = StudioProfile(
        name="My workspace",
        base_url="https://weave.example/",
        tenant_id=tenant,
        project_id=project,
        environment_id=environment,
        credential_store="file",
        credential_file=Path("/var/weave/tokens.json"),
    )
    converted = PlatformProfile.from_studio_profile(legacy, login("local"))
    assert converted.name == "My workspace" and converted.login.account == "My workspace"
    assert converted.source == "file" and converted.server == "https://weave.example"
    assert converted.workspace.environment_id == environment and converted.credential_store == "file"
    back = converted.to_studio_profile()
    assert isinstance(back, StudioProfile)
    assert (back.name, back.base_url, back.tenant_id, back.project_id, back.environment_id) == (
        "My workspace",
        "https://weave.example",
        tenant,
        project,
        environment,
    )
    assert back.credential_store == "file" and back.credential_file == Path("/var/weave/tokens.json")
    partial = StudioProfile(name="prod", base_url="https://weave.example", tenant_id=tenant)
    assert PlatformProfile.from_studio_profile(partial, login("prod"), source="manual").workspace is None
    with pytest.raises(ProfileError) as failure:
        PlatformProfile.from_studio_profile(StudioProfile(name="bad/name", base_url="https://weave.example"), login())
    assert code(failure) == "WV-PROFILE-NAME"
    with pytest.raises(ValueError):
        PlatformProfile.from_studio_profile(StudioProfile(name="prod", base_url="https://other.example"), login())
    assert profile().to_studio_profile().tenant_id is None


def test_profile_errors_carry_plain_messages():
    error = ProfileError("WV-PROFILE-NOT-FOUND")
    assert error.code == "WV-PROFILE-NOT-FOUND" and error.exit_code == 2 and error.message
    assert str(error) == "WV-PROFILE-NOT-FOUND"
    assert ProfileError("WV-PROFILE-STORE", "Custom detail").message == "Custom detail"


def stat_mode(path):
    return path.stat().st_mode & 0o777
