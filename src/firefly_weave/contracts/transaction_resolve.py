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


"""The platform's resolve action: how a workflow resolves a transaction that is in doubt.

``weave.transaction-resolve@1.0.0`` is an Action whose worker implementation names a task type no worker may offer:
the platform runs it itself, under the run's own principal, as a person's resolution would run. Its documents are
fixed, so the platform recognizes the action by its digest.
"""

from typing import Final

from firefly_weave.contracts.transactions import TRANSACTION_STATES
from firefly_weave.contracts.values import JsonObject

RESOLVE_ACTION: Final = "weave.transaction-resolve"
RESOLVE_VERSION: Final = "1.0.0"
# No worker release may offer a task type with this prefix: only the platform runs these tasks.
PLATFORM_TASK_PREFIX: Final = "weave-platform."
RESOLVE_TASK_TYPE: Final = PLATFORM_TASK_PREFIX + "transaction-resolve"


def platform_task(task_type: str) -> bool:
    """Whether the platform reserves a task type for itself, however its prefix is capitalized (names allow it)."""
    return task_type.lower().startswith(PLATFORM_TASK_PREFIX)


# The catalog name under which workflows read the platform event as their input schema.
PLATFORM_EVENT_SCHEMA: Final = "weave.platform-event-v1"
# The resolution kinds a person may choose, in the order the incident's resolution lists them.
RESOLUTION_KINDS: Final = ("retry_safe", "accept_reconciled_result", "accept_reconciled_failure", "terminate")
RESOLVE_TIMEOUT_SECONDS: Final = 300

_UUID: Final[JsonObject] = {"type": "string", "format": "uuid"}
# Nonblank, as a resolution's reason and evidence reference must be.
_TEXT: Final[JsonObject] = {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": "\\S"}


def resolve_input_schema() -> JsonObject:
    """Which incident, how it is resolved and why; evidence and an output only where the kind needs them."""
    return {
        "type": "object",
        "properties": {
            "incident_id": dict(_UUID),
            "kind": {"type": "string", "enum": list(RESOLUTION_KINDS)},
            "reason": dict(_TEXT),
            "evidence_reference": dict(_TEXT),
            "output": {},
        },
        "required": ["incident_id", "kind", "reason"],
        "additionalProperties": False,
    }


def resolve_output_schema() -> JsonObject:
    """The resolution the platform recorded, and the transaction's state after it when the incident held one."""
    return {
        "type": "object",
        "properties": {
            "incident_id": dict(_UUID),
            "kind": {"type": "string", "enum": list(RESOLUTION_KINDS)},
            "receipt_id": dict(_UUID),
            "transaction": {
                "type": "object",
                "properties": {
                    # As long as an instance key may be.
                    "instance_key": {"type": "string", "minLength": 1, "maxLength": 512},
                    "state": {"type": "string", "enum": list(TRANSACTION_STATES)},
                },
                "required": ["instance_key", "state"],
                "additionalProperties": False,
            },
        },
        "required": ["incident_id", "kind", "receipt_id"],
        "additionalProperties": False,
    }


def resolve_action_definition() -> JsonObject:
    """The resolve action: one attempt, since a resolution that was refused is final and one that applied replays
    by its receipt."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": RESOLVE_ACTION, "version": RESOLVE_VERSION},
        "spec": {
            "implementation": {"kind": "worker", "taskType": RESOLVE_TASK_TYPE, "taskVersion": RESOLVE_VERSION},
            "inputSchema": resolve_input_schema(),
            "outputSchema": resolve_output_schema(),
            "sideEffect": "idempotency_key",
            "timeoutSeconds": RESOLVE_TIMEOUT_SECONDS,
            "retry": {"maxAttempts": 1},
        },
    }


def resolve_task_capability() -> JsonObject:
    """The task contract of the resolve action, with the same schemas, side effect and timeout."""
    return {
        "taskType": RESOLVE_TASK_TYPE,
        "taskVersion": RESOLVE_VERSION,
        "inputSchema": resolve_input_schema(),
        "outputSchema": resolve_output_schema(),
        "sideEffect": "idempotency_key",
        "timeoutSeconds": RESOLVE_TIMEOUT_SECONDS,
    }
