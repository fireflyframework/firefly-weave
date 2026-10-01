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

"""Hand-authored expected outcomes shared by public compiler and CLI transports."""

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot

CASES = json.loads((Path(__file__).parents[1] / "fixtures/compiler/corpus.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_compiler_corpus_through_library_and_cli(tmp_path, case):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(case["definition"]))
    lock = tmp_path / "catalog.json"
    lock_value = case.get("catalog", {"definitions": [], "tasks": [], "adapters": [], "schemas": {}})
    lock.write_text(json.dumps(lock_value))
    result = compile_source(
        source.read_bytes(), format="json", catalog=CatalogSnapshot.from_lock(lock_value), filename=str(source)
    )
    assert result.ok is case["ok"]
    assert {d.code for d in result.diagnostics} == set(case["codes"])
    cli = importlib.import_module("firefly_weave.cli.main").cli
    response = CliRunner().invoke(cli, ["workflow", "compile", str(source), "--catalog", str(lock), "--output", "json"])
    assert response.exit_code == (0 if case["ok"] else 1), response.output
    assert response.output.strip().encode() == result.to_bytes()
    if case["ok"]:
        assert import_artifact(json.loads(response.output)["artifact"]).digest == result.artifact.digest


def test_published_catalog_lock_shape_and_semantic_boundary(fixture_dir):
    from jsonschema import Draft202012Validator

    from firefly_weave.compiler.schemas import validate_contract_payload
    from firefly_weave.contracts.schema_export import export_schemas

    lock = json.loads((fixture_dir / "catalog/onboarding.lock.json").read_text())
    schema = export_schemas()["catalog-lock"]
    assert not list(Draft202012Validator(schema).iter_errors(lock))
    assert not validate_contract_payload("catalog-lock", lock)
    lock["definitions"][0]["digest"] = "0" * 64
    assert validate_contract_payload("catalog-lock", lock)
