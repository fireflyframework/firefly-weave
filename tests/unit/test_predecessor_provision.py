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

"""Version-isolated administrative provisioning and fail-closed receipt checks."""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

SUPPORT = Path(__file__).parents[1] / "e2e/support"


def bridge():
    path = SUPPORT / "predecessor_access.py"
    assert path.is_file(), "Predecessor provisioning must cross the installed child boundary"
    spec = importlib.util.spec_from_file_location("predecessor_access_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wheel_fixture(tmp_path, source='SCHEMA_VERSION = "0028_files"\n'):
    wheel = tmp_path / "old.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("firefly_weave/persistence/migrations.py", source)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def test_expected_schema_is_bound_to_pinned_wheel_without_executing_it(tmp_path):
    module = bridge()
    wheel, sha = wheel_fixture(tmp_path, 'raise RuntimeError("must not execute")\nSCHEMA_VERSION = "0028_files"\n')
    assert module.artifact_head(wheel, sha) == "0028_files"
    with pytest.raises(ValueError, match="artifact"):
        module.artifact_head(wheel, "0" * 64)
    with pytest.raises(ValueError, match="expected schema"):
        module.verify_expected_head(wheel, sha, "0012_secret_admission")
    assert module.verify_expected_head(wheel, sha, "0028_files") == "0028_files"
    with pytest.raises(ValueError, match="must differ"):
        module.artifact_head(wheel, sha, current_sha=sha)
    assert module.artifact_head(wheel, sha, current_sha="f" * 64) == "0028_files"


@pytest.mark.parametrize(
    "source",
    [
        'SCHEMA_VERSION = str("0028_files")',
        'SCHEMA_VERSION = "0028_files"\nSCHEMA_VERSION = "0030_worker_presence"',
        'SCHEMA_VERSION = "0028_files"\nSCHEMA_VERSION: str = "0030_worker_presence"',
        'SCHEMA_VERSION = "../../invalid"',
        'OTHER_VERSION = "0028_files"',
    ],
)
def test_expected_schema_rejects_nonliteral_ambiguous_or_invalid_metadata(tmp_path, source):
    wheel, sha = wheel_fixture(tmp_path, source)
    with pytest.raises(ValueError, match="schema"):
        bridge().artifact_head(wheel, sha)


def test_expected_schema_rejects_oversized_metadata(tmp_path):
    wheel, sha = wheel_fixture(tmp_path, "#" + "x" * (256 * 1024))
    with pytest.raises(ValueError, match="bounded"):
        bridge().artifact_head(wheel, sha)


def test_expected_schema_rejects_duplicate_wheel_members(tmp_path):
    wheel, _ = wheel_fixture(tmp_path)
    with ZipFile(wheel, "a") as archive, pytest.warns(UserWarning, match="Duplicate name"):
        archive.writestr("firefly_weave/persistence/migrations.py", 'SCHEMA_VERSION = "0030_worker_presence"')
    with pytest.raises(ValueError, match="Unique bounded"):
        bridge().artifact_head(wheel, hashlib.sha256(wheel.read_bytes()).hexdigest())


@pytest.mark.asyncio
async def test_child_rejects_wrong_expected_head_before_request_or_database_access(tmp_path, monkeypatch):
    module = bridge()
    wheel, sha = wheel_fixture(tmp_path)
    monkeypatch.setattr(sys, "flags", SimpleNamespace(isolated=True))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "predecessor_access.py",
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            sha,
            "--expected-head",
            "0012_secret_admission",
            "--request",
            str(tmp_path / "absent-input"),
            "--output",
            str(tmp_path / "absent-output"),
        ],
    )
    monkeypatch.setattr(module, "load", lambda *args: SimpleNamespace(verify_install=lambda *args: {}))
    with pytest.raises(ValueError, match="expected schema"):
        await module.main()
    assert not (tmp_path / "absent-output").exists()


def test_receipt_rejects_wrong_version_schema_request_or_in_process_execution(tmp_path):
    module = bridge()
    expected = {"wheel_sha256": "a" * 64, "request_sha256": "b" * 64, "head": "0028_files"}
    valid = {
        **expected,
        "pid": os.getpid() + 1000,
        "members": 20,
        "installed_origin": "/isolated/site-packages/firefly_weave",
        "result": {"grant_id": "receipt"},
    }
    assert module.validate_receipt(valid, expected) == valid["result"]
    for changes in (
        {"head": "0021_operations"},
        {"wheel_sha256": "c" * 64},
        {"request_sha256": "d" * 64},
        {"pid": os.getpid()},
        {"members": 0},
    ):
        with pytest.raises(ValueError):
            module.validate_receipt({**valid, **changes}, expected)


@pytest.mark.asyncio
async def test_selected_child_receipt_is_retained_and_not_parent_provisioning(tmp_path, monkeypatch):
    module = bridge()
    # Real process I/O, with a stand-in for the unavailable database endpoint only.
    child = tmp_path / "child.py"
    child.write_text("""import argparse,hashlib,json,os,sys
from pathlib import Path
p=argparse.ArgumentParser()
for key in ("wheel", "wheel-sha256", "expected-head", "request", "output"): p.add_argument("--"+key, required=True)
a=p.parse_args(); raw=Path(a.request).read_bytes(); r=json.loads(raw)
assert r["operation"] == "grant" and r["runtime"]["private"] == "not-in-argv"
assert "PYTHONPATH" not in os.environ and sys.flags.isolated
receipt={"wheel_sha256":a.wheel_sha256,"request_sha256":hashlib.sha256(raw).hexdigest(),"head":a.expected_head,"pid":os.getpid(),"members":20,"installed_origin":"/isolated/site-packages/firefly_weave","result":{"grant_id":"old-native-result"}}
with os.fdopen(os.open(a.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),"w") as f: json.dump(receipt,f)
""")
    monkeypatch.setattr(module, "CHILD", child)
    request = {"operation": "grant", "runtime": {"private": "not-in-argv"}}
    wheel, sha = wheel_fixture(tmp_path)
    result = await module.invoke(sys.executable, wheel, sha, tmp_path, request)
    assert result == {"grant_id": "old-native-result"}
    outputs = list(tmp_path.glob("access-*.output.json"))
    inputs = list(tmp_path.glob("access-*.input.json"))
    assert len(outputs) == len(inputs) == 1
    assert outputs[0].stat().st_mode & 0o777 == inputs[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(outputs[0].read_text())["pid"] != os.getpid()


def test_request_rejects_symlinks_and_oversized_or_public_files(tmp_path):
    module = bridge()
    path = tmp_path / "input.json"
    path.write_text("{}")
    path.chmod(0o600)
    assert module.read_private(path) == b"{}"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises((OSError, ValueError)):
        module.read_private(link)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        module.read_private(path)
    path.chmod(0o600)
    path.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError):
        module.read_private(path)


@pytest.mark.asyncio
async def test_current_domain_code_is_refused_before_any_predecessor_database_access(monkeypatch):
    import sqlalchemy.ext.asyncio as database

    def forbidden(*args, **kwargs):
        raise AssertionError("Wrong-version code must not reach database construction")

    monkeypatch.setattr(database, "create_async_engine", forbidden)
    with pytest.raises(ValueError, match="Retained predecessor schema"):
        await bridge().dispatch({"operation": "grant"}, "0028_files")
