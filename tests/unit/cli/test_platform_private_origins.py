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

"""weave platform up --allow-private-origin: consent, egress network and the private-origin file (C8)."""

import hashlib
import json
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave import __version__
from firefly_weave import private_origins as po
from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform, platform_docker, platform_origins

ACME = "http://acme.acceptance.test:8080"
EGRESS = "10.231.1.0/24"
OWNER = "b" * 24


@pytest.fixture
def owned(tmp_path, monkeypatch):
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    state = {
        "format": platform._FORMAT,
        "id": OWNER,
        "directory": str(directory),
        "source": str(tmp_path),
        "source_sha256": "fingerprint",
        "version": __version__,
        "stage": "ready",
        "mode": "docker",
        "context": "owned",
        "endpoint": "unix:///owned.sock",
        "engine": "owned-engine",
        "subnet": "10.231.0.0/24",
        "ports": {"postgres": 55101, "keycloak": 55102, "api": 55103, "container_api": 55104},
    }
    platform._write(directory / "platform.json", state)
    override = directory / "compose.network.yaml"
    override.write_bytes(platform._network_override(state))
    override.chmod(0o600)
    issuer = "http://localhost:55102/realms/weave"
    runtime = {
        "WEAVE_DATABASE_URL": "postgresql+asyncpg://weave_runtime_t:app@localhost:55101/weave_b2_dev_t",
        "WEAVE_SCHEDULER_DATABASE_URL": "postgresql+asyncpg://weave_scheduler_t:sch@localhost:55101/weave_b2_dev_t",
        "WEAVE_OIDC_PROVIDERS": json.dumps(
            [
                {
                    "provider_id": "local-keycloak",
                    "issuer": issuer,
                    "jwks_uri": issuer + "/protocol/openid-connect/certs",
                    "local_development": True,
                    "audience": "weave-api",
                    "clients": {"weave-cli": "human"},
                }
            ]
        ),
    }
    for name, values in {
        "runtime.env": runtime,
        "identity.env": {"WEAVE_HOST_SECRET": "host-secret", "WEAVE_KC_ADMIN_SECRET": "admin-secret"},
        "postgres.env": {"WEAVE_POSTGRES_PASSWORD": "owner-secret"},
    }.items():
        path = directory / name
        path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()))
        path.chmod(0o600)
    monkeypatch.setattr(platform, "_source", lambda p: (p, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda c: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda s: None)
    return directory, state


@pytest.fixture
def docker(monkeypatch):
    calls = []

    def run(state, name, command, **kwargs):
        calls.append((name, command))
        if name == "egress-inspect":
            record = {
                "Name": command[-1],
                "Labels": {"io.getfirefly.weave.installation": state["id"]},
                "IPAM": {"Config": [{"Subnet": EGRESS}]},
            }
            return json.dumps([record]).encode()
        return b""

    monkeypatch.setattr(platform, "_run", run)
    monkeypatch.setattr(platform, "_check_subnet", lambda context, value: value)
    return calls


@pytest.mark.parametrize(
    ("value", "expected"),
    [("HTTP://Acme.Acceptance.Test:8080/", ACME), ("http://acme.acceptance.test", "http://acme.acceptance.test:80")],
)
def test_fixture_origins_are_canonical(value, expected):
    assert platform_origins.fixture_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://acme.acceptance.test:8443",
        "smtp://mail.acceptance.test",
        "acme.acceptance.test:8080",
        "http://10.0.0.5:8080",
        "http://[fd00::5]:8080",
        "http://localhost:8080",
        "http://keycloak:8080",
        "http://acme:8080",
        "http://api.localhost:8080",
        "http://acme.acceptance.test/orders",
        "http://user@acme.acceptance.test",
    ],
)
def test_other_origins_are_refused(value):
    with pytest.raises(platform.PlatformError):
        platform_origins.fixture_origin(value)


@pytest.mark.parametrize(
    "value", ["http://0xa.0xe7.1.1:8080", "http://0x7f.1:8080", "http://acme.1:8080", "http://metadata.google.internal"]
)
def test_numeric_and_metadata_host_names_are_refused(value):
    # Resolvers that accept inet_aton forms read a name ending in a number as an address,
    # such as the egress gateway (0xa.0xe7.1.1) or loopback (0x7f.1).
    with pytest.raises(platform.PlatformError, match="dotted DNS name"):
        platform_origins.fixture_origin(value)


def test_requested_origins_are_unique_and_bounded():
    assert platform_origins.requested([ACME, "HTTP://ACME.acceptance.test:8080/"]) == (ACME,)
    with pytest.raises(platform.PlatformError, match="at most"):
        platform_origins.requested([f"http://f{n}.acceptance.test:8080" for n in range(17)])


def test_the_egress_subnet_follows_the_installation_subnet():
    assert platform_origins.egress_subnet({"subnet": "10.231.0.0/24"}) == EGRESS
    assert platform_origins.egress_subnet({"subnet": None}) is None
    for subnet in ("192.168.255.0/24", "10.255.255.0/24"):
        with pytest.raises(platform.PlatformError):
            platform_origins.egress_subnet({"subnet": subnet})


def test_prepare_creates_the_network_and_writes_one_entry_per_purpose(owned, docker):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    (create_stage, create), (inspect_stage, _) = docker
    assert (create_stage, inspect_stage) == ("egress-network", "egress-inspect")
    assert create[-1] == f"weave-local-{OWNER}-egress"
    assert create[create.index("--subnet") + 1] == EGRESS
    assert f"io.getfirefly.weave.installation={OWNER}" in create
    path = directory / "private-origins.json"
    data = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(data) == {
        "format": po.FORMAT,
        "platform": po.PLATFORM,
        "entries": [
            {"origin": ACME, "purpose": "event-delivery", "networks": [EGRESS], "credentials": "bridge"},
            {"origin": ACME, "purpose": "http-connector", "networks": [EGRESS], "credentials": "bridge"},
        ],
    }
    saved = json.loads((directory / "platform.json").read_text())["private_origins"]
    assert saved["origins"] == [ACME] and saved["subnet"] == EGRESS
    assert saved["network"] == f"weave-local-{OWNER}-egress" and saved["consent"] == "--allow-private-origin"
    assert saved["file_sha256"] == hashlib.sha256(data).hexdigest()
    assert "external: true" in (directory / "compose.egress.yaml").read_text()


def test_consent_is_recorded_before_the_private_origin_file_exists(owned, docker, monkeypatch):
    directory, state = owned
    created = []
    original = platform_origins._create

    def create(path, data):
        # C8: platform.json records consent and the exact digest before any entry exists on disk.
        saved = json.loads((directory / "platform.json").read_text()).get("private_origins")
        assert saved is not None and saved["consent"] == "--allow-private-origin"
        if path.name == platform_origins.FILE:
            assert saved["file_sha256"] == hashlib.sha256(data).hexdigest()
        created.append(path.name)
        original(path, data)

    monkeypatch.setattr(platform_origins, "_create", create)
    platform_origins.prepare(state, (ACME,))
    assert created == [platform_origins.FILE, platform_origins.COMPOSE]


def test_keycloak_and_the_api_join_the_egress_network(owned, docker):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    assert str(directory / "compose.egress.yaml") in platform._compose(state)


def test_a_changed_private_origin_file_stops_every_command(owned, docker):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    path = directory / "private-origins.json"
    path.write_text(path.read_text().replace(EGRESS, "10.231.9.0/24"))
    with pytest.raises(platform.PlatformError, match="changed outside"):
        platform._compose(state)


def _edited(path, state):
    path.write_text(path.read_text().replace(EGRESS, "10.231.9.0/24"))


def _loosened(path, state):
    path.chmod(0o644)


def _linked(path, state):
    copy = path.with_name("elsewhere.json")
    copy.write_bytes(path.read_bytes())
    copy.chmod(0o600)
    path.unlink()
    path.symlink_to(copy)


def _unknown_keys(path, state):
    document = json.loads(path.read_bytes())
    document["entries"][0]["widen"] = True
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    # Even a matching digest in platform.json cannot make a document outside the format usable.
    state["private_origins"]["file_sha256"] = hashlib.sha256(data).hexdigest()
    platform._write(path.parent / "platform.json", state, replace=True)


def _missing(path, state):
    path.unlink()


@pytest.mark.parametrize("tamper", [_edited, _loosened, _linked, _unknown_keys, _missing])
def test_any_tampering_with_the_private_origin_file_stops_every_command(owned, docker, tamper):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    tamper(directory / "private-origins.json", state)
    # Every platform command loads the installation first, so none of them runs on a changed file.
    with pytest.raises(platform.PlatformError, match="private-origin file"):
        platform._load(directory)
    with pytest.raises(platform.PlatformError, match="private-origin file"):
        platform._compose(state)
    result = CliRunner().invoke(cli, ["platform", "--directory", str(directory), "token"])
    assert result.exit_code != 0 and "private-origin file" in result.output


def test_tampered_consent_metadata_is_refused(owned, docker):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    state["private_origins"]["network"] = "someone-elses-network"
    platform._write(directory / "platform.json", state, replace=True)
    with pytest.raises(platform.PlatformError, match="private-origin metadata"):
        platform._load(directory)


def test_the_api_reads_a_read_only_copy_of_the_file(owned, docker):
    directory, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    platform_origins.prepare(state, (ACME,))
    config = json.loads(platform_docker._configuration(state, {}).read_text())["services"]["api"]
    (mount,) = config["volumes"]
    assert mount == {
        "type": "bind",
        "source": mount["source"],
        "target": "/run/weave-config/private-origins.json",
        "read_only": True,
    }
    copy = Path(mount["source"])
    assert copy.read_bytes() == (directory / "private-origins.json").read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o444
    env = dict(line.split("=", 1) for line in (directory / "api-container.env").read_text().splitlines())
    assert env["WEAVE_PRIVATE_ORIGINS_FILE"] == "/run/weave-config/private-origins.json"
    loaded = po.load({"WEAVE_PRIVATE_ORIGINS_FILE": str(copy)})
    assert [(e.origin, e.purpose) for e in loaded.entries] == [(ACME, "event-delivery"), (ACME, "http-connector")]


def test_up_keeps_private_origins_fixed_after_creation(owned, docker, monkeypatch):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    monkeypatch.setattr(platform_docker, "start", lambda s, n: None)
    monkeypatch.setattr(platform, "demo", lambda d: {"existing": True, "receipt": {"status": "succeeded"}})
    result = platform.up(
        directory, Path("."), None, subnet="10.231.0.0/24", private_origins=("HTTP://ACME.acceptance.test:8080/",)
    )
    summary = result["private_origins"]
    assert summary["label"] == "Development only" and summary["subnet"] == EGRESS
    assert {entry["origin"] for entry in summary["entries"]} == {ACME}
    with pytest.raises(platform.PlatformError, match="fixed when an installation is created"):
        platform.up(directory, Path("."), None, private_origins=("http://other.acceptance.test:8080",))
    assert platform.up(directory, Path("."), None)["private_origins"]["network"] == f"weave-local-{OWNER}-egress"


def test_a_new_up_creates_private_origins_during_setup(tmp_path, monkeypatch):
    seen = {}

    def setup(directory, source, context, **kwargs):
        seen.update(kwargs)
        raise platform.PlatformError("stop here")

    monkeypatch.setattr(platform, "setup", setup)
    with pytest.raises(platform.PlatformError, match="stop here"):
        platform.up(tmp_path / "new", tmp_path, "owned", private_origins=("http://acme.acceptance.test:8080/",))
    assert seen["private_origins"] == (ACME,) and seen["mode"] == "docker"


def test_host_mode_setup_refuses_private_origins(tmp_path):
    with pytest.raises(platform.PlatformError, match="Docker platform"):
        platform.setup(tmp_path / "host", tmp_path, "owned", private_origins=(ACME,))


def test_the_cli_passes_each_private_origin(monkeypatch):
    calls = []

    def up(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "mode": "docker", "api_url": "http://127.0.0.1:1"}

    monkeypatch.setattr(platform, "up", up)
    result = CliRunner().invoke(
        cli,
        [
            "platform",
            "up",
            "--allow-private-origin",
            ACME,
            "--allow-private-origin",
            "http://b.acceptance.test:8081",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["private_origins"] == (ACME, "http://b.acceptance.test:8081")


def test_status_lists_private_origins_as_development_only(owned, docker):
    directory, state = owned
    platform_origins.prepare(state, (ACME,))
    text = CliRunner().invoke(cli, ["platform", "--directory", str(directory), "status"])
    assert text.exit_code == 0, text.output
    assert f"Private origins (Development only): {ACME}" in text.output
    assert f"Egress network: weave-local-{OWNER}-egress ({EGRESS})" in text.output
    value = json.loads(
        CliRunner().invoke(cli, ["platform", "--directory", str(directory), "status", "--output", "json"]).output
    )
    assert value["private_origins"]["subnet"] == EGRESS
