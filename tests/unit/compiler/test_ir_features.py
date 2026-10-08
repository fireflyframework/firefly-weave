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

"""The IR feature set's frozen shape; the compiler computes and checks it in a later release."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import IR_VERSION_EXTENSIONS, WorkflowIR
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES, LANGUAGE_FEATURES
from firefly_weave.contracts.schema_export import export_schemas

EXECUTABLE = json.loads(Path("tests/fixtures/canonical/empty-workflow.executable.json").read_text())


def test_the_ir_version_extensions_is_named_but_nothing_is_advertised_yet():
    assert IR_VERSION_EXTENSIONS == "weave/ir-v1alpha4"
    assert ADVERTISED_FEATURES == ()


def test_an_empty_feature_set_is_omitted():
    model = WorkflowIR.model_validate(EXECUTABLE)
    assert model.features == []
    assert "features" not in model.model_dump(by_alias=True)
    assert "features" not in WorkflowIR.model_validate({**EXECUTABLE, "features": []}).model_dump(by_alias=True)


def test_compiled_workflows_keep_their_ir_version_and_digest(catalog, workflow_source):
    result = compile_source(workflow_source, format="yaml", catalog=catalog)
    executable = result.artifact.executable
    assert "features" not in executable
    assert executable["irVersion"] == "weave/ir-v1alpha1"
    assert canonical_digest(WorkflowIR.model_validate(executable).model_dump(by_alias=True)) == result.artifact.digest


@pytest.mark.parametrize(
    ("features", "message"),
    [
        (["text.join", "text.concat"], "sorted and unique"),
        (["text.concat", "text.concat"], "sorted and unique"),
        (["text.concat"], "require weave/ir-v1alpha4"),
        (["loops"], "Input should be"),
    ],
)
def test_features_are_sorted_known_and_need_the_ir_version_extensions(features, message):
    with pytest.raises(ValidationError, match=message):
        WorkflowIR.model_validate({**EXECUTABLE, "features": features})


@pytest.mark.parametrize("features", [[], ["text.concat"]])
def test_the_ir_version_extensions_is_not_accepted_until_a_later_release_widens_the_model(features):
    # The version is named, but the model still lists v1alpha1 to v1alpha3 only, so no executable can use it yet.
    with pytest.raises(ValidationError, match="Input should be 'weave/ir-v1alpha1'"):
        WorkflowIR.model_validate({**EXECUTABLE, "irVersion": IR_VERSION_EXTENSIONS, "features": features})


def test_exported_executable_schema_carries_the_feature_vocabulary():
    schema = export_schemas()["executable"]
    assert schema["$defs"]["WorkflowIR"]["properties"]["features"]["uniqueItems"] is True
    assert schema["$defs"]["LanguageFeature"]["enum"] == list(LANGUAGE_FEATURES)
