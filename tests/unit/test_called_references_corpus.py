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

"""The platform finds the called workflows in Studio's corpus that Studio's activation dialog finds, in its order."""

import json
from pathlib import Path

import pytest

from firefly_weave.compiler.calls import called_references

CORPUS = json.loads(Path("studio/tests/fixtures/called-references-corpus.json").read_text())


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: str(case["name"]))
def test_called_references_match_the_corpus(case: dict[str, object]) -> None:
    assert called_references(case["document"]) == case["references"]  # type: ignore[arg-type]
