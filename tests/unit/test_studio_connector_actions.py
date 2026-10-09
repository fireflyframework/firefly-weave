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

"""The Studio host lists the built-in connector actions a workflow can own, as valid Action documents."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from firefly_weave.contracts.connector_actions import builtin_connector_actions
from firefly_weave.contracts.definitions import ActionDefinition
from firefly_weave.contracts.file_connectors import NAMES, OPERATIONS
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def test_lists_email_and_every_file_operation_as_valid_actions():
    actions = builtin_connector_actions()
    keys = [(entry["connector"], entry["action"]) for entry in actions]
    assert keys[:2] == [("weave-email@1.0.0", "send"), ("weave-email@1.0.0", "reply")]
    assert keys[2:] == [(f"{name}@1.0.0", operation) for name in NAMES for operation in OPERATIONS]
    for entry in actions:
        document = ActionDefinition.model_validate(entry["document"])
        assert document.spec.connection is not None
        assert document.spec.connection.connector == entry["connector"]
    send = actions[0]["document"]["spec"]
    assert send["sideEffect"] == "non_idempotent"
    assert send["inputSchema"]["required"] == ["to", "subject", "text"]


def test_host_serves_connector_actions_only_to_a_paired_session(tmp_path):
    with client(tmp_path) as browser:
        assert browser.get("/studio/contracts/connector-actions").status_code == 401
        assert browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN}).status_code == 200
        response = browser.get("/studio/contracts/connector-actions")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"actions": builtin_connector_actions()}
