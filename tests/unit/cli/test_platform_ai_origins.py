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

"""AI entries join the installation's private-origin file with recorded consent, and leave it on disable."""

import hashlib
import json
import stat

import pytest
from ai_platform_support import OWNER, SUBNET, owned_fixture  # noqa: F401

from firefly_weave import private_origins as po
from firefly_weave.cli.platform import _show_private_origins
from firefly_weave.sdk import platform, platform_origins

ACME = "http://acme.acceptance.test:8080"
LOOPBACK = ("127.0.0.1/32",)
ENTRIES = [
    po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=(SUBNET,), credentials="none"),
    po.PrivateOrigin(origin="http://127.0.0.1:8090", purpose="model", networks=LOOPBACK, credentials="loopback"),
    po.PrivateOrigin(origin="http://127.0.0.1:8080", purpose="worker-auth", networks=LOOPBACK, credentials="loopback"),
    po.PrivateOrigin(origin="http://127.0.0.1:8000", purpose="platform-api", networks=LOOPBACK, credentials="loopback"),
]


def pairs(directory):
    return {
        (entry.origin, entry.purpose) for entry in po.parse((directory / "private-origins.json").read_bytes()).entries
    }


def test_ai_entries_are_recorded_with_consent_and_the_file_digest(owned):
    directory, _, _ = owned
    state = platform._load(directory)
    assert platform_origins.set_ai_entries(state, ENTRIES) is True
    saved = json.loads((directory / "platform.json").read_text())["private_origins"]
    assert set(saved) == {"ai", "file_sha256"} and saved["ai"]["consent"] == "weave platform ai enable"
    path = directory / "private-origins.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == saved["file_sha256"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    policy = po.parse(path.read_bytes())
    assert policy.match("model", "http://ollama:11434/v1").credentials == "none"
    reloaded = platform._load(directory)
    assert platform_origins.set_ai_entries(reloaded, ENTRIES) is False
    assert not platform_origins.has_egress(reloaded)


def test_ai_entries_join_and_leave_the_connector_test_origins(owned):
    directory, _, runner = owned
    runner.answers["egress-inspect"] = lambda command: json.dumps(
        [
            {
                "Name": command[-1],
                "Labels": {"io.getfirefly.weave.installation": OWNER},
                "IPAM": {"Config": [{"Subnet": "10.246.22.0/24"}]},
            }
        ]
    ).encode()
    platform_origins.prepare(platform._load(directory), (ACME,))
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    fixtures = {(ACME, "event-delivery"), (ACME, "http-connector")}
    assert pairs(directory) == fixtures | {(entry.origin, entry.purpose) for entry in ENTRIES}
    assert platform_origins.has_egress(platform._load(directory))
    assert platform_origins.remove_ai_entries(platform._load(directory)) is True
    assert pairs(directory) == fixtures


def test_removing_the_only_ai_entries_removes_the_file(owned):
    directory, _, _ = owned
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    assert platform_origins.remove_ai_entries(platform._load(directory)) is True
    assert "private_origins" not in json.loads((directory / "platform.json").read_text())
    assert not (directory / "private-origins.json").exists()
    assert platform_origins.remove_ai_entries(platform._load(directory)) is False


def test_only_ai_purposes_are_accepted(owned):
    directory, _, _ = owned
    connector = po.PrivateOrigin(origin=ACME, purpose="http-connector", networks=(SUBNET,), credentials="bridge")
    with pytest.raises(ValueError):
        platform_origins.set_ai_entries(platform._load(directory), [connector])


def test_empty_or_duplicated_ai_entries_are_refused(owned):
    directory, _, _ = owned
    before = (directory / "platform.json").read_bytes()
    # Recording no entries would leave metadata every later command refuses; disable removes them instead.
    for entries in ([], [ENTRIES[0], ENTRIES[0]]):
        with pytest.raises(ValueError):
            platform_origins.set_ai_entries(platform._load(directory), entries)
    assert (directory / "platform.json").read_bytes() == before
    assert not (directory / "private-origins.json").exists()
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    saved = json.loads((directory / "platform.json").read_text())
    # Each record twice keeps the canonical order, so only the one-entry-per-origin-and-purpose rule refuses it.
    records = saved["private_origins"]["ai"]["entries"]
    saved["private_origins"]["ai"]["entries"] = [record for record in records for _ in range(2)]
    platform._write(directory / "platform.json", saved, replace=True)
    with pytest.raises(platform.PlatformError, match="metadata is invalid"):
        platform._load(directory)


def test_tampered_ai_consent_stops_every_command(owned):
    directory, _, _ = owned
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    saved = json.loads((directory / "platform.json").read_text())
    saved["private_origins"]["ai"]["entries"][0]["networks"] = ["10.0.0.0/16"]
    platform._write(directory / "platform.json", saved, replace=True)
    # The recorded entries must render to exactly the file on disk, so widening them in platform.json fails too.
    with pytest.raises(platform.PlatformError, match="changed outside platform commands"):
        platform._load(directory)
    saved["private_origins"]["ai"]["consent"] = "someone else"
    platform._write(directory / "platform.json", saved, replace=True)
    with pytest.raises(platform.PlatformError, match="metadata is invalid"):
        platform._load(directory)


def test_status_lists_ai_entries_without_an_egress_network(owned, capsys):
    directory, _, _ = owned
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    summary = platform_origins.summary(platform._load(directory))
    assert "network" not in summary and len(summary["entries"]) == 4
    _show_private_origins(summary)
    output = capsys.readouterr().out
    assert "Private origins (Development only): http://127.0.0.1:8000" in output
    assert "Egress network" not in output


PENDING = ".private-origins.pending.json"


def connector_fixture(directory, runner):
    runner.answers["egress-inspect"] = lambda command: json.dumps(
        [
            {
                "Name": command[-1],
                "Labels": {"io.getfirefly.weave.installation": OWNER},
                "IPAM": {"Config": [{"Subnet": "10.246.22.0/24"}]},
            }
        ]
    ).encode()
    platform_origins.prepare(platform._load(directory), (ACME,))


@pytest.mark.parametrize("operation", ["add", "add-keep", "update", "update-keep", "remove-only", "remove-keep"])
@pytest.mark.parametrize("boundary", ["journal", "state", "policy", "cleanup"])
def test_interrupted_origin_pair_recovers_every_durable_boundary(owned, monkeypatch, operation, boundary):
    from pathlib import Path

    directory, _, runner = owned
    if operation.endswith("keep"):
        connector_fixture(directory, runner)
    if not operation.startswith("add"):
        platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    entries = [
        e.model_copy(update={"networks": ("10.246.23.0/24",)}) if e.origin == "http://ollama:11434" else e
        for e in ENTRIES
    ]
    expected = {(e.origin, e.purpose) for e in entries} if not operation.startswith("remove") else set()
    if operation.endswith("keep"):
        expected |= {(ACME, "event-delivery"), (ACME, "http-connector")}
    sync, replace, unlink = platform._sync, platform_origins._replace, Path.unlink

    def interrupted_sync(path):
        sync(path)
        if path.name == (PENDING if boundary == "journal" else "platform.json") and boundary in {"journal", "state"}:
            raise OSError("interrupted durable publication")

    def interrupted_replace(path, data):
        replace(path, data)
        if boundary == "policy" and path.name == platform_origins.FILE:
            raise OSError("interrupted policy publication")

    def interrupted_unlink(path, *args, **kwargs):
        if boundary == "cleanup" and path.name == PENDING:
            raise OSError("interrupted transition cleanup")
        result = unlink(path, *args, **kwargs)
        if boundary == "policy" and path.name == platform_origins.FILE:
            raise OSError("interrupted policy removal")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(platform, "_sync", interrupted_sync)
        patch.setattr(platform_origins, "_replace", interrupted_replace)
        patch.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(OSError, match="interrupted"):
            state = platform._load(directory)
            if operation.startswith("remove"):
                platform_origins.remove_ai_entries(state)
            else:
                platform_origins.set_ai_entries(state, entries)
    assert (directory / PENDING).is_file()
    with platform._lock(directory):
        state = platform._load(directory)
    assert not (directory / PENDING).exists()
    if expected:
        assert pairs(directory) == expected
        if operation.startswith("update"):
            assert po.parse((directory / platform_origins.FILE).read_bytes()).match(
                "model", "http://ollama:11434"
            ).networks == ("10.246.23.0/24",)
    else:
        assert "private_origins" not in state and not (directory / platform_origins.FILE).exists()


def pending_transition(directory):
    before = platform._load(directory).get("private_origins")
    state = platform._load(directory)
    platform_origins.set_ai_entries(state, ENTRIES)
    after = state["private_origins"]
    platform._write(directory / PENDING, {"format": 1, "installation": OWNER, "before": before, "after": after})
    return before, after


@pytest.mark.parametrize("tamper", ["foreign", "extra", "digest", "mode", "symlink", "policy", "metadata"])
def test_pending_transition_rejects_unrecognized_or_unsafe_state(owned, tamper):
    directory, _, _ = owned
    _, after = pending_transition(directory)
    path = directory / PENDING
    pending = json.loads(path.read_text())
    if tamper == "foreign":
        pending["installation"] = "e" * 24
    elif tamper == "extra":
        pending["extra"] = True
    elif tamper == "digest":
        pending["after"]["file_sha256"] = "0" * 64
    elif tamper == "mode":
        path.chmod(0o644)
    elif tamper == "symlink":
        target = directory / "foreign-pending"
        path.rename(target)
        path.symlink_to(target)
    elif tamper == "policy":
        (directory / platform_origins.FILE).write_bytes(platform_origins._render(after) + b"\n")
    elif tamper == "metadata":
        state = json.loads((directory / "platform.json").read_text())
        state["private_origins"]["ai"]["consented_at"] = "different"
        platform._write(directory / "platform.json", state, replace=True)
    if tamper in {"foreign", "extra", "digest"}:
        platform._write(path, pending, replace=True)
    original = (directory / "platform.json").read_bytes(), (directory / platform_origins.FILE).read_bytes()
    with pytest.raises(platform.PlatformError):
        platform._load(directory)
    assert original == ((directory / "platform.json").read_bytes(), (directory / platform_origins.FILE).read_bytes())
    assert path.exists()


@pytest.mark.asyncio
async def test_pending_recovery_lock_ownership_does_not_leak_to_tasks_or_threads(owned):
    import asyncio

    directory, _, _ = owned
    pending_transition(directory)

    async def child():
        with pytest.raises(platform.PlatformError, match="Another platform command"):
            platform._load(directory)

    with platform._lock(directory):
        await asyncio.create_task(child())

        def thread():
            with pytest.raises(platform.PlatformError, match="Another platform command"):
                platform._load(directory)

        await asyncio.to_thread(thread)
        platform._load(directory)
    assert not (directory / PENDING).exists()


def test_pending_transition_cannot_change_retained_connector_consent(owned):
    directory, _, runner = owned
    connector_fixture(directory, runner)
    before = platform._load(directory)["private_origins"]
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    after = platform._load(directory)["private_origins"]
    after["origins"] = ["http://foreign.acceptance.test:8080"]
    data = platform_origins._render(after)
    after["file_sha256"] = hashlib.sha256(data).hexdigest()
    platform._write(directory / PENDING, {"format": 1, "installation": OWNER, "before": before, "after": after})
    state = platform._state(directory)
    state["private_origins"] = before
    platform._write(directory / "platform.json", state, replace=True)
    platform_origins._replace(directory / platform_origins.FILE, platform_origins._render(before))
    original = (directory / "platform.json").read_bytes()
    with pytest.raises(platform.PlatformError):
        platform._load(directory)
    assert (directory / "platform.json").read_bytes() == original
    assert (directory / PENDING).exists()


def test_recovery_revalidates_metadata_after_acquiring_actual_lock(owned, monkeypatch):
    from contextlib import contextmanager

    directory, _, _ = owned
    pending_transition(directory)
    original = platform._lock
    changed = None

    @contextmanager
    def lock(path):
        nonlocal changed
        with original(path):
            state = platform._state(path)
            state["private_origins"]["ai"]["consented_at"] = "concurrent-unrecognized-state"
            platform._write(path / "platform.json", state, replace=True)
            changed = (path / "platform.json").read_bytes()
            yield

    monkeypatch.setattr(platform, "_lock", lock)
    with pytest.raises(platform.PlatformError, match="unrecognized"):
        platform._load(directory)
    assert (directory / "platform.json").read_bytes() == changed
    assert (directory / PENDING).exists()
