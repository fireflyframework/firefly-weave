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


"""Platform-event triggers, their starts, and the platform's resolve action, as published before they are served."""

import json
from typing import get_args
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from firefly_weave.compiler.catalog import TaskCapability
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.event_triggers import EventTrigger, EventTriggerRequest, EventTriggerStart
from firefly_weave.contracts.operations import IncidentResolution, RecordedResolution
from firefly_weave.contracts.public import Page
from firefly_weave.contracts.runtime import RecoveryReport
from firefly_weave.contracts.surface import ENVIRONMENT, OPERATIONS
from firefly_weave.contracts.transaction_resolve import (
    RESOLVE_ACTION,
    RESOLVE_TASK_TYPE,
    resolve_action_definition,
    resolve_input_schema,
    resolve_output_schema,
    resolve_task_capability,
)

ACTIVATION = "5b0f3c0e-8f0e-4b8a-9c39-3c1f7c0b8a11"
REQUEST = {"name": "notify", "activation_id": ACTIVATION, "events": ["transaction.in_doubt"], "may_reconcile": False}
STARTED = {
    "id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c01",
    "trigger_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c02",
    "event_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c03",
    "event_type": "transaction.in_doubt",
    "source_run_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c04",
    "root_event_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c07",
    "depth": 1,
    "status": "started",
    "run_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c05",
    "requested_at": "2026-11-15T10:00:00Z",
    "started_at": "2026-11-15T10:00:01Z",
}
RESOLUTION = {"receipt_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c06", "kind": "retry_safe", "reason": "Retried."}


def wire(model, value):
    """Validated as the API reads it: strict models take UUIDs and times as strings only from JSON."""
    return model.model_validate_json(json.dumps(value))


def test_an_event_trigger_request_names_events_and_sources():
    request = wire(EventTriggerRequest, REQUEST)
    assert request.model_dump(mode="json") == REQUEST
    limited = wire(EventTriggerRequest, {**REQUEST, "source_workflows": ["move-between-systems"]})
    assert limited.model_dump(mode="json")["source_workflows"] == ["move-between-systems"]
    six = ["transaction.completed", "transaction.compensated", "transaction.cancelled"]
    six += ["transaction.in_doubt", "transaction.reconciled", "transaction.force_ended"]
    assert wire(EventTriggerRequest, {**REQUEST, "events": six}).events == six
    for refused in (
        {**REQUEST, "events": ["transaction.in_doubt", "transaction.in_doubt"]},
        {**REQUEST, "events": six * 2 + ["transaction.completed"]},
        {**REQUEST, "events": []},
        {**REQUEST, "events": ["transaction.started"]},
        {**REQUEST, "source_workflows": []},
        {**REQUEST, "source_workflows": ["move", "move"]},
        {**REQUEST, "source_workflows": None},
        {**REQUEST, "name": "not a name"},
    ):
        with pytest.raises(ValidationError):
            wire(EventTriggerRequest, refused)


def test_an_omitted_source_list_publishes_no_null_default():
    # A null default would tell clients they may send null, which the request refuses.
    sources = EventTriggerRequest.model_json_schema()["properties"]["source_workflows"]
    assert "default" not in sources


def test_an_event_trigger_is_its_request_plus_who_bound_it():
    bound = {
        **REQUEST,
        "id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c07",
        "workflow_name": "notify-team",
        "principal_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c08",
        "revision": 1,
        "disabled": False,
        "created_at": "2026-11-15T10:00:00Z",
    }
    assert wire(EventTrigger, bound).model_dump(mode="json") == {**bound, "created_at": "2026-11-15T10:00:00Z"}
    with pytest.raises(ValidationError):
        wire(EventTrigger, {**bound, "revision": 0})


def test_an_event_trigger_start_reports_its_outcome_without_values():
    assert wire(EventTriggerStart, STARTED).model_dump(mode="json") == STARTED
    assert "payload" not in EventTriggerStart.model_json_schema()["properties"]
    rejected = {key: value for key, value in STARTED.items() if key not in {"run_id", "started_at"}}
    rejected |= {"status": "rejected", "code": "WV-TRIGGER-START", "cause": "WV-FORBIDDEN"}
    assert wire(EventTriggerStart, rejected).model_dump(mode="json") == rejected
    requested = {key: value for key, value in rejected.items() if key not in {"code", "cause"}}
    assert set(wire(EventTriggerStart, {**requested, "status": "requested"}).model_dump()) == set(requested)
    for code in ("WV-TRIGGER-DISABLED", "WV-TRIGGER-EVENT-DEPTH", "WV-TRIGGER-CHAIN-BUDGET"):
        skipped = {**requested, "status": "skipped", "code": code}
        assert wire(EventTriggerStart, skipped).model_dump(mode="json") == skipped
    expired = {**requested, "status": "rejected", "code": "WV-TRIGGER-START-EXPIRED"}
    assert wire(EventTriggerStart, expired).model_dump(mode="json") == expired
    for refused in (
        {**STARTED, "depth": 0},
        {**STARTED, "run_id": None},
        {key: value for key, value in STARTED.items() if key != "run_id"},
        {**rejected, "run_id": STARTED["run_id"]},
        {key: value for key, value in rejected.items() if key != "code"},
        {**rejected, "code": "WV-SOMETHING-ELSE"},
        {**rejected, "status": "skipped", "code": "WV-TRIGGER-EVENT-DEPTH", "payload": {}},
    ):
        with pytest.raises(ValidationError):
            wire(EventTriggerStart, refused)


def test_a_start_always_names_the_event_that_began_its_chain():
    assert wire(EventTriggerStart, STARTED).model_dump(mode="json")["root_event_id"] == STARTED["root_event_id"]
    assert "root_event_id" in EventTriggerStart.model_json_schema()["required"]
    with pytest.raises(ValidationError):
        wire(EventTriggerStart, {key: value for key, value in STARTED.items() if key != "root_event_id"})


def test_recovery_reports_event_starts_and_resolutions_only_when_nonzero():
    assert set(RecoveryReport().model_dump()) == {"resumed", "incidents", "timed_out", "elapsed"}
    report = RecoveryReport(event_starts=2, event_starts_rejected=1, resolutions=3).model_dump()
    assert (report["event_starts"], report["event_starts_rejected"], report["resolutions"]) == (2, 1, 3)


def test_a_recorded_resolution_names_the_workflow_run_that_resolved_it():
    run = "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c09"
    named = wire(RecordedResolution, {**RESOLUTION, "workflow_run_id": run})
    assert named.workflow_run_id == UUID(run) and named.model_dump(mode="json")["workflow_run_id"] == run
    assert "workflow_run_id" not in wire(RecordedResolution, RESOLUTION).model_dump(mode="json")


def test_a_person_cannot_name_a_workflow_run_in_a_resolution():
    with pytest.raises(ValidationError) as refused:
        wire(IncidentResolution, {**RESOLUTION, "workflow_run_id": str(uuid4())})
    (error,) = refused.value.errors()
    assert error["type"] == "value_error"
    assert "Only the platform names the workflow run that resolved an incident" in error["msg"]
    request = OPERATIONS["incidents.resolve"].request
    assert request is IncidentResolution
    assert "workflow_run_id" not in request.model_json_schema()["properties"]
    assert "workflow_run_id" in RecordedResolution.model_json_schema()["properties"]
    assert "workflow_run_id" not in wire(IncidentResolution, RESOLUTION).recorded().model_dump(mode="json")


def test_the_platform_resolve_action_is_a_fixed_document():
    definition = load_definition(resolve_action_definition())
    assert isinstance(definition, ActionDefinition)
    assert (definition.metadata.name, definition.metadata.version) == (RESOLVE_ACTION, "1.0.0")
    implementation = definition.spec.implementation
    assert (implementation.kind, implementation.task_type, implementation.task_version) == (
        "worker",
        RESOLVE_TASK_TYPE,
        "1.0.0",
    )
    assert RESOLVE_TASK_TYPE == "weave-platform.transaction-resolve"
    assert (definition.spec.side_effect, definition.spec.timeout_seconds, definition.spec.retry.max_attempts) == (
        "idempotency_key",
        300,
        1,
    )
    schema = definition.spec.input_schema
    assert schema["additionalProperties"] is False and set(schema["required"]) == {"incident_id", "kind", "reason"}
    kinds = get_args(RecordedResolution.model_fields["kind"].annotation)
    assert schema["properties"]["kind"]["enum"] == list(kinds)
    validator = Draft202012Validator(schema)
    incident = str(uuid4())
    assert validator.is_valid({"incident_id": incident, "kind": "terminate", "reason": "Ended by hand."})
    assert not validator.is_valid({"incident_id": incident, "kind": "terminate", "reason": ""})
    assert not validator.is_valid({"incident_id": incident, "kind": "retry_safe", "reason": "x", "extra": 1})
    capability = TaskCapability.model_validate(resolve_task_capability())
    assert (capability.task_type, capability.task_version) == (RESOLVE_TASK_TYPE, "1.0.0")
    assert (capability.input_schema, capability.output_schema) == (resolve_input_schema(), resolve_output_schema())
    assert (capability.side_effect, capability.timeout_seconds) == ("idempotency_key", 300)
    assert definition.spec.output_schema == resolve_output_schema()
    assert resolve_action_definition() == resolve_action_definition()
    assert resolve_action_definition() is not resolve_action_definition()


EVENT_TRIGGERS = ENVIRONMENT + "/event-triggers"
PUBLISHED = {
    "event_triggers.create": (EVENT_TRIGGERS, "POST", EventTrigger, "trigger.manage", EventTriggerRequest, (201,)),
    "event_triggers.list": (EVENT_TRIGGERS, "GET", Page[EventTrigger], "trigger.manage", None, (200,)),
    "event_triggers.read": (EVENT_TRIGGERS + "/{identifier}", "GET", EventTrigger, "trigger.manage", None, (200,)),
    "event_triggers.disable": (
        EVENT_TRIGGERS + "/{identifier}/disable",
        "POST",
        EventTrigger,
        "trigger.manage",
        None,
        (200,),
    ),
    "event_triggers.starts": (
        EVENT_TRIGGERS + "/{identifier}/starts",
        "GET",
        Page[EventTriggerStart],
        "trigger.manage",
        None,
        (200,),
    ),
}


def test_event_trigger_operations_are_served_with_their_published_shapes():
    for identifier, expected in PUBLISHED.items():
        operation = OPERATIONS[identifier]
        actual = (
            operation.path,
            operation.method,
            operation.response,
            operation.capability,
            operation.request,
            operation.statuses,
        )
        assert actual == expected, identifier
        assert operation.served is True, identifier
        assert operation.page is identifier.endswith((".list", ".starts")), identifier
