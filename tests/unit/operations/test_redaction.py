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

"""Evidence classification and redaction keep secrets out of operational output."""

import pytest

from firefly_weave.compiler.schemas import validate_payload, validate_schema


@pytest.mark.parametrize("marker", ["x-secret", "writeOnly"])
@pytest.mark.parametrize("value", [None, "", 0, False, [], {}, "secret://reference"])
def test_presence_is_not_truthiness(marker, value):
    schema = {"type": "object", "properties": {"private": {marker: True}}}
    assert not validate_payload(schema, {}, {})
    issues = validate_payload(schema, {"private": value}, {})
    assert [i.code for i in issues] == ["WV-SCHEMA-SECRET_VALUE"]
    assert issues[0].path == ""


@pytest.mark.parametrize("wrapper", ["allOf", "anyOf", "oneOf", "if", "not"])
def test_compositions_are_conservative(wrapper):
    child = {"properties": {"private": {"x-secret": True}}}
    schema = {wrapper: [child, {}] if wrapper.endswith("Of") else child}
    assert validate_payload(schema, {"private": None}, {})[0].code == "WV-SCHEMA-SECRET_VALUE"


def test_bundle_local_array_and_literals():
    bundle = {"safe": {"$defs": {"credential": {"writeOnly": True}}}}
    schema = {"type": "array", "items": {"$ref": "safe#/$defs/credential"}}
    assert not validate_schema(schema, bundle)
    assert not validate_payload(schema, [], bundle)
    assert validate_payload(schema, [None], bundle)[0].code == "WV-SCHEMA-SECRET_VALUE"
    for keyword, value in (("default", None), ("const", ""), ("examples", [None]), ("enum", [None])):
        assert validate_schema({"$ref": "safe#/$defs/credential", keyword: value}, bundle)
    assert not validate_schema({"properties": {"private": {"x-secret": True}}, "default": {}}, {})


def test_projection_has_explicit_unavailable_and_safe_paths():
    from firefly_weave.operations.redaction import project

    assert project(None, {}, {}).available
    hidden = project({"sensitive-key": "value"}, {"additionalProperties": {"x-secret": True}}, {})
    assert not hidden.available
    assert hidden.omissions[0].path == ""
    assert "sensitive-key" not in hidden.model_dump_json()
    assert project("[REDACTED]", {}, {}).value == "[REDACTED]"
    assert not project({}, {"$ref": "unknown"}, {}).available


def test_bounded_profile_remains_offline():
    from firefly_weave.compiler.schema_profile import SchemaLimits

    assert validate_payload({}, [None] * 20, {}, limits=SchemaLimits(max_document_nodes=10))
    assert validate_payload({"$ref": "https://unreachable.invalid"}, {}, {})
    assert validate_schema({"$defs": {"loop": {"$ref": "#/$defs/loop"}}}, {})


def test_source_and_incomplete_draft_classification():
    from firefly_weave.compiler.admission import admit_authoring
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.compiler.schemas import _Failure

    catalog = CatalogSnapshot.empty()
    admit_authoring({"incomplete": True}, catalog)
    admit_authoring(
        {"kind": "Workflow", "spec": {"outputSchema": {"properties": {"password": {"x-secret": True}}}}}, catalog
    )
    with pytest.raises(_Failure):
        admit_authoring(
            {"kind": "Workflow", "spec": {"outputSchema": {"x-secret": True}, "output": {"literal": "canary"}}}, catalog
        )
    with pytest.raises(_Failure):
        admit_authoring(
            {
                "kind": "Workflow",
                "spec": {"steps": [{"kind": "action", "uses": "unknown@1.0.0", "with": {"literal": "canary"}}]},
            },
            catalog,
        )
    admit_authoring(
        {
            "kind": "Workflow",
            "spec": {"steps": [{"kind": "action", "uses": "unknown@1.0.0", "with": {"ref": "/input"}}]},
        },
        catalog,
    )


def test_imported_artifact_rejects_secret_literal(worker_runtime_fixture):
    import json

    from firefly_weave.compiler.api import compile_source, import_artifact
    from firefly_weave.compiler.canonical import canonical_digest

    result = compile_source(
        worker_runtime_fixture["action"].model_dump(by_alias=True),
        format="object",
        catalog=worker_runtime_fixture["catalog"],
    )
    value = json.loads(result.artifact.to_bytes())
    value["executable"]["spec"]["inputSchema"] = {"x-secret": True, "default": "canary"}
    value["digest"] = canonical_digest(value["executable"])
    with pytest.raises(ValueError):
        import_artifact(value)


def test_default_operational_projection_omits_freeform_and_payload_proofs():
    from firefly_weave.operations.redaction import project_operational_record

    projection = project_operational_record(
        {
            "id": "receipt-id",
            "code": "SAFE_CODE",
            "actor_id": "actor",
            "reason": "private",
            "evidence_reference": "private",
            "accepted_output_hash": "private",
            "output": "private",
        }
    )
    assert projection.value == {"id": "receipt-id", "code": "SAFE_CODE", "actor_id": "actor"}
    assert "private" not in projection.model_dump_json()


def test_unavailable_kernel_never_executes_payloads():
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.kernel import KernelError, transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    state = RunState(unavailable=True)
    for kind in ("started", "task_completed", "incident_resolved", "wait_elapsed"):
        with pytest.raises(KernelError):
            transition(state, RuntimeEvent(id=uuid4(), type=kind, timestamp=datetime.now(UTC), sequence=1), None)


def test_incomplete_reference_binding_is_editable_but_not_executable():
    from firefly_weave.compiler.admission import admit_authoring
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.compiler.schemas import _Failure

    document = {"spec": {"outputSchema": {"writeOnly": True}, "output": {"ref": "/input"}}}
    admit_authoring(document, CatalogSnapshot.empty(), incomplete=True)
    with pytest.raises(_Failure):
        admit_authoring(document, CatalogSnapshot.empty())
    document["spec"]["output"] = {"literal": "classified"}
    with pytest.raises(_Failure):
        admit_authoring(document, CatalogSnapshot.empty(), incomplete=True)


def test_computed_output_rejection_discards_partial_steps_but_keeps_overall_deadline():
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent

    result = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "computed", "version": "1.0.0"},
            "spec": {
                "inputSchema": {},
                "outputSchema": {"properties": {"private": {"x-secret": True}}},
                "timeoutSeconds": 60,
                "steps": [{"id": "copy", "kind": "transform", "value": {"ref": "/input"}}],
                "output": {"ref": "/steps/copy/output"},
            },
        },
        format="object",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok
    event = RuntimeEvent(id=uuid4(), type="started", timestamp=datetime.now(UTC), sequence=1)
    transition_result = transition(RunState(input={"private": "unclassified-at-ingress"}), event, result.artifact)
    assert transition_result.state.incident == "WV-SCHEMA-SECRET_VALUE"
    assert transition_result.state.steps == {} and transition_result.state.output is None
    assert not transition_result.steps
    assert len(transition_result.commands) == 1
    assert isinstance(transition_result.commands[0], Deadline) and transition_result.commands[0].node_id == "@run"


@pytest.mark.parametrize(
    "schema",
    [
        {"allOf": [{"x-secret": True}, {"default": "canary"}]},
        {"properties": {"p": {"x-secret": True}}, "allOf": [{"properties": {"p": {"default": "canary"}}}]},
        {
            "$defs": {"marked": {"writeOnly": True}, "literal": {"examples": ["canary"]}},
            "allOf": [{"$ref": "#/$defs/marked"}, {"$ref": "#/$defs/literal"}],
        },
        {"items": {"writeOnly": True}, "allOf": [{"prefixItems": [{"const": "canary"}]}]},
    ],
)
def test_composed_schema_literals_use_effective_location(schema):
    assert validate_schema(schema, {})[0].code == "WV-SCHEMA-SECRET_VALUE"


@pytest.mark.parametrize("nested", [False, True])
def test_mixed_operator_binding_cannot_hide_visible_literal(nested):
    from firefly_weave.compiler.admission import admit_authoring
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.compiler.schemas import _Failure

    expression = {"op": {"name": "coalesce", "args": [{"ref": "/input"}, {"literal": {"p": "canary"}}]}}
    schema = {"properties": {"p": {"x-secret": True}}}
    if nested:
        expression = {"array": [expression]}
        schema = {"items": schema}
    document = {"spec": {"outputSchema": schema, "output": expression}}
    with pytest.raises(_Failure):
        admit_authoring(document, CatalogSnapshot.empty(), incomplete=True)


@pytest.mark.parametrize("case", ["composed", "coalesce"])
def test_effective_literal_rejected_by_compiler_and_import(case):
    import json

    from firefly_weave.compiler.api import compile_source, import_artifact
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.compiler.catalog import CatalogSnapshot

    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "governed", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [{"id": "copy", "kind": "transform", "value": {"ref": "/input"}}],
            "output": {"ref": "/input"},
        },
    }
    safe = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    envelope = json.loads(safe.artifact.to_bytes())
    if case == "composed":
        schema = {"allOf": [{"x-secret": True}, {"default": "canary"}]}
        document["spec"]["inputSchema"] = schema
        envelope["executable"]["inputSchema"] = canonical_digest(schema)
        envelope["executable"]["schemas"][canonical_digest(schema)] = schema
    else:
        schema = {"properties": {"p": {"writeOnly": True}}}
        expression = {"op": {"name": "coalesce", "args": [{"ref": "/input"}, {"literal": {"p": "canary"}}]}}
        document["spec"]["outputSchema"] = schema
        document["spec"]["output"] = expression
        envelope["executable"]["outputSchema"] = canonical_digest(schema)
        envelope["executable"]["schemas"][canonical_digest(schema)] = schema
        next(n for n in envelope["executable"]["graph"]["nodes"] if n["kind"] == "end")["output"] = expression
    assert not compile_source(document, format="object", catalog=CatalogSnapshot.empty()).ok
    envelope["digest"] = canonical_digest(envelope["executable"])
    from firefly_weave.compiler.ir import ArtifactEnvelope

    ArtifactEnvelope.model_validate(envelope)
    with pytest.raises(ValueError):
        import_artifact(envelope)
    from firefly_weave.runtime.admission import unavailable

    assert unavailable({"state": {"admission_policy": "classified-v1"}, "artifact": envelope})


def test_effective_literals_keep_unmarked_locations_and_reference_only_drafts():
    from firefly_weave.compiler.admission import admit_authoring
    from firefly_weave.compiler.catalog import CatalogSnapshot

    schema = {"properties": {"p": {"x-secret": True}}, "allOf": [{"properties": {"q": {"default": "ordinary"}}}]}
    assert not validate_schema(schema, {})
    document = {
        "spec": {
            "outputSchema": schema,
            "output": {"op": {"name": "coalesce", "args": [{"ref": "/input"}, {"ref": "/steps/missing/output"}]}},
        }
    }
    admit_authoring(document, CatalogSnapshot.empty(), incomplete=True)
    document["spec"]["output"]["op"]["args"][1] = {"literal": {"q": "ordinary"}}
    admit_authoring(document, CatalogSnapshot.empty(), incomplete=True)
