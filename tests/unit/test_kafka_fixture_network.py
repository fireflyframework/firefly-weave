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

"""Kafka fixture allocates no per-project subnet while retaining unique ownership."""

import importlib.util
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("host_data", [False, True])
async def test_fixture_uses_existing_bridge_without_changing_published_port(tmp_path_factory, monkeypatch, host_data):
    path = Path(__file__).parents[1] / "integration/connectors/test_kafka.py"
    spec = importlib.util.spec_from_file_location("kafka_network_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("WEAVE_TEST_KAFKA_IMAGE", "apache/kafka@sha256:" + "a" * 64)
    monkeypatch.delenv("WEAVE_TEST_KAFKA_DATA_ROOT", raising=False)
    if host_data:
        root = tmp_path_factory.mktemp("kafka-host-data")
        root.chmod(0o700)
        monkeypatch.setenv("WEAVE_TEST_KAFKA_DATA_ROOT", str(root))

    class Captured(Exception):
        pass

    async def docker(*args):
        assert args[-3:] == ("up", "-d", "kafka")
        files = [Path(args[i + 1]) for i, value in enumerate(args) if value == "-f"]
        assert len(files) == 2
        base = yaml.safe_load(files[0].read_text())
        override = yaml.safe_load(files[1].read_text())
        service = override["services"]["kafka"]
        assert service["network_mode"] == "bridge"
        if host_data:
            mount = service["volumes"][0]
            assert mount["type"] == "bind" and mount["target"] == "/tmp/kafka-logs"
            assert Path(mount["source"]).parent == root
        else:
            assert "volumes" not in service
        assert base["services"]["kafka"]["ports"] == ["127.0.0.1:${WEAVE_KAFKA_PORT:?Set the isolated Kafka port}:9092"]
        assert args[args.index("--project-name") + 1].startswith("weave-d2-")
        assert files[1].parent == Path(args[args.index("--env-file") + 1]).parent
        raise Captured

    monkeypatch.setattr(module, "docker", docker)
    fixture = module.kafka_backend.__wrapped__(tmp_path_factory)
    with pytest.raises(Captured):
        await anext(fixture)


def test_host_data_is_unique_retained_and_requires_private_parent(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "integration/connectors/test_kafka.py"
    spec = importlib.util.spec_from_file_location("kafka_storage_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("WEAVE_TEST_KAFKA_DATA_ROOT", raising=False)
    assert module.kafka_data_directory() is None
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("WEAVE_TEST_KAFKA_DATA_ROOT", str(root))
    first, second = module.kafka_data_directory(), module.kafka_data_directory()
    assert first != second
    assert first.parent == second.parent == root
    assert first.is_dir() and second.is_dir()
    root.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        module.kafka_data_directory()
    root.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(root)
    monkeypatch.setenv("WEAVE_TEST_KAFKA_DATA_ROOT", str(link))
    with pytest.raises(ValueError, match="private"):
        module.kafka_data_directory()


async def test_tls_fixture_uses_separate_host_bind(tmp_path, monkeypatch):
    folder = Path(__file__).parents[1] / "integration/connectors"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("kafka_tls_storage_fixture", folder / "test_kafka_tls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("WEAVE_TEST_KAFKA_DATA_ROOT", str(root))
    monkeypatch.setenv("WEAVE_TEST_KAFKA_IMAGE", "apache/kafka@sha256:" + "a" * 64)

    class Captured(Exception):
        pass

    async def docker(*args):
        assert args[0] == "create"
        mount = dict(part.split("=", 1) for part in args[args.index("--mount") + 1].split(","))
        assert mount["type"] == "bind" and mount["target"] == "/tmp/kafka-logs"
        assert Path(mount["source"]).parent == root
        assert args[args.index("-p") + 1].startswith("127.0.0.1:")
        raise Captured

    monkeypatch.setattr(module, "docker", docker)
    fixture = module.tls_broker.__wrapped__((None, None, tmp_path / "store", "fixture-password"), tmp_path)
    with pytest.raises(Captured):
        await anext(fixture)
