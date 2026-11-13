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

"""versions.toml may pin the cluster tools and node images in their own table, as strictly as the others."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def journeys(monkeypatch):
    """The acceptance journeys script, registered in sys.modules only for the test that loads it."""
    spec = importlib.util.spec_from_file_location("acceptance_journeys", ROOT / "scripts/acceptance_journeys.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "acceptance_journeys", module)
    spec.loader.exec_module(module)
    return module


BASE = """
version = 1

[tools]
uv = "0.8.22"

[images]
registry = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
"""
CLUSTER = """
[cluster.tools]
kind = "v0.33.0"

[cluster.images]
kind_node = "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
"""


def load(journeys, tmp_path, text):
    path = tmp_path / "versions.toml"
    path.write_text(text)
    return journeys.load_versions(path)


def test_a_file_without_a_cluster_table_still_loads(journeys, tmp_path):
    assert "cluster" not in load(journeys, tmp_path, BASE)


def test_a_cluster_table_of_tools_and_pinned_images_loads(journeys, tmp_path):
    cluster = load(journeys, tmp_path, BASE + CLUSTER)["cluster"]
    assert cluster["tools"] == {"kind": "v0.33.0"}
    assert cluster["images"]["kind_node"].startswith("kindest/node:v1.37.0@sha256:")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (CLUSTER.replace("@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5", ""), "kind_node"),
        (CLUSTER.replace('kind = "v0.33.0"', "kind = 33"), "cluster tool versions"),
        (CLUSTER + '\n[cluster.extra]\nname = "x"\n', "cluster"),
    ],
)
def test_a_cluster_table_is_checked_as_strictly_as_the_others(journeys, tmp_path, text, message):
    with pytest.raises(journeys.JourneysInvalid, match=message):
        load(journeys, tmp_path, BASE + text)


def test_an_unknown_table_is_still_refused(journeys, tmp_path):
    with pytest.raises(journeys.JourneysInvalid, match="version = 1"):
        load(journeys, tmp_path, BASE + '\n[clusters]\nkind = "v0.33.0"\n')
