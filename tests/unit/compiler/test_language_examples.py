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

"""The language examples have their final shape and report exactly the features not compiled yet."""

from pathlib import Path

import pytest

from firefly_weave.compiler.api import compile_source, validate_authoring
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition

EXAMPLES = Path("examples/language")
EXPECTED = {
    "notify-customer.workflow.yaml": [],
    # Text operators compile; loops and workflow calls are still reported.
    "notify-overdue.workflow.yaml": [("/spec/steps/0/kind", "flow.forEach")],
    "order-intake.workflow.yaml": [
        ("/spec/steps/0/kind", "flow.callWorkflow"),
        ("/spec/steps/1/kind", "flow.callWorkflow"),
    ],
}


def test_every_language_example_is_listed():
    assert sorted(path.name for path in EXAMPLES.glob("*.yaml")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_example_shape_is_final_and_unsupported_features_are_reported(name):
    text = (EXAMPLES / name).read_text()
    load_definition(parse_source(text, format="yaml").value)
    result = validate_authoring(text, format="yaml", filename=name)
    reported = sorted(
        (d.path, d.message.split("language feature ")[1].split(",")[0])
        for d in result.diagnostics
        if d.code == "WV-COMP-UNSUPPORTED_FEATURE"
    )
    assert reported == EXPECTED[name]
    assert [d.code for d in result.diagnostics if d.severity == "error"] == ["WV-COMP-UNSUPPORTED_FEATURE"] * len(
        EXPECTED[name]
    )


def test_the_callable_example_compiles_today():
    text = (EXAMPLES / "notify-customer.workflow.yaml").read_text()
    result = compile_source(text, format="yaml", catalog=CatalogSnapshot.from_definitions([]))
    assert result.ok, [d.code for d in result.diagnostics]
