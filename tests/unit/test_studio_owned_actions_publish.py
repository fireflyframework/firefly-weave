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
"""Workflow-owned actions publish before the workflow that calls them.

Studio builds these Action documents itself: an API action through the HTTP
action builder, an email action from the built-in connector action. It
publishes them one by one before the workflow. These checks pin what that
order relies on, with the compiler the platform runs when it publishes.
"""

from __future__ import annotations

import copy

from firefly_weave.compiler.api import CompileResult, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connectors import email
from firefly_weave.contracts.connector_actions import builtin_connector_actions
from firefly_weave.contracts.definitions import ConnectorDefinition, load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.contracts.values import JsonObject
from firefly_weave.sdk.http_actions import HTTP_ADAPTER, author_http_action

REQUEST: JsonObject = {
    "name": "order-intake.call-api",
    "version": "1.0.0",
    "method": "GET",
    "pathTemplate": "/customers/{customerId}/orders",
    "parameters": [{"name": "status", "location": "query", "type": "string"}],
    "statuses": [200],
    "timeoutSeconds": 30,
    "description": "Used by step call-api of order-intake",
}
WORKFLOW = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
    mail: {connector: weave-email@1.0.0, required: true}
  steps:
    - id: call-api
      kind: action
      uses: order-intake.call-api@1.0.0
      connection: crm
      with:
        object:
          path:
            object:
              customerId: {ref: /input/customerId}
          query:
            object:
              status: {literal: open}
    - id: send-email
      kind: action
      uses: order-intake.send-email@1.0.0
      connection: mail
      with:
        literal: {to: [ana@example.com], subject: Your order, text: Thanks for your order.}
  output: {literal: {}}
"""


def catalog(*actions: JsonObject) -> CatalogSnapshot:
    """The bundled HTTP and email connectors, and these published actions."""
    http, mail = HTTP_PROFILE_DESCRIPTOR, email.package.descriptor
    return CatalogSnapshot.from_definitions(
        [
            ConnectorDefinition.model_validate(http.manifest.value),
            ConnectorDefinition.model_validate(mail.manifest.value),
            *(load_definition(action) for action in actions),
        ],
        tasks=[*http.capabilities, *mail.capabilities],
        adapters=[HTTP_ADAPTER, "weave-email"],
    )


def api_action(**changes: object) -> JsonObject:
    result = author_http_action({**REQUEST, **changes})
    assert result.ok and result.action is not None, result.diagnostics
    return dict(result.action)


def email_action() -> JsonObject:
    entry = next(
        entry
        for entry in builtin_connector_actions()
        if entry["connector"] == "weave-email@1.0.0" and entry["action"] == "send"
    )
    document = copy.deepcopy(entry["document"])
    document["metadata"] = {"name": "order-intake.send-email", "version": "1.0.0"}
    return document


def errors(result: CompileResult) -> list[tuple[str, str]]:
    return [(d.code, d.path) for d in result.diagnostics if d.severity == "error"]


def test_a_workflow_whose_own_actions_are_not_published_reports_each_one():
    result = compile_source(WORKFLOW, format="yaml", catalog=catalog())
    assert errors(result) == [
        ("WV-COMP-UNKNOWN_ACTION", "/spec/steps/0/uses"),
        ("WV-COMP-UNKNOWN_ACTION", "/spec/steps/1/uses"),
    ]


def test_the_actions_compile_on_their_own_and_then_the_workflow_compiles_with_them():
    api, mail = api_action(), email_action()
    for action in (api, mail):
        assert compile_source(action, format="object", catalog=catalog()).ok
    result = compile_source(WORKFLOW, format="yaml", catalog=catalog(api, mail))
    assert result.ok and result.artifact is not None


def test_a_published_action_is_accepted_again_unchanged_and_a_change_needs_the_next_version():
    published = api_action()
    assert compile_source(api_action(), format="object", catalog=catalog(published)).ok
    changed = compile_source(api_action(timeoutSeconds=20), format="object", catalog=catalog(published))
    assert errors(changed) == [("WV-COMP-IMMUTABLE_VERSION", "/metadata/version")]
    bumped = api_action(version="1.0.1", timeoutSeconds=20)
    assert compile_source(bumped, format="object", catalog=catalog(published)).ok


def test_without_its_connector_the_project_cannot_take_an_email_action():
    http = HTTP_PROFILE_DESCRIPTOR
    snapshot = CatalogSnapshot.from_definitions(
        [ConnectorDefinition.model_validate(http.manifest.value)], tasks=http.capabilities, adapters=[HTTP_ADAPTER]
    )
    result = compile_source(email_action(), format="object", catalog=snapshot)
    assert ("WV-COMP-UNKNOWN_CONNECTOR", "/spec/implementation/uses") in errors(result)
