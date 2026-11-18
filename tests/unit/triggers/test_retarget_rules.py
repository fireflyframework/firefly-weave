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

"""Moving a trigger to another activation: the rules every source shares, before anything is written."""

import json
from uuid import uuid4

import pytest

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.definitions.models import CatalogError
from firefly_weave.triggers.retarget import (
    ActivationFacts,
    RetargetOutcome,
    email_input_schema,
    email_rule,
    mapping_rule,
    payload_rule,
    require_revision,
    retarget_audit,
    same_workflow,
)

ENVIRONMENT = uuid4()
ORDER = {
    "type": "object",
    "required": ["order_id"],
    "properties": {"order_id": {"type": "string"}, "amount": {"type": "integer"}},
    "additionalProperties": False,
}
CHANGED = {**ORDER, "required": ["order_id", "amount"]}


def facts(workflow="orders", *, environment=ENVIRONMENT, test=False) -> ActivationFacts:
    return ActivationFacts(workflow=workflow, environment_id=environment, test=test)


def refusal(call) -> CatalogError:
    with pytest.raises(CatalogError) as raised:
        call()
    return raised.value


def test_a_version_of_the_same_workflow_in_the_same_environment_is_accepted():
    same_workflow(facts(), facts())


def test_another_workflow_is_refused_with_a_hint():
    error = refusal(lambda: same_workflow(facts(), facts("invoices")))
    assert (error.status, error.code) == (409, "WV-TRIGGER-WORKFLOW")
    assert error.message == "Retarget it to a version of the same workflow"


@pytest.mark.parametrize(
    "target", [None, facts(environment=uuid4()), facts(test=True)], ids=["missing", "other environment", "test"]
)
def test_a_missing_foreign_or_test_activation_is_not_found(target):
    error = refusal(lambda: same_workflow(facts(), target))
    assert (error.status, error.code) == (404, "WV-NOT-FOUND")


def test_a_payload_schema_equal_to_the_new_input_is_accepted():
    payload_rule(ORDER, current_input=canonical_digest(CHANGED), new_input=canonical_digest(ORDER))


def test_an_unchanged_input_is_accepted_whatever_the_payload_schema():
    # A trigger created without a payload schema admits what the version's own input admits.
    payload_rule({}, current_input=canonical_digest(ORDER), new_input=canonical_digest(ORDER))


def test_a_changed_input_is_refused_with_a_hint():
    error = refusal(
        lambda: payload_rule(ORDER, current_input=canonical_digest(ORDER), new_input=canonical_digest(CHANGED))
    )
    assert (error.status, error.code) == (409, "WV-TRIGGER-SCHEMA-CHANGED")
    assert error.message == "Create a new trigger for the changed input"


MESSAGE = {
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}, "chat": {"type": "integer"}},
}


def test_a_mapping_whose_output_the_new_input_accepts_is_accepted():
    target = {"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}}
    mapping = {"object": {"body": {"ref": "/payload/text"}}}
    mapping_rule(mapping, [MESSAGE], target, {})


def test_a_mapping_whose_output_the_new_input_doesnt_accept_is_refused():
    target = {"type": "object", "required": ["body"], "properties": {"body": {"type": "integer"}}}
    mapping = {"object": {"body": {"ref": "/payload/text"}}}
    error = refusal(lambda: mapping_rule(mapping, [MESSAGE], target, {}))
    assert (error.status, error.code) == (409, "WV-TRIGGER-SCHEMA-CHANGED")


def test_the_mapping_must_fit_every_event_kind_the_source_dispatches():
    target = {"type": "object", "required": ["body"], "properties": {"body": {"type": "string"}}}
    mapping = {"object": {"body": {"ref": "/payload/text"}}}
    numeric = {**MESSAGE, "properties": {"text": {"type": "integer"}}}
    error = refusal(lambda: mapping_rule(mapping, [MESSAGE, numeric], target, {}))
    assert error.code == "WV-TRIGGER-SCHEMA-CHANGED"


def test_an_input_that_accepts_every_email_message_is_accepted():
    email_rule(
        {
            "type": "object",
            "required": ["sender", "subject"],
            "properties": {"sender": {"type": "string"}, "subject": {"type": "string"}, "text": {"type": "string"}},
        },
        {},
    )


def test_an_input_that_requires_a_field_messages_lack_is_refused():
    required = {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}
    error = refusal(lambda: email_rule(required, {}))
    assert (error.status, error.code) == (409, "WV-TRIGGER-SCHEMA-CHANGED")


def test_an_input_that_types_a_message_field_differently_is_refused():
    error = refusal(lambda: email_rule({"type": "object", "properties": {"subject": {"type": "integer"}}}, {}))
    assert error.code == "WV-TRIGGER-SCHEMA-CHANGED"


def test_a_retarget_needs_the_revision_it_read():
    require_revision(3, 3)
    missing = refusal(lambda: require_revision(None, 3))
    assert (missing.status, missing.code) == (422, "WV-ETAG")
    stale = refusal(lambda: require_revision(2, 3))
    assert (stale.status, stale.code) == (409, "WV-REVISION")


def test_the_audit_names_identities_and_nothing_from_schemas_or_mappings():
    outcome = RetargetOutcome(
        kind="kafka",
        resource_id=uuid4(),
        from_activation_id=uuid4(),
        to_activation_id=uuid4(),
        from_principal_id=uuid4(),
        to_principal_id=uuid4(),
        from_binding_id=uuid4(),
        to_binding_id=uuid4(),
    )
    details = retarget_audit(outcome)
    assert details == {
        "kind": "kafka",
        "from_activation_id": str(outcome.from_activation_id),
        "to_activation_id": str(outcome.to_activation_id),
        "from_principal_id": str(outcome.from_principal_id),
        "to_principal_id": str(outcome.to_principal_id),
        "from_binding_id": str(outcome.from_binding_id),
        "to_binding_id": str(outcome.to_binding_id),
    }


def test_a_source_without_a_binding_audits_no_binding():
    outcome = RetargetOutcome(
        kind="webhook",
        resource_id=uuid4(),
        from_activation_id=uuid4(),
        to_activation_id=uuid4(),
        from_principal_id=uuid4(),
        to_principal_id=uuid4(),
    )
    assert set(retarget_audit(outcome)) == {
        "kind",
        "from_activation_id",
        "to_activation_id",
        "from_principal_id",
        "to_principal_id",
    }


def test_the_email_input_schema_is_a_valid_schema_without_references():
    schema = email_input_schema()
    assert not validate_schema(schema, {})
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    assert set(schema["required"]) >= {"sender", "to", "subject", "text"}
