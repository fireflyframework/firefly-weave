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

"""The published contract of moving triggers between versions, and of reading email sources."""

import pytest

from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.contracts.triggers import RetargetRequest

ENVIRONMENT = "/tenants/{tenant}/projects/{project}/environments/{environment}"
RETARGETS = {
    "triggers.retarget": ("/triggers/{identifier}/retarget", "trigger.manage and run.start", "Trigger"),
    "broker_triggers.retarget": (
        "/broker-triggers/{identifier}/retarget",
        "trigger.manage and run.start and connection.manage and connection.bind",
        "BrokerTrigger",
    ),
    "provider_sources.retarget": (
        "/provider-sources/{identifier}/retarget",
        "trigger.manage and run.start and connection.manage and connection.bind",
        "ProviderSource",
    ),
    "email_sources.retarget": (
        "/email/sources/{identifier}/retarget",
        "email.manage and trigger.manage and connection.bind and run.start",
        "EmailSourceView",
    ),
    "event_triggers.retarget": (
        "/event-triggers/{identifier}/retarget",
        "trigger.manage and run.start and run.read",
        "EventTrigger",
    ),
}
READS = ("triggers.read", "broker_triggers.read", "provider_sources.read", "email_sources.read", "event_triggers.read")


@pytest.mark.parametrize("identifier", sorted(RETARGETS))
def test_each_retarget_is_a_post_that_needs_the_revision_it_read(identifier):
    path, capability, response = RETARGETS[identifier]
    operation = OPERATIONS[identifier]
    assert (operation.method, operation.path, operation.capability) == ("POST", ENVIRONMENT + path, capability)
    assert operation.request is RetargetRequest and operation.response.__name__ == response
    assert (operation.revision, operation.etag, operation.statuses) == ("required", True, (200,))


@pytest.mark.parametrize("identifier", READS)
def test_each_read_answers_the_revision_as_its_etag(identifier):
    operation = OPERATIONS[identifier]
    assert operation.method == "GET" and operation.etag is True
    assert "revision" in operation.response.model_json_schema()["properties"]


def test_the_openapi_document_requires_if_match_on_every_retarget():
    paths = export_openapi()["paths"]
    for identifier, (path, _, _) in RETARGETS.items():
        post = paths["/api/v1" + ENVIRONMENT + path]["post"]
        assert post["operationId"] == identifier
        [header] = [parameter for parameter in post["parameters"] if parameter["in"] == "header"]
        assert (header["name"], header["required"]) == ("If-Match", True)


def test_a_retarget_names_only_the_activation():
    assert set(RetargetRequest.model_json_schema()["properties"]) == {"activation_id"}
    assert RetargetRequest.model_json_schema()["additionalProperties"] is False


def test_email_sources_are_listed_and_read_with_email_read_and_show_no_mail():
    listed, read = OPERATIONS["email_sources.list"], OPERATIONS["email_sources.read"]
    assert (listed.method, listed.path, listed.capability, listed.page) == (
        "GET",
        ENVIRONMENT + "/email/sources",
        "email.read",
        True,
    )
    assert (read.method, read.path, read.capability) == (
        "GET",
        ENVIRONMENT + "/email/sources/{identifier}",
        "email.read",
    )
    assert set(read.response.model_json_schema()["properties"]) == {
        "id",
        "connection_revision_id",
        "activation_id",
        "mailbox",
        "revision",
        "disabled",
    }
