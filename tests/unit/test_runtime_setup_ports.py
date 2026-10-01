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

"""Local runtime endpoint guards run before database or output allocation."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def setup_module(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/setup-runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_setup_ports", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv(
        "WEAVE_TEST_DATABASE_URL",
        "postgresql+asyncpg://weave_b1_owner:unused@localhost:55434/weave_b1_control",
    )
    return module


class BackendBoundary(Exception):
    pass


@pytest.mark.parametrize("port", [1024, 18080, 18081, 18082, 65535])
async def test_local_canonical_port_reaches_existing_backend_boundary(setup_module, monkeypatch, tmp_path, port):
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", f"http://localhost:{port}")

    def engine(*args, **kwargs):
        raise BackendBoundary

    monkeypatch.setattr(setup_module, "create_async_engine", engine)
    output = tmp_path / "runtime.env"
    with pytest.raises(BackendBoundary):
        await setup_module.setup(output)
    assert not output.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:1023",
        "http://localhost:65536",
        "http://localhost:018082",
        "http://localhost:-18082",
        "http://localhost:port",
        "http://user@localhost:18082",
        "http://user:password@localhost:18082",
        "http://localhost:18082/",
        "http://localhost:18082/path",
        "http://localhost:18082?query",
        "http://localhost:18082#fragment",
        "http://localhost:18082?",
        "http://localhost:18082#",
        "http://127.0.0.1:18082",
        "http://[::1]:18082",
        "https://localhost:18082",
        "http://remote:18082",
        "http://localhost.evil:18082",
        "HTTP://localhost:18082",
        "http://LOCALHOST:18082",
        " http://localhost:18082",
        "http://localhost:18082\n",
    ],
)
async def test_unsafe_origin_rejected_before_database_and_output(setup_module, monkeypatch, tmp_path, url):
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", url)

    def engine(*args, **kwargs):
        pytest.fail("Rejected endpoint must not allocate a database engine")

    monkeypatch.setattr(setup_module, "create_async_engine", engine)
    output = tmp_path / "runtime.env"
    with pytest.raises(ValueError, match="guarded local Keycloak"):
        await setup_module.setup(output)
    assert not output.exists()
