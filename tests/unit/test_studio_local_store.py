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

"""The desktop store keeps Studio's keys in private files, refuses links, stays bounded and forgets a platform."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from firefly_weave.studio import local_store
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.local_store import LocalStore, StoreFull, StoreUnavailable, file_name, profile_prefix
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and descriptor-relative links")
ACCOUNT = (
    profile_prefix("acme")
    + "0123456789abcdef:7b0c1c43-3f5a-4cde-9f0e-3c2d5a6b7c8d:9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718:"
)


def client(tmp_path: Path, store: Path | None) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    options = StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE, store_directory=store)
    return TestClient(make_studio_app(options), base_url=ORIGIN)


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


def test_the_prefix_is_the_one_studio_computes():
    # studio/src/app/editor/state/browser-store.ts profileId: "p" + the first 16 hex digits of SHA-256(name).
    assert profile_prefix("acme") == "p" + hashlib.sha256(b"acme").hexdigest()[:16] + ":"


def test_values_round_trip_and_list_by_prefix(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    assert store.keys() == [] and store.get("local:weave.localDrafts.v1") is None
    store.put("local:weave.localDrafts.v1", '[{"id":"d-1"}]')
    store.put("local:weave.localDraft.d-1.testData", '{"schemaVersion":1}')
    store.put(ACCOUNT + "testData.d-2", "{}")
    store.put("ui:weave.ndv.views.v1", "{}")
    assert store.get("local:weave.localDrafts.v1") == '[{"id":"d-1"}]'
    assert store.keys("local:") == ["local:weave.localDraft.d-1.testData", "local:weave.localDrafts.v1"]
    store.put("local:weave.localDrafts.v1", "[]")
    assert store.get("local:weave.localDrafts.v1") == "[]"
    assert store.delete("local:weave.localDrafts.v1") is True
    assert store.delete("local:weave.localDrafts.v1") is False
    assert len(store.keys()) == 3


def test_a_platform_s_keys_go_when_it_is_forgotten(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    store.put(ACCOUNT + "testData.d-2", "{}")
    store.put(ACCOUNT + "canvas.d-2", "{}")
    store.put(profile_prefix("other") + "x:y:z:canvas.d-3", "{}")
    store.put("local:weave.localDraft.d-1", "{}")
    assert store.purge(profile_prefix("acme")) == 2
    assert store.keys() == ["local:weave.localDraft.d-1", profile_prefix("other") + "x:y:z:canvas.d-3"]
    with pytest.raises(ValueError):
        store.purge("")


@POSIX
def test_files_are_private_and_named_by_hash(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    store.put("local:a", "1")
    folder = tmp_path / "studio-store"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    path = folder / file_name("local:a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"key": "local:a", "value": "1"}
    assert ":" not in path.name


@POSIX
def test_links_are_refused(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    store.put("local:a", "1")
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"key": "local:b", "value": "stolen"}))
    os.symlink(target, tmp_path / "studio-store" / file_name("local:b"))
    with pytest.raises(StoreUnavailable):
        store.get("local:b")
    os.unlink(tmp_path / "studio-store" / file_name("local:b"))
    os.link(tmp_path / "studio-store" / file_name("local:a"), tmp_path / "hard.json")
    with pytest.raises(StoreUnavailable):
        store.get("local:a")


def test_the_store_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "STORE_LIMIT", 1024)
    store = LocalStore(tmp_path / "studio-store")
    store.put("local:a", "x" * 600)
    with pytest.raises(StoreFull):
        store.put("local:b", "x" * 600)
    store.put("local:a", "x" * 900)  # replacing a value counts only the new one
    assert store.keys() == ["local:a"]


def test_the_routes_sit_behind_the_boundary_and_answer_in_words(tmp_path):
    with client(tmp_path, tmp_path / "studio-store") as browser:
        origin = {"Origin": ORIGIN}
        assert browser.get("/studio/local/store", headers=origin).status_code == 401
        headers = pair(browser)
        assert browser.put("/studio/local/store/local:a", json={"value": "1"}, headers=origin).status_code == 403
        assert browser.put("/studio/local/store/local:a", json={"value": "1"}, headers=headers).status_code == 204
        assert browser.get("/studio/local/store/local:a", headers=headers).json() == {"value": "1"}
        assert browser.get("/studio/local/store", params={"prefix": "local:"}, headers=headers).json() == {
            "keys": ["local:a"]
        }
        missing = browser.get("/studio/local/store/local:b", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "WV-STUDIO-STORE-KEY"
        assert browser.put("/studio/local/store/bad key", json={"value": "1"}, headers=headers).status_code in {
            404,
            422,
        }
        assert browser.put("/studio/local/store/local:a", json={"value": 1}, headers=headers).status_code == 422
        assert browser.delete("/studio/local/store/local:a", headers=headers).status_code == 204
        assert browser.get("/studio/local/store", headers=headers).json() == {"keys": []}


def test_a_browser_studio_has_no_store(tmp_path):
    with client(tmp_path, None) as browser:
        headers = pair(browser)
        answer = browser.get("/studio/local/store", headers=headers)
    assert answer.status_code == 404 and answer.json()["code"] == "WV-STUDIO-STORE-OFF"
