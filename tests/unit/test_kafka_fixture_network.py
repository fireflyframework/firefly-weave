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


async def test_fixture_uses_existing_bridge_without_changing_published_port(tmp_path_factory, monkeypatch):
    path = Path(__file__).parents[1] / "integration/connectors/test_kafka.py"
    spec = importlib.util.spec_from_file_location("kafka_network_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("WEAVE_TEST_KAFKA_IMAGE", "apache/kafka@sha256:" + "a" * 64)

    class Captured(Exception):
        pass

    async def docker(*args):
        assert args[-3:] == ("up", "-d", "kafka")
        files = [Path(args[i + 1]) for i, value in enumerate(args) if value == "-f"]
        assert len(files) == 2
        base = yaml.safe_load(files[0].read_text())
        override = yaml.safe_load(files[1].read_text())
        assert override == {"services": {"kafka": {"network_mode": "bridge"}}}
        assert base["services"]["kafka"]["ports"] == ["127.0.0.1:${WEAVE_KAFKA_PORT:?Set the isolated Kafka port}:9092"]
        assert args[args.index("--project-name") + 1].startswith("weave-d2-")
        assert files[1].parent == Path(args[args.index("--env-file") + 1]).parent
        raise Captured

    monkeypatch.setattr(module, "docker", docker)
    fixture = module.kafka_backend.__wrapped__(tmp_path_factory)
    with pytest.raises(Captured):
        await anext(fixture)
