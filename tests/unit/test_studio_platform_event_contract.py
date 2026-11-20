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


"""Studio reads the platform event, the input of a run a platform event starts, from its host."""

from pathlib import Path

from starlette.testclient import TestClient

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.platform_catalog import platform_schema_digests
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
ROUTE = "/studio/contracts/platform-event"


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def test_the_platform_event_contract_is_the_platforms_frozen_view(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        assert browser.get(ROUTE).status_code == 401
        paired = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
        assert paired.status_code == 200
        response = browser.get(ROUTE)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert FrozenDocument.from_value(response.json()).digest == platform_schema_digests()[PLATFORM_EVENT_SCHEMA]
        assert response.headers["cache-control"] == "no-store"
