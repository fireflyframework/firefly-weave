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

"""The shared language fixtures are current and agree with the Python contracts."""

import json
import re
import runpy
from pathlib import Path

import pytest
import rfc8785

from firefly_weave.contracts.instance_keys import (
    INSTANCE_KEY_PATTERN,
    InstanceKey,
    InvalidInstanceKey,
    format_instance,
    instance_view,
    node_of,
    split_instance,
)
from firefly_weave.contracts.language import language_manifest

FIXTURES = Path("studio/tests/fixtures/language")


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_committed_fixtures_are_what_the_generator_writes_today():
    rendered = runpy.run_path("scripts/language_fixtures.py")["render"]()
    assert sorted(rendered) == [
        "instance-keys.json",
        "manifest.json",
        "template-segments.json",
        "text-conversion.json",
    ]
    for name, text in rendered.items():
        assert (FIXTURES / name).read_text(encoding="utf-8") == text, name


@pytest.mark.parametrize("case", fixture("instance-keys.json")["valid"], ids=lambda case: case["key"])
def test_valid_instance_key_cases(case):
    parsed = InstanceKey(case["node_id"], tuple(case["indexes"]), tuple(case["segments"]), case["count"])
    assert split_instance(case["key"]) == parsed
    assert format_instance(parsed) == case["key"]
    assert node_of(case["key"]) == split_instance(case["key"]).node_id == case["node_id"]
    assert re.fullmatch(INSTANCE_KEY_PATTERN, case["key"])
    view = instance_view(case["key"])
    assert {"node_id": view.node_id, "instance_key": view.instance_key, "iteration": view.iteration} == case["view"]


@pytest.mark.parametrize("case", fixture("instance-keys.json")["invalid"], ids=lambda case: case["reason"])
def test_invalid_instance_key_cases(case):
    with pytest.raises(InvalidInstanceKey):
        split_instance(case["key"])
    assert re.fullmatch(INSTANCE_KEY_PATTERN, case["key"]) is None


def test_instance_key_fixture_publishes_the_python_pattern():
    assert fixture("instance-keys.json")["pattern"] == INSTANCE_KEY_PATTERN


@pytest.mark.parametrize("case", fixture("text-conversion.json")["cases"], ids=lambda case: repr(case["value"]))
def test_text_conversion_cases_follow_ecmascript_number_formatting(case):
    value = case["value"]
    expected = value if isinstance(value, str) else rfc8785.dumps(value).decode()
    if isinstance(value, bool):
        expected = "true" if value else "false"
    assert case["text"] == expected


def test_manifest_snapshot_is_the_served_manifest():
    assert fixture("manifest.json")["manifest"] == language_manifest().model_dump(mode="json")


def test_freshness_check_accepts_windows_line_endings_and_catches_drift(tmp_path):
    generator = runpy.run_path("scripts/language_fixtures.py")
    for name, text in generator["render"]().items():
        (tmp_path / name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert generator["main"](["--check", "--output", str(tmp_path)]) == 0
    (tmp_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    assert generator["main"](["--check", "--output", str(tmp_path)]) == 1
