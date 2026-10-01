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

"""Keep navigation validation complete across public repository documentation."""

import runpy
from pathlib import Path

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check_docs.py"))["check"]


def test_nested_example_fixture_and_deployment_guides_are_checked(tmp_path):
    paths = ["docs/guide.md", "examples/demo/README.md", "tests/fixtures/demo/README.md", "deploy/demo/README.md"]
    (tmp_path / "README.md").write_text("# Home\n" + "\n".join(f"[Guide]({path})" for path in paths))
    for name in paths:
        page = tmp_path / name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("# Guide\n[Missing](missing.md)\n")
    report = CHECK(tmp_path)
    assert report["markdown_pages"] == 5
    assert {(item["path"], item["reason"]) for item in report["errors"]} == {(name, "missing") for name in paths}
    for name in paths:
        (tmp_path / name).write_text("# Guide\n")
    assert CHECK(tmp_path)["errors"] == []


def test_private_plans_remain_outside_public_navigation(tmp_path):
    (tmp_path / "README.md").write_text("# Home\n")
    private = tmp_path / "docs/superpowers/plan.md"
    private.parent.mkdir(parents=True)
    private.write_text("# Private\n[Missing](missing.md)\n")
    report = CHECK(tmp_path)
    assert report["markdown_pages"] == 1
    assert report["errors"] == []
