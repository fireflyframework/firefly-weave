# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Provider contracts reject ambiguous authority and bound semantic events."""

import importlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError


def contracts():
    return importlib.import_module("firefly_weave.contracts.providers")


def request(**changes):
    return dict(
        name="inbox",
        provider="fixture",
        package="fixture-provider",
        package_version="1.0.0",
        schema_digest="a" * 64,
        connection_revision_id=uuid4(),
        policy={"account_id": "a"},
        kind="run",
        activation_id=uuid4(),
        **changes,
    )


def test_provider_request_rejects_owner_injection_and_ambiguous_target():
    c = contracts()
    with pytest.raises(ValidationError):
        c.ProviderSourceRequest(**request(principal_id=uuid4()))
    with pytest.raises(ValidationError):
        c.ProviderSourceRequest(**request(run_id=uuid4(), signal="incoming"))


def test_ignore_requires_safe_reason_and_payload_is_bounded():
    c = contracts()
    event = dict(event_id="e1", kind="message", payload={"text": "hi"}, disposition="ignore")
    with pytest.raises(ValidationError):
        c.ProviderEvent(**event)
    with pytest.raises(ValidationError):
        c.ProviderEvent(event_id="e1", kind="message", payload={"text": "x" * 1048577})
    assert c.ProviderEvent(**event, reason="unsupported").disposition == "ignore"


def test_semantic_fingerprint_excludes_delivery_time():
    c = contracts()
    event = c.ProviderEvent(event_id="e1", kind="message", account_id="a", payload={"text": "hi"})
    assert event.fingerprint == c.ProviderEvent(**event.model_dump()).fingerprint
    assert event.fingerprint != event.model_copy(update={"kind": "delivery"}).fingerprint
    with pytest.raises(ValidationError):
        c.ProviderEvent(**event.model_dump(), received_at=datetime.now(UTC))


def test_ingress_ack_has_bounded_safe_transport_contract():
    c = contracts()
    with pytest.raises(ValidationError):
        c.ProviderIngressResponse(status_code=302, body="redirect")
    with pytest.raises(ValidationError):
        c.ProviderIngressResponse(body="x" * 65537)


def test_distribution_version_is_exact_ascii_and_persisted_adapter_pin_required():
    c = contracts()
    value = request()
    value["package_version"] = "0.1.0a1"
    assert c.ProviderSourceRequest(**value).package_version == "0.1.0a1"
    for invalid in (" 1.0", "1.0\n", "é1", "1" * 129, 1):
        with pytest.raises(ValidationError):
            c.ProviderSourceRequest(**{**value, "package_version": invalid})
    assert c.ProviderSource.model_fields["adapter_version"].is_required()
    with pytest.raises(ValidationError):
        c.ProviderSourceRequest(**{**value, "adapter_version": 1})


def test_entire_source_request_obeys_existing_document_budget():
    with pytest.raises(ValidationError):
        contracts().ProviderSourceRequest(**{**request(), "name": "x" * 1048577})


def test_dispatch_contract_defaults_to_all_and_changes_schema_pin():
    c = contracts()
    schemas = {"message": {"type": "object"}, "lifecycle": {"type": "string"}}
    assert c.provider_dispatch_kinds(schemas) == ("lifecycle", "message")
    assert c.provider_schema_digest(schemas) == c.provider_schema_digest(schemas, ["message", "lifecycle"])
    assert c.provider_schema_digest(schemas) != c.provider_schema_digest(schemas, ["message"])
    for invalid in ([], ["message", "message"], ["unknown"]):
        with pytest.raises(ValueError):
            c.provider_dispatch_kinds(schemas, invalid)
