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
    purged = store.purge(profile_prefix("acme"))
    assert (purged.removed, purged.unreadable, purged.complete) == (2, 0, True)
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


@POSIX
def test_a_purge_skips_what_it_can_t_read_and_says_so(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    store.put(ACCOUNT + "canvas.d-1", "{}")
    store.put("local:weave.localDraft.d-9", "{}")
    (folder / "backup").mkdir()
    (tmp_path / "x.json").write_text("{}")
    os.symlink(tmp_path / "x.json", folder / "zz-link.json")
    os.link(folder / file_name("local:weave.localDraft.d-9"), tmp_path / "hard-elsewhere.json")
    (folder / "zz-big.json").write_bytes(b"x" * (local_store.FILE_LIMIT + 1))
    purged = store.purge(profile_prefix("acme"))
    # The platform's own keys go; four entries nobody can attribute stay, and the purge says it is incomplete.
    assert (purged.removed, purged.unreadable, purged.complete) == (2, 4, False)
    assert not (folder / file_name(ACCOUNT + "testData.d-1")).exists()
    assert not (folder / file_name(ACCOUNT + "canvas.d-1")).exists()
    assert (tmp_path / "x.json").read_text() == "{}"
    assert store.pending() == [profile_prefix("acme")]


@POSIX
def test_a_purge_removes_copies_a_write_left_behind(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put("local:a", "1")
    # What a process killed between writing and renaming leaves: a hidden temporary copy with the key and value.
    orphan = folder / f".{file_name(ACCOUNT + 'testData.d-3')}.0123456789abcdef.tmp"
    orphan.write_text(json.dumps({"key": ACCOUNT + "testData.d-3", "value": "from a killed write"}))
    assert store.purge(profile_prefix("acme")).complete
    assert not orphan.exists()
    assert store.keys() == ["local:a"]


def test_an_unfinished_purge_is_kept_and_finished_later(tmp_path, monkeypatch):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    remove = local_store.PrivateFolder.remove

    def failing(self, directory, name):
        raise OSError("the disk said no")

    monkeypatch.setattr(local_store.PrivateFolder, "remove", failing)
    with pytest.raises(StoreUnavailable):
        store.purge(profile_prefix("acme"))
    assert store.pending() == [profile_prefix("acme")]
    monkeypatch.setattr(local_store.PrivateFolder, "remove", remove)
    finished = LocalStore(folder).finish_purges(saved=set())
    assert [(prefix, result.complete) for prefix, result in finished] == [(profile_prefix("acme"), True)]
    assert store.keys() == [] and store.pending() == []


def test_starting_forgets_platforms_that_are_no_longer_saved(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    kept = profile_prefix("Saved") + "0123456789abcdef:t:p:canvas.d-2"
    store.put(kept, "{}")
    store.put("local:weave.localDraft.d-3", "{}")
    store.put("ui:weave.panes", "{}")
    store.finish_purges(saved={profile_prefix("Saved")})
    assert store.keys() == ["local:weave.localDraft.d-3", kept, "ui:weave.panes"]


def test_files_are_named_by_the_sha_256_of_their_key(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    key = ACCOUNT + "testData.d-1"
    store.put(key, "{}")
    expected = hashlib.sha256(key.encode("ascii")).hexdigest() + ".json"
    assert [path.name for path in (tmp_path / "studio-store").iterdir() if not path.name.endswith(".lock")] == [
        expected
    ]


@POSIX
def test_a_file_that_names_another_key_is_never_read_as_this_one(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put("local:a", "1")
    # A copy of a's file under b's name holds key a: neither listing nor reading b may answer a's value.
    (folder / file_name("local:b")).write_bytes((folder / file_name("local:a")).read_bytes())
    os.chmod(folder / file_name("local:b"), 0o600)
    assert store.keys() == ["local:a"]
    assert store.get("local:b") is None


@POSIX
def test_a_damaged_file_or_a_value_utf_8_can_t_hold_is_answered_in_words(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put("local:a", "1")
    (folder / file_name("local:a")).write_bytes(b"{not json")
    with pytest.raises(StoreUnavailable):
        store.get("local:a")
    with pytest.raises(ValueError):
        store.put("local:b", "\ud800")
    with client(tmp_path, folder) as browser:
        headers = pair(browser)
        damaged = browser.get("/studio/local/store/local:a", headers=headers)
        assert damaged.status_code == 500 and damaged.json()["code"] == "WV-STUDIO-STORE"


def test_the_routes_refuse_a_full_store_a_delete_without_csrf_and_an_oversized_body(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "STORE_LIMIT", 1024)
    with client(tmp_path, tmp_path / "studio-store") as browser:
        headers = pair(browser)
        full = browser.put("/studio/local/store/local:a", json={"value": "x" * 2000}, headers=headers)
        assert full.status_code == 413 and full.json()["code"] == "WV-STUDIO-STORE-FULL"
        assert browser.delete("/studio/local/store/local:a", headers={"Origin": ORIGIN}).status_code == 403
        huge = browser.put("/studio/local/store/local:a", json={"value": "x" * (3 * 1024 * 1024)}, headers=headers)
        assert huge.status_code == 413


@POSIX
def test_an_entry_counts_only_under_its_own_file_name(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put("local:a", "1")
    impostor = folder / "zzzz.json"
    impostor.write_text(json.dumps({"key": "local:a", "value": "2"}))
    os.chmod(impostor, 0o600)
    assert store.purge("local:").removed == 1
    assert store.keys() == [] and impostor.exists()
