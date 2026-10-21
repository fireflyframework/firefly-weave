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
    assert (purged.removed, purged.discarded, purged.complete) == (2, 0, True)
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


def unattributable(tmp_path: Path, folder: Path, kind: str) -> Path:
    """One entry of a kind nobody can read in the store folder; returns what must survive outside it."""
    outside = tmp_path / f"outside-{kind}"
    if kind == "symlink":
        outside.write_text("keep me")
        os.symlink(outside, folder / "zz-link.json")
    elif kind == "folder":
        outside.mkdir()
        (outside / "keep.txt").write_text("keep me")
        (folder / "backup").mkdir()
        (folder / "backup" / "inner.json").write_text("{}")
        # A link inside the folder is removed, never followed.
        os.symlink(outside, folder / "backup" / "elsewhere")
    elif kind == "hard link":
        outside.write_text(json.dumps({"key": "local:x", "value": "keep me"}))
        os.link(outside, folder / file_name("local:x"))
    else:
        outside.write_text("nothing to keep")
        (folder / "zz-big.json").write_bytes(b"x" * (local_store.FILE_LIMIT + 1))
        os.chmod(folder / "zz-big.json", 0o600)
    return outside


@POSIX
@pytest.mark.parametrize("kind", ["symlink", "folder", "hard link", "oversized file"])
def test_an_entry_nobody_can_read_is_removed_without_following_it(tmp_path, kind, caplog):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    store.put("local:weave.localDraft.d-9", "{}")
    outside = unattributable(tmp_path, folder, kind)
    with caplog.at_level("WARNING", logger="firefly_weave.studio.local_store"):
        purged = store.purge(profile_prefix("acme"))
    assert (purged.removed, purged.discarded, purged.complete) == (1, 1, True)
    assert sorted(path.name for path in folder.iterdir() if not path.name.endswith(".lock")) == [
        file_name("local:weave.localDraft.d-9")
    ]
    # What lay outside is untouched, and the log names counts only.
    assert outside.exists() and (kind != "folder" or (outside / "keep.txt").exists())
    assert all("zz-" not in record.getMessage() and "backup" not in record.getMessage() for record in caplog.records)
    assert any("discarded 1" in record.getMessage() for record in caplog.records)


def test_signing_out_leaves_a_tombstone_that_the_next_start_purges_again(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    assert store.forget(profile_prefix("acme")).complete
    assert store.tombstones() == [profile_prefix("acme")]
    # A write that came in late is purged when Studio starts again.
    store.put(ACCOUNT + "testData.d-2", "{}")
    startup = LocalStore(folder).finish_purges()
    assert [(prefix, result.removed) for prefix, result in startup.purged] == [(profile_prefix("acme"), 1)]
    assert store.keys() == []
    store.remember(profile_prefix("acme"))
    assert store.tombstones() == []


def test_starting_leaves_platforms_it_doesn_t_know_and_counts_them(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    unknown = profile_prefix("Older name") + "0123456789abcdef:t:p:testData.d-1"
    saved = profile_prefix("Saved") + "0123456789abcdef:t:p:canvas.d-2"
    store.put(unknown, "{}")
    store.put(saved, "{}")
    store.put("local:weave.localDraft.d-3", "{}")
    startup = store.finish_purges(saved={profile_prefix("Saved")})
    assert startup.purged == [] and startup.untracked == 1
    assert store.keys() == sorted(["local:weave.localDraft.d-3", unknown, saved])


def test_a_purge_that_can_t_remove_an_entry_says_so(tmp_path, monkeypatch):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")

    def failing(self, directory, name):
        raise OSError("the disk said no")

    monkeypatch.setattr(local_store.PrivateFolder, "remove", failing)
    purged = store.forget(profile_prefix("acme"))
    assert (purged.removed, purged.failed, purged.complete) == (0, 1, False)
    monkeypatch.undo()
    assert store.tombstones() == [profile_prefix("acme")]
    assert store.finish_purges().purged[0][1].removed == 1


@POSIX
def test_a_folder_named_like_a_temporary_copy_never_stops_a_purge(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    store.put(ACCOUNT + "testData.d-1", "{}")
    (folder / ".x.json.0123.tmp").mkdir()
    (folder / ".x.json.0123.tmp" / "inside").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, folder / ".y.json.0123.tmp")
    purged = store.purge(profile_prefix("acme"))
    assert (purged.removed, purged.complete) == (1, True)
    assert not (folder / ".x.json.0123.tmp").exists()
    assert not (folder / ".y.json.0123.tmp").is_symlink() and outside.is_dir()


def test_a_guarded_write_under_a_tombstoned_prefix_is_refused_inside_the_lock(tmp_path):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    key = ACCOUNT + "testData.d-1"
    store.forget(profile_prefix("acme"))
    with pytest.raises(local_store.StoreSignedOut):
        store.put(key, "{}", guarded=True)
    # A write refused by the caller's own rule, checked under the same lock.
    with pytest.raises(local_store.StoreSignedOut):
        store.put("local:a", "{}", guarded=True, refuse=lambda name: name == "local:a")
    store.put("local:b", "{}", guarded=True)
    assert store.keys() == ["local:b"]
    store.remember(profile_prefix("acme"))
    store.put(key, "{}", guarded=True)
    assert store.keys() == ["local:b", key]


def test_signing_in_lifts_only_that_platform_s_tombstone(tmp_path):
    store = LocalStore(tmp_path / "studio-store")
    beta = profile_prefix("beta") + "0123456789abcdef:t:p:testData.d-1"
    store.forget(profile_prefix("acme"))
    store.forget(profile_prefix("beta"))
    store.remember(profile_prefix("acme"))
    assert store.tombstones() == [profile_prefix("beta")]
    store.put(ACCOUNT + "testData.d-1", "{}", guarded=True)
    with pytest.raises(local_store.StoreSignedOut):
        store.put(beta, "{}", guarded=True)


@pytest.mark.parametrize("damage", ["not json", '{"a": 1}', "\xff"])
def test_an_unreadable_tombstone_file_refuses_every_platform_until_it_signs_in(tmp_path, damage, caplog):
    folder = tmp_path / "studio-store"
    store = LocalStore(folder)
    acme = ACCOUNT + "testData.d-1"
    beta = profile_prefix("beta") + "0123456789abcdef:t:p:testData.d-2"
    store.put(acme, "{}")
    store.forget(profile_prefix("acme"))
    (folder / ".tombstones").write_bytes(damage.encode("latin-1"))
    # Nobody knows which platforms signed out: every platform's writes wait for its sign-in.
    for key in (acme, beta):
        with pytest.raises(local_store.StoreSignedOut):
            store.put(key, "{}", guarded=True)
    store.put("local:weave.localDraft.d-3", "{}", guarded=True)
    # Start-up purges nothing it can't attribute, and says so with a count only.
    store.put(beta, "{}")
    with caplog.at_level("WARNING"):
        startup = LocalStore(folder).finish_purges(saved={profile_prefix("beta")})
    assert startup.purged == [] and startup.unknown_tombstones
    kept = store.keys()
    assert beta in kept
    assert any("tombstones" in record.getMessage() for record in caplog.records)
    assert all("beta" not in record.getMessage() for record in caplog.records)
    # A sign-in lifts it for that platform only.
    store.remember(profile_prefix("acme"))
    store.put(acme, "{}", guarded=True)
    with pytest.raises(local_store.StoreSignedOut):
        store.put(beta, "{}", guarded=True)
    # And a later sign-out of that platform tombstones it again.
    store.forget(profile_prefix("acme"))
    with pytest.raises(local_store.StoreSignedOut):
        store.put(acme, "{}", guarded=True)
    store.remember(profile_prefix("beta"))
    store.put(beta, "{}", guarded=True)


def test_the_host_checks_the_sign_out_again_when_it_writes(tmp_path, monkeypatch):
    from firefly_weave.studio.service import StudioService

    folder = tmp_path / "studio-store"
    key = ACCOUNT + "testData.d-1"
    calls: list[str] = []

    def keeps(self, name):
        # The platform signs out after the first check, while the body arrives.
        calls.append(name)
        return len(calls) == 1

    monkeypatch.setattr(StudioService, "keeps", keeps)
    with client(tmp_path, folder) as browser:
        headers = pair(browser)
        answer = browser.put(f"/studio/local/store/{key}", headers=headers, json={"value": "{}"})
        assert answer.status_code == 409 and answer.json()["code"] == "WV-STUDIO-STORE-SIGNED-OUT"
    assert LocalStore(folder).keys() == []
    assert len(calls) >= 2


def test_the_host_refuses_a_write_whose_platform_signed_out_while_it_arrived(tmp_path, monkeypatch):
    folder = tmp_path / "studio-store"
    key = ACCOUNT + "testData.d-1"
    original = LocalStore.put

    def signed_out_meanwhile(self, name, value, **options):
        LocalStore(folder).forget(profile_prefix("acme"))
        return original(self, name, value, **options)

    monkeypatch.setattr(LocalStore, "put", signed_out_meanwhile)
    with client(tmp_path, folder) as browser:
        headers = pair(browser)
        answer = browser.put(f"/studio/local/store/{key}", headers=headers, json={"value": "{}"})
        assert answer.status_code == 409
    monkeypatch.undo()
    assert LocalStore(folder).keys() == []
