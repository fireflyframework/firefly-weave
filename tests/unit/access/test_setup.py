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

"""Local identity setup protects generated secrets and the realm's audience policy."""

import json
import subprocess
import sys
from pathlib import Path


def test_local_setup_generates_owner_only_secrets_and_refuses_overwrite(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts/setup-identity.py"
    assert script.exists(), "Explicit Keycloak setup is absent"
    destination = tmp_path / ".env.identity"
    command = [sys.executable, str(script), "--output", str(destination)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in destination.read_text().splitlines())
    assert destination.stat().st_mode & 0o777 == 0o600
    for key in ["WEAVE_KC_DB_PASSWORD", "WEAVE_KC_ADMIN_SECRET", "WEAVE_HOST_SECRET", "WEAVE_WORKER_SECRET"]:
        assert len(values[key]) >= 43 and values[key] not in result.stdout + result.stderr
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_realm_template_flow_and_audience_policy():
    path = Path(__file__).resolve().parents[3] / "infra/keycloak/weave-realm.json"
    assert path.exists(), "Pinned realm template is absent"
    realm = json.loads(path.read_text())
    clients = {client["clientId"]: client for client in realm["clients"]}
    for name in ["weave-host", "weave-worker"]:
        client = clients[name]
        assert client["serviceAccountsEnabled"] and not client["directAccessGrantsEnabled"]
        assert not client["standardFlowEnabled"] and not client["implicitFlowEnabled"]
        assert client["secret"].startswith("${")
        audience = next(m for m in client["protocolMappers"] if m["protocolMapper"] == "oidc-audience-mapper")
        assert audience["config"]["included.client.audience"] == "weave-api"
        assert audience["config"]["id.token.claim"] == "false"
    assert clients["weave-cli"]["attributes"]["pkce.code.challenge.method"] == "S256"


def test_postgres_setup_supports_new_isolated_destination(tmp_path):
    import os

    script = Path(__file__).resolve().parents[3] / "scripts/setup-local.py"
    destination = tmp_path / ".env.weave-isolated"
    command = [sys.executable, str(script), "--output", str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "WEAVE_POSTGRES_PORT": "55433"})
    assert result.returncode == 0, result.stderr
    assert destination.stat().st_mode & 0o777 == 0o600
    values = dict(line.split("=", 1) for line in destination.read_text().splitlines())
    assert ":55433/weave_b1_control" in values["WEAVE_TEST_DATABASE_URL"]
    assert values["WEAVE_POSTGRES_PASSWORD"] not in result.stdout + result.stderr
    original = destination.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert destination.read_bytes() == original
