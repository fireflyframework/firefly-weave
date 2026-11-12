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


"""A compile reads the built-in contract schemas generated once per process, and never shares them."""

from firefly_weave.compiler import schemas
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import schema_export
from firefly_weave.contracts.schema_export import export_schemas

WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "cached", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "object"},
        "outputSchema": {},
        "steps": [{"id": "greet", "kind": "transform", "value": {"literal": "hello"}}],
        "output": {"ref": "/steps/greet/output"},
    },
}


def generated_schemas(monkeypatch) -> list[str]:
    """Records every JSON schema pydantic generates for a built-in contract model."""
    generated: list[str] = []
    generate = schema_export._ContractSchemaGenerator.generate

    def recording(self, schema, mode="validation"):
        generated.append(mode)
        return generate(self, schema, mode)

    monkeypatch.setattr(schema_export._ContractSchemaGenerator, "generate", recording)
    return generated


def test_a_compile_after_the_first_generates_no_contract_schema(monkeypatch):
    compile_source(WORKFLOW, format="object", catalog=CatalogSnapshot.empty())
    generated = generated_schemas(monkeypatch)
    result = compile_source(WORKFLOW, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.diagnostics
    assert generated == []


def test_the_contract_schema_is_the_exported_one():
    assert schemas.contract_schema("workflow") == export_schemas()["workflow"]


def test_changing_a_returned_contract_schema_changes_neither_the_next_one_nor_validation():
    changed = schemas.contract_schema("workflow")
    changed["properties"]["kind"] = {"const": "Something else"}
    changed["required"].append("unknown")
    assert schemas.contract_schema("workflow") == export_schemas()["workflow"]
    assert schemas.validate_contract_payload("workflow", WORKFLOW) == ()
