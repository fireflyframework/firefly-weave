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
