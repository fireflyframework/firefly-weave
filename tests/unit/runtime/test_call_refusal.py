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


"""A platform that does not list the call feature refuses a compiled call at import."""

from pathlib import Path

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import UnsupportedIR
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition

EXAMPLES = Path("examples/language")


@pytest.fixture
def artifact():
    callee = parse_source((EXAMPLES / "notify-customer.workflow.yaml").read_text(), format="yaml").value
    result = compile_source(
        (EXAMPLES / "order-intake.workflow.yaml").read_text(),
        format="yaml",
        catalog=CatalogSnapshot.from_definitions([load_definition(callee)]),
    )
    assert result.ok, result.to_bytes()
    return result.artifact


def test_import_refuses_a_call_while_the_platform_does_not_list_the_call_feature(artifact):
    with pytest.raises(UnsupportedIR) as refused:
        import_artifact(artifact.to_bytes())
    assert refused.value.missing == ("flow.callWorkflow",)
    features = ["flow.callWorkflow", "text.concat", "text.join"]
    assert import_artifact(artifact.to_bytes(), features=features).digest == artifact.digest
