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

"""The agent conversation operations are registered and documented like every operation: paths, methods,
capabilities, typed path parameters and paging."""

import re
from functools import cache

import pytest

from firefly_weave.contracts.agent_conversations import (
    AgentConversation,
    AgentConversationDeletion,
    AgentConversationQuery,
    AgentConversationSummary,
    ConversationPurge,
    ConversationPurgeRequest,
)
from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.public import Page
from firefly_weave.contracts.surface import OPERATIONS

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
CONVERSATIONS = ENVIRONMENT + "/agent-conversations"
ONE = CONVERSATIONS + "/{workflow}/{key_hash}"

spec = cache(export_openapi)


def native(identifier: str) -> dict:
    operation = OPERATIONS[identifier]
    return spec()["paths"][operation.canonical_path][operation.method.lower()]


def resolved(value: dict) -> dict:
    while "$ref" in value:
        value = spec()["components"]["schemas"][value["$ref"].rsplit("/", 1)[1]] | {
            key: item for key, item in value.items() if key != "$ref"
        }
    return value


@pytest.mark.parametrize(
    ("identifier", "method", "path", "capability", "response", "request_model"),
    [
        ("agent_conversations.list", "GET", CONVERSATIONS, "run.read", Page[AgentConversationSummary], None),
        ("agent_conversations.read", "GET", ONE, "run.read", AgentConversation, None),
        ("agent_conversations.delete", "DELETE", ONE, "run.purge", AgentConversationDeletion, None),
        (
            "agent_conversations.purge_workflow",
            "POST",
            CONVERSATIONS + "/{workflow}/purge",
            "run.purge",
            ConversationPurge,
            ConversationPurgeRequest,
        ),
    ],
)
def test_each_conversation_operation_is_registered(identifier, method, path, capability, response, request_model):
    operation = OPERATIONS[identifier]

    assert (operation.method, operation.canonical_path, operation.capability) == (method, path, capability)
    assert (operation.response, operation.request) == (response, request_model)
    assert operation.served is True and f"Required capability: {capability}" in native(identifier)["description"]


def test_the_list_pages_with_its_query():
    operation = OPERATIONS["agent_conversations.list"]
    parameters = {parameter["name"]: parameter for parameter in native("agent_conversations.list")["parameters"]}

    assert operation.page is True and operation.query is AgentConversationQuery
    assert resolved(parameters["limit"]["schema"]).items() >= {"minimum": 1, "maximum": 100, "default": 50}.items()
    assert parameters["workflow"]["required"] is False and parameters["cursor"]["required"] is False


def test_the_workflow_purge_needs_an_idempotency_key():
    assert OPERATIONS["agent_conversations.purge_workflow"].idempotency is True
    headers = [item for item in native("agent_conversations.purge_workflow")["parameters"] if item["in"] == "header"]
    assert [(item["name"], item["required"]) for item in headers] == [("Idempotency-Key", True)]


@pytest.mark.parametrize("identifier", ["agent_conversations.read", "agent_conversations.delete"])
def test_path_parameters_are_typed_as_a_workflow_name_and_a_key_hash(identifier):
    parameters = {parameter["name"]: parameter for parameter in native(identifier)["parameters"]}

    key_hash = resolved(parameters["key_hash"]["schema"])
    workflow = resolved(parameters["workflow"]["schema"])
    assert key_hash["pattern"] == "^[0-9a-f]{64}$"
    assert re.fullmatch(workflow["pattern"][1:-1], "support-chat") and workflow["maxLength"] == 200
    assert not re.fullmatch(workflow["pattern"][1:-1], "../other")
