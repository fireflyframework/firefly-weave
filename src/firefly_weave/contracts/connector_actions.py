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

"""Built-in connector actions a workflow can own, as Action documents.

Studio's Send email, Reply to an email and file steps start from these documents: it
copies one, names it after the workflow and the step, and keeps it with the workflow
until it is published. The Studio host serves the list at
``GET /studio/contracts/connector-actions``.
"""

from __future__ import annotations

from typing import Any, cast

from firefly_weave.connectors import email
from firefly_weave.contracts import file_connectors
from firefly_weave.contracts.values import JsonObject

EMAIL_CONNECTOR = "weave-email@1.0.0"


def _email_actions() -> list[JsonObject]:
    document = cast(dict[str, Any], email.package.descriptor.manifest.value)
    actions = cast(dict[str, dict[str, Any]], document["spec"]["actions"])
    entries: list[JsonObject] = []
    for name in ("send", "reply"):
        action = actions[name]
        entries.append(
            {
                "connector": EMAIL_CONNECTOR,
                "action": name,
                "document": {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": f"weave-email-{name}", "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "connector", "uses": EMAIL_CONNECTOR, "action": name},
                        "inputSchema": action["inputSchema"],
                        "outputSchema": action["outputSchema"],
                        "sideEffect": action["sideEffect"],
                        "timeoutSeconds": action["timeoutSeconds"],
                        "connection": {"connector": EMAIL_CONNECTOR},
                    },
                },
            }
        )
    return entries


def builtin_connector_actions() -> list[JsonObject]:
    """Email send and reply, then every operation of every file connector, in a fixed order."""
    entries = _email_actions()
    for name in file_connectors.NAMES:
        for operation in file_connectors.OPERATIONS:
            entries.append(
                {
                    "connector": f"{name}@{file_connectors.VERSION}",
                    "action": operation,
                    "document": file_connectors.action_definition(name, operation),
                }
            )
    return entries
