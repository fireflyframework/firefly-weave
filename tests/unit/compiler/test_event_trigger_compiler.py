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


"""Compiling workflows that platform events start: the event as their input, checked against what they accept."""

import json
from datetime import timedelta, timezone
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from firefly_weave.compiler import platform_events
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.language_support import COMPILED_FEATURES
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.platform_catalog import platform_event_view, platform_schema_bundle
from firefly_weave.compiler.schemas import validate_payload, validate_schema
from firefly_weave.contracts.definitions import NAME_PATTERN, load_definition
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN
from firefly_weave.contracts.language import language_manifest
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA
from firefly_weave.contracts.transactions import TRANSACTION_EVENT_TYPES, TRANSACTION_STATES, UNDO_MODES

EVENT = {"$ref": PLATFORM_EVENT_SCHEMA}
# The view's bytes are frozen: artifacts pin it by digest, so a change to the event is a new view, never an edit.
VIEW_DIGEST = "c3aca992120f55f6e1ca7fb835b14a73b5fa26454567d90061658448adde9c84"
# The example's artifact digest on main before platform events compiled: it must not change.
EXAMPLE_DIGEST = "be79539c9e245ac3639eed9220443ce136c76edff1945c70e62626394ec31583"
FIXTURE = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text())
CATALOG = CatalogSnapshot.from_definitions(
    [load_definition(item) for item in FIXTURE["definitions"]], tasks=FIXTURE["tasks"]
)


def workflow(input_schema=None, **spec):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "notify-team", "version": "1.0.0"},
        "spec": {
            "inputSchema": EVENT if input_schema is None else input_schema,
            "outputSchema": {},
            "steps": [],
            "output": {"ref": "/input/transaction/business_key"},
            "eventTrigger": {"events": ["transaction.in_doubt"]},
            **spec,
        },
    }


def compiled(document, catalog=None):
    return compile_source(document, format="object", catalog=CatalogSnapshot.empty() if catalog is None else catalog)


def findings(result):
    return [(d.code.removeprefix("WV-COMP-"), d.path, d.severity) for d in result.diagnostics]


def executable(result) -> dict:
    return json.loads(result.artifact.to_bytes())["executable"]


def test_a_workflow_that_accepts_platform_events_compiles():
    result = compiled(workflow(output={"literal": None}))
    assert result.ok and findings(result) == []
    value = executable(result)
    assert "flow.eventTrigger" not in value.get("features", [])
    schemas = [(item["reference"], item["digest"]) for item in value["dependencies"] if item["kind"] == "Schema"]
    assert schemas == [(PLATFORM_EVENT_SCHEMA, FrozenDocument.from_value(platform_event_view()).digest)]


def test_only_artifacts_that_name_the_platform_event_carry_it():
    example = parse_source(Path("examples/language/move-between-systems.workflow.yaml").read_text(), format="yaml")
    result = compiled(example.value, CATALOG)
    assert result.ok and result.artifact.digest == EXAMPLE_DIGEST
    assert compiled(workflow()).ok


def test_an_input_schema_the_event_cannot_fill_is_a_type_mismatch():
    result = compiled(workflow({"type": "object", "required": ["orderTotal"]}, output={"literal": None}))
    assert findings(result) == [("TYPE_MISMATCH", "/spec/inputSchema", "error")]
    nullable = {"type": "object", "properties": {"transaction": {"properties": {"business_key": {"type": "string"}}}}}
    assert findings(compiled(workflow(nullable, output={"literal": None}))) == [
        ("TYPE_MISMATCH", "/spec/inputSchema", "error")
    ]


def test_an_input_schema_the_compiler_cannot_prove_warns():
    narrowed = {
        "type": "object",
        "properties": {"transaction": {"type": "object", "properties": {"instance_key": {"pattern": "^move$"}}}},
    }
    result = compiled(workflow(narrowed, output={"literal": None}))
    assert result.ok and findings(result) == [("UNKNOWN_COMPATIBILITY", "/spec/inputSchema", "warning")]
    assert findings(compiled(workflow({}, output={"literal": None}))) == []


def test_only_the_platforms_own_event_schema_is_the_platform_event():
    other = {"type": "object"}
    catalog = CatalogSnapshot.from_definitions([], schema_bundle={PLATFORM_EVENT_SCHEMA: other})
    result = compiled(workflow(output={"literal": None}), catalog)
    assert not result.ok
    assert ("RESERVED_SCHEMA", "/spec/inputSchema/$ref", "error") in findings(result)
    plain = compiled(workflow({}, output={"literal": None}), catalog)
    assert findings(plain) == [("RESERVED_SCHEMA", "", "error")]


def guarded(result) -> list[tuple[str, str]]:
    return [(item["path"], item["purpose"]) for item in executable(result)["guards"]]


@pytest.mark.parametrize(
    "pointer", ["/input/transaction/business_key", "/input/transaction/workflow/name", "/input/type"]
)
def test_reading_a_required_event_field_is_present(pointer):
    result = compiled(workflow(output={"ref": pointer}))
    assert result.ok and findings(result) == []
    assert ("/spec/output", "reference_presence") not in guarded(result)


def test_an_optional_event_field_still_warns():
    fields = {"key": {"ref": "/input/transaction/business_key"}, "incident": {"ref": "/input/transaction/incident_id"}}
    result = compiled(workflow(output={"object": fields}))
    assert result.ok and findings(result) == [("REFERENCE_PRESENCE", "/spec/output/object/incident", "warning")]
    assert [path for path, purpose in guarded(result) if purpose == "reference_presence"] == [
        "/spec/output/object/incident"
    ]


def test_an_unknown_event_field_is_missing():
    result = compiled(workflow(output={"ref": "/input/transaction/nope"}))
    assert findings(result) == [("MISSING_REFERENCE", "/spec/output", "error")]


# A workflow whose input is a project's own schema by name, compiled on main before the platform event's fields
# became readable: only platform schema names are looked through, so it keeps its warning and its bytes.
SHARED = {
    "type": "object",
    "properties": {"key": {"type": "string"}, "note": {"type": "string"}},
    "required": ["key"],
    "additionalProperties": False,
}
SHARED_DIGEST = "e270b3f3e0db7c6f55ab2d26fa5adc15ae9f78390160ce55637e7bf50a224565"


def test_a_shared_schema_reference_keeps_its_warning():
    catalog = CatalogSnapshot.from_definitions([], schema_bundle={"notify.request-v1": SHARED})
    document = workflow({"$ref": "notify.request-v1"}, output={"ref": "/input/key"})
    del document["spec"]["eventTrigger"]
    result = compiled(document, catalog)
    assert findings(result) == [("REFERENCE_PRESENCE", "/spec/output", "warning")]
    assert result.artifact.digest == SHARED_DIGEST


def test_only_the_platforms_own_bytes_give_the_view():
    view, bundle = platform_events.platform_view, platform_schema_bundle()
    assert view(EVENT, bundle) == platform_event_view()
    assert view(EVENT, {PLATFORM_EVENT_SCHEMA: {"type": "object"}}) is None
    assert view(EVENT, {}) is None
    assert view({"$ref": PLATFORM_EVENT_SCHEMA, "title": "Event"}, bundle) is None
    assert view({}, bundle) is None


def caller(*steps, **spec):
    """A workflow that never names the platform event itself."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "order-intake", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": {"literal": None}, **spec},
    }


def schema_dependencies(result) -> list[tuple[str, str]]:
    return [
        (item["reference"], item["digest"]) for item in executable(result)["dependencies"] if item["kind"] == "Schema"
    ]


def errors(result):
    return [finding for finding in findings(result) if finding[2] == "error"]


def test_a_workflow_can_call_a_workflow_that_platform_events_start():
    called = workflow(output={"literal": None}, callable={})
    catalog = CatalogSnapshot.from_definitions([load_definition(called)])
    step = {"id": "react", "kind": "callWorkflow", "uses": "notify-team@1.0.0", "with": {"ref": "/input"}}
    result = compiled(caller(step), catalog)
    assert errors(result) == [] and result.ok
    assert schema_dependencies(result) == [(PLATFORM_EVENT_SCHEMA, VIEW_DIGEST)]


ACTION = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "record-event", "version": "1.0.0"},
    "spec": {
        "implementation": {"kind": "worker", "taskType": "record-event", "taskVersion": "1.0.0"},
        "sideEffect": "idempotency_key",
        "timeoutSeconds": 30,
        "inputSchema": EVENT,
        "outputSchema": {},
    },
}
TASK = {
    "taskType": "record-event",
    "taskVersion": "1.0.0",
    "inputSchema": EVENT,
    "outputSchema": {},
    "sideEffect": "idempotency_key",
    "timeoutSeconds": 30,
}
TABLE = {
    "apiVersion": "weave/v1alpha1",
    "kind": "DecisionTable",
    "metadata": {"name": "route-event", "version": "1.0.0"},
    "spec": {
        "inputSchema": EVENT,
        "outputSchema": {"type": "string"},
        "hitPolicy": "first",
        "rules": [{"id": "any", "when": {"literal": True}, "output": {"literal": "notify"}}],
    },
}


@pytest.mark.parametrize(
    ("definition", "tasks", "kind"),
    [(ACTION, [TASK], "action"), (TABLE, [], "decisionTable")],
    ids=["action and task capability", "decision table"],
)
def test_a_workflow_can_use_a_step_whose_schema_is_the_platform_event(definition, tasks, kind):
    catalog = CatalogSnapshot.from_definitions([load_definition(definition)], tasks=tasks)
    reference = f"{definition['metadata']['name']}@1.0.0"
    result = compiled(caller({"id": "use", "kind": kind, "uses": reference, "with": {"ref": "/input"}}), catalog)
    assert errors(result) == [] and result.ok
    assert schema_dependencies(result) == [(PLATFORM_EVENT_SCHEMA, VIEW_DIGEST)]


def test_a_schema_that_fixes_values_can_reference_the_platform_event():
    schema = {"type": "object", "properties": {"event": EVENT, "mode": {"enum": ["notify", "skip"]}}}
    result = compiled(caller(outputSchema=schema))
    assert errors(result) == [] and result.ok
    assert schema_dependencies(result) == [(PLATFORM_EVENT_SCHEMA, VIEW_DIGEST)]


def test_a_platform_event_reference_outside_a_schema_adds_no_dependency():
    lookalike = {"$ref": PLATFORM_EVENT_SCHEMA}
    result = compiled(caller(outputSchema={"const": lookalike}, output={"literal": lookalike}))
    assert result.ok and findings(result) == []
    assert schema_dependencies(result) == []
    # A property may be called like a data keyword: its schema is still a schema.
    named = compiled(caller(outputSchema={"properties": {"default": lookalike}}))
    assert errors(named) == [] and schema_dependencies(named) == [(PLATFORM_EVENT_SCHEMA, VIEW_DIGEST)]


def test_the_platform_advertises_platform_event_triggers():
    assert "flow.eventTrigger" in COMPILED_FEATURES and "flow.eventTrigger" in ADVERTISED_FEATURES
    trigger = next(entry for entry in language_manifest().workflow_fields if entry.name == "eventTrigger")
    assert (trigger.feature, trigger.studio) == ("flow.eventTrigger", "pending")


def test_the_platform_event_view_is_frozen_and_fits_the_schema_profile():
    view = platform_event_view()
    assert FrozenDocument.from_value(view).digest == VIEW_DIGEST
    assert validate_schema(view, {}) == ()
    assert not {"$id", "$schema", "$defs"} & set(view)
    assert "$ref" not in json.dumps(view)
    assert platform_event_view() == view and platform_event_view() is not view


NAMES = st.from_regex(rf"^{NAME_PATTERN}$", fullmatch=True).filter(lambda text: len(text) <= 63)
KEYS = st.from_regex(rf"^(?:{INSTANCE_KEY_PATTERN})$", fullmatch=True).filter(lambda text: len(text) <= 512)
CODES = st.from_regex(r"^[A-Z][A-Z0-9_-]{0,63}$", fullmatch=True)
TEXT = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=200)
OFFSETS = st.integers(-14 * 60, 14 * 60).map(lambda minutes: timezone(timedelta(minutes=minutes)))
STEP = st.fixed_dictionaries(
    {"step": NAMES, "instance_key": KEYS, "how": st.sampled_from(UNDO_MODES)},
    optional={
        "phase": st.sampled_from(["confirm", "cancel", "undo"]),
        "attempts": st.integers(0, 2**53 - 1),
    },
)
EVENTS = st.fixed_dictionaries(
    {
        "version": st.just("weave/platform-event-v1"),
        "event_id": st.uuids().map(str),
        "type": st.sampled_from(TRANSACTION_EVENT_TYPES),
        # Offsets in whole minutes, as RFC 3339 times carry them.
        "emitted_at": st.datetimes(timezones=OFFSETS).map(lambda value: value.isoformat()),
        "transaction": st.fixed_dictionaries(
            {
                "workflow": st.fixed_dictionaries(
                    {
                        "name": NAMES,
                        "version": st.from_regex(r"^(0|[1-9][0-9]{0,3})\.(0|[1-9])\.[0-9]$", fullmatch=True),
                    }
                ),
                "run_id": st.uuids().map(str),
                "instance_key": KEYS,
                "business_key": st.none() | TEXT,
                "state": st.sampled_from(TRANSACTION_STATES),
                "steps": st.fixed_dictionaries(
                    {},
                    optional={name: st.lists(STEP, max_size=3) for name in ("done", "undone", "pending")},
                ),
                "test": st.booleans(),
            },
            optional={
                "cause": st.fixed_dictionaries({"code": CODES}, optional={"step": NAMES, "origin_code": CODES}),
                "incident_id": st.uuids().map(str),
            },
        ),
    }
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(EVENTS)
def test_every_platform_event_fits_the_view(value):
    event = PlatformEvent.model_validate_json(json.dumps(value)).model_dump(mode="json")
    assert validate_payload(platform_event_view(), event, {}) == ()
