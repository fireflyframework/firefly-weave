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

"""J8.3 reads every built docs page for the old name except the changelog, which keeps releases as they shipped."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Spelled from parts so the repository's brand check never matches this test itself.
OLD_NAME = "Lu" + "mi"


def load():
    spec = importlib.util.spec_from_file_location("acceptance_j8_brand", ROOT / "tests/acceptance/test_j8_brand.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


brand = load()


def test_the_changelog_keeps_old_names_and_every_other_page_is_read():
    texts = {
        "project/changelog/index.html": f"<li>Remove the {OLD_NAME} mascot from Studio</li>",
        "project/changelog/notes.html": f"<p>{OLD_NAME}</p>",
        "changelog/index.html": f"<p>{OLD_NAME}</p>",
        "index.html": "<p>Firefly Weave</p>",
        "diagrams/ai.svg": f"<text>{OLD_NAME} says</text>",
        "stylesheets/extra.css": f"/* {OLD_NAME} */",
    }
    assert brand.pages_with_the_old_name(texts) == [
        "project/changelog/notes.html",
        "changelog/index.html",
        "diagrams/ai.svg",
    ]


def test_a_moved_changelog_fails_instead_of_widening_the_exclusion():
    with pytest.raises(AssertionError, match="changelog page moved"):
        brand.pages_with_the_old_name({"index.html": "<p>Firefly Weave</p>"})
