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

"""Plain HTTP for local operations stays exact: development-only entries limited to the installation's network."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.cli.platform import _show_private_origins
from firefly_weave.deployment_runner.application import RunnerConfiguration
from firefly_weave.sdk import platform_operations

ROOT = Path(__file__).resolve().parents[2]
SUBNET = "10.246.71.0/24"


def policy() -> po.PrivateOrigins:
    entries = platform_operations.entries({}, SUBNET)
    return po.parse(po.render(po.PrivateOrigins(platform=po.PLATFORM).with_entries(entries)))


def test_operations_entries_are_development_only_and_never_widen():
    rendered = policy()
    assert {(entry.origin, entry.credentials, entry.networks) for entry in rendered.entries} == {
        ("http://keycloak:8000", "bridge", (SUBNET,)),
        ("http://keycloak:8080", "bridge", (SUBNET,)),
        ("http://127.0.0.1:8000", "loopback", ("127.0.0.1/32",)),
        ("http://127.0.0.1:8080", "loopback", ("127.0.0.1/32",)),
    }
    assert all(entry.label == po.DEVELOPMENT_ONLY for entry in rendered.entries)
    allowed = rendered.check("platform-api", "http://keycloak:8000/api/v1", ["10.246.71.3"], sends_credentials=True)
    assert allowed is not None and allowed.origin == "http://keycloak:8000"
    # The same name resolving into another network, another port, or HTTPS changes nothing they allow.
    for url, address in (
        ("http://keycloak:8000/api/v1", "10.246.99.3"),
        ("http://keycloak:8001/api/v1", "10.246.71.3"),
        ("http://other:8000/api/v1", "10.246.71.3"),
    ):
        with pytest.raises(po.PrivateOriginDenied):
            rendered.check("platform-api", url, [address], sends_credentials=True)


def test_no_operations_component_reads_the_local_development_flag():
    for path in (
        ROOT / "src/firefly_weave/sdk/platform_operations.py",
        ROOT / "tests/fixtures/operations-worker/main.py",
        ROOT / "src/firefly_weave/deployment_runner/application.py",
    ):
        assert "local_development" not in path.read_text(), path
    # The runner's configuration names no identity provider at all, so the flag cannot reach it.
    state = {"id": "a" * 24, "engine": "engine-id", "directory": "/unused"}
    value = {
        "target_id": str(uuid4()),
        "registry_port": 5000,
        "scope": {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())},
    }
    configuration = RunnerConfiguration.model_validate_json(
        json.dumps(platform_operations.runner_configuration(state, value))
    )
    assert configuration.base_url == "http://127.0.0.1:8000" and configuration.oauth_file
    assert "local_development" not in configuration.model_dump_json()


def test_cli_status_labels_the_operations_entries_development_only(capsys):
    rendered = policy()
    _show_private_origins(
        {
            "label": po.DEVELOPMENT_ONLY,
            "entries": [{"origin": entry.origin, "purpose": entry.purpose} for entry in rendered.entries],
        }
    )
    printed = capsys.readouterr().out
    assert printed.startswith("Private origins (Development only): ")
    assert "http://keycloak:8000" in printed and "http://127.0.0.1:8080" in printed
