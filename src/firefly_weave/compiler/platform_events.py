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


"""Publish-time rules for workflows that platform events start.

A workflow that declares ``eventTrigger`` receives the platform event as its input, so its input schema must accept
every event. An input schema the event can never fill is an error; one the compiler can't prove is a warning, and the
start validates the event against it anyway, recording a refused start when it doesn't fit.
"""

from typing import Literal, Protocol, cast

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.platform_catalog import platform_event_view, platform_schema_digests
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.compiler.typecheck import TypeCheckLimit, check_compatibility
from firefly_weave.contracts.transaction_resolve import PLATFORM_EVENT_SCHEMA
from firefly_weave.contracts.values import JsonObject

INPUT = "/spec/inputSchema"


class EventTriggerChecks(Protocol):
    """What the trigger rules read from the workflow analysis."""

    bundle: dict[str, JsonObject]
    schema_limits: SchemaLimits

    def issue(
        self,
        code: str,
        path: str,
        *,
        unknown: bool = False,
        stage: Literal["resolution", "semantic"] = "semantic",
    ) -> None: ...


def _names_the_platform_event(checks: EventTriggerChecks, schema: JsonObject) -> bool:
    """Whether the schema is exactly a reference to the platform's own event view, not a catalog's look-alike."""
    supplied = checks.bundle.get(PLATFORM_EVENT_SCHEMA)
    return (
        schema == {"$ref": PLATFORM_EVENT_SCHEMA}
        and supplied is not None
        and FrozenDocument.from_value(supplied).digest == platform_schema_digests()[PLATFORM_EVENT_SCHEMA]
    )


def check_event_trigger(checks: EventTriggerChecks, spec: JsonObject) -> None:
    """The workflow's input schema must accept the platform event when the workflow declares an event trigger."""
    if "eventTrigger" not in spec:
        return
    target = cast(JsonObject, spec["inputSchema"])
    if _names_the_platform_event(checks, target):
        return
    try:
        result = check_compatibility(platform_event_view(), target, checks.bundle, limits=checks.schema_limits)
    except TypeCheckLimit:
        checks.issue("RESOURCE_LIMIT", INPUT)
        return
    if result == "incompatible":
        checks.issue("TYPE_MISMATCH", INPUT)
    elif result == "unknown":
        # No guard: the start validates the event against the input schema and records a refused start.
        checks.issue("UNKNOWN_COMPATIBILITY", INPUT, unknown=True)
