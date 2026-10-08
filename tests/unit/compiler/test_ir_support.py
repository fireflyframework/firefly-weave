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

"""A platform imports only artifacts whose IR version and language features it runs."""

import json

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import UnsupportedIR, require_supported_ir
from firefly_weave.definitions.models import ir_unsupported

TEXT = ("text.concat", "text.join")


def envelope(value):
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greeting", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [], "output": value},
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return json.loads(result.artifact.to_bytes())


CONCAT = {"op": {"name": "concat", "args": [{"literal": "Hello"}]}}


def test_a_platform_running_the_feature_imports_the_artifact():
    artifact = import_artifact(envelope(CONCAT), features=TEXT)
    assert artifact.executable["features"] == ["text.concat"]


@pytest.mark.parametrize("features", [(), ("text.join",)], ids=["no-features", "other-feature"])
def test_a_platform_without_the_feature_refuses_it_with_ir_unsupported(features):
    with pytest.raises(UnsupportedIR) as refused:
        import_artifact(envelope(CONCAT), features=features)
    assert (str(refused.value), refused.value.missing) == ("ir_unsupported", ("text.concat",))


def forged(change):
    value = envelope(CONCAT)
    change(value["executable"])
    value["digest"] = canonical_digest(value["executable"])
    return value


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        (lambda e: e.update(features=["flow.tryCatch"]), ("flow.tryCatch",)),
        (lambda e: e.update(features=["text.concat", "zz.unknown"]), ("zz.unknown",)),
        (lambda e: e.update(irVersion="weave/ir-v1alpha9"), ()),
        (lambda e: e.update(irVersion=4), ()),
    ],
    ids=["unknown-feature", "known-and-unknown", "unknown-version", "malformed-version"],
)
def test_unknown_features_and_versions_are_ir_unsupported_not_contract_errors(change, missing):
    with pytest.raises(UnsupportedIR) as refused:
        import_artifact(forged(change), features=TEXT)
    assert refused.value.missing == missing


@pytest.mark.parametrize(
    "change",
    [lambda e: e.update(features="text.concat"), lambda e: e.update(features=[1])],
    ids=["not-a-list", "not-names"],
)
def test_malformed_feature_lists_stay_contract_errors(change):
    with pytest.raises(ValueError) as rejected:
        import_artifact(forged(change), features=TEXT)
    assert not isinstance(rejected.value, UnsupportedIR)


def test_existing_artifacts_import_on_a_platform_without_features():
    plain = envelope({"literal": "Hello"})
    assert "features" not in plain["executable"]
    assert import_artifact(plain, features=()).digest == plain["digest"]


def test_the_check_ignores_shapes_validation_owns():
    require_supported_ir(None, ())
    require_supported_ir([], ())


def test_the_api_answer_names_the_missing_features():
    error = ir_unsupported(("text.concat", "text.join"))
    assert (error.status, error.code) == (422, "WV-IR-UNSUPPORTED")
    assert error.result == {"reason": "ir_unsupported", "missing_features": ["text.concat", "text.join"]}
