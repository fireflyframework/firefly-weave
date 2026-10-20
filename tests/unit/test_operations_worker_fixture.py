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

"""The sleep fixture worker the local platform runs for Operations: bounded, unprivileged, database-free."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from firefly_weave.contracts.workers import ReleaseRequest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/operations-worker"


def fixture():
    spec = importlib.util.spec_from_file_location("operations_worker_fixture", FIXTURE / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest():
    return json.loads((FIXTURE / "manifest.json").read_text())


async def test_the_fixture_worker_returns_after_sleeping_the_requested_time(monkeypatch):
    module = fixture()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    assert await module.sleep(SimpleNamespace(input={"ms": 25})) == {"slept_ms": 25}
    assert slept == [0.025]


def test_the_fixture_manifest_bounds_the_sleep():
    (capability,) = manifest()["capabilities"]
    assert (capability["taskType"], capability["taskVersion"], capability["sideEffect"]) == (
        "example-sleep",
        "1.0.0",
        "read_only",
    )
    # The manifest is what the platform admits as the fixture worker's release.
    ReleaseRequest.model_validate_json(json.dumps({"image_digest": "sha256:" + "a" * 64, **manifest()}))
    assert manifest()["credential_capabilities"] == []
    schema = capability["inputSchema"]
    jsonschema.validate({"ms": 60000}, schema)
    for refused in ({"ms": 60001}, {"ms": -1}, {"ms": 5, "extra": True}, {}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(refused, schema)
    jsonschema.validate({"slept_ms": 1}, capability["outputSchema"])


@pytest.mark.parametrize("name", ["WEAVE_DATABASE_URL", "PGPASSWORD", "WEAVE_KC_ADMIN_PASSWORD"])
async def test_the_fixture_worker_refuses_database_settings(monkeypatch, name):
    module = fixture()
    monkeypatch.setenv(name, "anything")

    def no_network(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(module.httpx, "AsyncClient", no_network)
    with pytest.raises(RuntimeError, match="database or administrator"):
        await module.main()


def test_the_fixture_worker_prints_its_manifest(capsys):
    module = fixture()
    assert module.cli(["--release-manifest"]) == 0
    assert json.loads(capsys.readouterr().out) == manifest()


@pytest.mark.parametrize(("value", "expected"), [(None, 2), ("1", 1), ("8", 8)])
def test_the_fixture_worker_capacity_is_bounded(value, expected):
    environ = {} if value is None else {"WEAVE_WORKER_CAPACITY": value}
    assert fixture().capacity(environ) == expected
    for refused in ("0", "9", "two"):
        with pytest.raises(ValueError):
            fixture().capacity({"WEAVE_WORKER_CAPACITY": refused})
