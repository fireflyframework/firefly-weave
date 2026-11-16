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

"""Operations records as Weave AI may see them: allowlisted facts, picked field by field, every string a token.

Each builder copies named fields into a new object and never dumps a record and deletes keys, so a field added to a
record later never reaches the model by default. Every string it emits is an enum value, a code, a name, a step ID or
instance key, a checked attention title or a server constant; anything else becomes ``null``. The token patterns are
a format check, not a confidentiality control: what stays out is decided by the field allowlists.

Each reader also returns the record's *reach*, the access grants that filled the data. Reach stays on the server; the
caller compares the reach of the read after the reply with the first one and discards a reply built from wider access.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN
from firefly_weave.contracts.language import STEP_KINDS
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import WorkerStatus
from firefly_weave.operations.incident_codes import INCIDENT_CODES
from firefly_weave.operations.incidents import IncidentFacts
from firefly_weave.runtime.transactions import HELD_INCIDENT

if TYPE_CHECKING:
    from firefly_weave.operations.summary import OperationsSummaryService

# The union of the resource, deployment, component, task-type and SDK-version name patterns. Always fullmatched.
NAME_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+#\[\]-]{0,252}")
# Platform codes (WV-TASK-AMBIGUOUS) and worker codes (PAYMENT_DECLINED).
CODE_TOKEN: Final = re.compile(r"WV-[A-Z0-9_-]{1,60}|[A-Z][A-Z0-9_]{0,63}")
# Step IDs (control steps start with @) and instance keys (loop[3]#2.1~4), as the contract defines them.
KEY_TOKEN: Final = re.compile(INSTANCE_KEY_PATTERN)
KEY_LENGTH: Final = 512
# Each list a builder sends is capped; the rest is counted, never sent.
TASK_TYPES: Final = 20
EVENT_TYPES: Final = 20

EXPLAINED_KINDS: Final = frozenset({"worker", "incident", "environment", "change-request"})


def name_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and NAME_TOKEN.fullmatch(value) else None


def code_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and CODE_TOKEN.fullmatch(value) else None


def key_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= KEY_LENGTH and KEY_TOKEN.fullmatch(value) else None


def age_seconds(at: datetime | None, observed_at: datetime) -> int | None:
    """Whole seconds from ``at`` to the read's own clock; never negative, never a timestamp."""
    return None if at is None else max(0, int((observed_at - at).total_seconds()))


@dataclass(frozen=True, slots=True)
class Explained:
    """What one attachment sends (``data``) and the access grants that filled it (``reach``, server-side only)."""

    data: JsonObject
    reach: frozenset[str]


@dataclass(frozen=True, slots=True)
class ExplainRequest:
    actor: Principal
    scope: Scope
    id: UUID
    context: AuditContext
    summary: "OperationsSummaryService | None"


type Reader = Callable[[ExplainRequest], Awaitable[Explained]]
# Shortens one attachment's data to fit ``room`` tokens (None: no limit), noting what it left out in ``trimmed``.
type Shorten = Callable[[JsonObject, int | None, list[str]], JsonObject]


@dataclass(frozen=True, slots=True)
class Explainer:
    fields: frozenset[str]
    read: Reader
    limitation: str
    shorten: Shorten


WORKER_FIELDS: Final = frozenset(
    {
        "/presence",
        "/last_contact_seconds",
        "/draining",
        "/capacity",
        "/active_leases",
        "/task_types",
        "/more_task_types",
        "/runtime",
        "/platform_hint",
        "/sdk_version",
        "/placement",
        "/placement/target",
        "/placement/component",
        "/registered_seconds",
    }
)


def worker_context(status: WorkerStatus) -> Explained:
    """A worker's registration and contact facts: never its host, node, namespace, labels, IDs or principal."""
    identity = status.identity
    placement = status.placement
    task_types: list[JsonValue] = [name_or_none(item) for item in status.task_types[:TASK_TYPES]]
    data: JsonObject = {
        "presence": status.presence,
        "last_contact_seconds": age_seconds(status.last_seen_at, status.observed_at),
        "draining": status.draining,
        "capacity": status.capacity,
        "active_leases": status.active_leases,
        "task_types": task_types,
        "more_task_types": max(0, len(status.task_types) - TASK_TYPES),
        "runtime": identity.runtime if identity else None,
        "platform_hint": identity.platform_hint if identity else None,
        "sdk_version": name_or_none(identity.sdk_version) if identity else None,
        "placement": (
            {"target": name_or_none(placement.target_name), "component": name_or_none(placement.component)}
            if placement
            else None
        ),
        "registered_seconds": age_seconds(status.registered_at, status.observed_at),
    }
    reach = frozenset({f"placement:{placement.target_id}"}) if placement else frozenset()
    return Explained(data, reach)


# The step kinds a workflow can declare; a stored kind outside them is not sent.
STEP_KIND_NAMES: Final = frozenset(kind.kind for kind in STEP_KINDS)
UNKNOWN_CODE_NOTE: Final = "No description for this code."
INCIDENT_FIELDS: Final = frozenset(
    {
        "/status",
        "/code",
        "/origin_code",
        "/node_id",
        "/instance_key",
        "/step_kind",
        "/generation",
        "/opened_seconds",
        "/workflow",
        "/workflow/name",
        "/workflow/version",
        "/run_status",
        "/recent_events",
        "/more_recent_events",
        "/other_events",
        "/resolution",
        "/external_effects_may_continue",
        "/glossary",
        "/glossary/meaning",
        "/glossary/decisions",
        "/glossary/note",
        "/glossary_note",
    }
)


def _glossary(view: IncidentView, code: str | None) -> JsonObject | None:
    entry = INCIDENT_CODES.get(code) if code is not None else None
    if entry is None:
        return None
    # A held incident refuses every resolution but terminate, whatever its code allows elsewhere.
    decisions: list[JsonValue] = ["terminate"] if view.incident_key == HELD_INCIDENT else list(entry.decisions)
    return {"meaning": entry.meaning, "decisions": decisions, "note": entry.note}


def incident_context(facts: IncidentFacts | UnavailableResource) -> Explained:
    """An incident's code, step, state and recent event types: never its reason, output, evidence, people or IDs."""
    if isinstance(facts, UnavailableResource):
        return Explained({"status": "unavailable"}, frozenset())
    view = facts.view
    code = code_or_none(view.code)
    workflow = view.workflow
    withheld = facts.events == "withheld"
    step_kind = facts.step_kind
    events: JsonValue = "withheld" if facts.events == "withheld" else list(facts.events[-EVENT_TYPES:])
    data: JsonObject = {
        "code": code,
        "origin_code": code_or_none(view.origin_code),
        "status": view.status,
        "node_id": key_or_none(view.node_id),
        "instance_key": key_or_none(view.instance_key) if view.instance_key else None,
        "step_kind": step_kind if step_kind == "withheld" or step_kind in STEP_KIND_NAMES else None,
        "generation": view.generation,
        "opened_seconds": age_seconds(view.opened_at, facts.observed_at),
        "workflow": (
            {"name": name_or_none(workflow.name), "version": name_or_none(workflow.version)} if workflow else None
        ),
        "run_status": view.run_status,
        "recent_events": events,
        "more_recent_events": None if withheld else facts.more_events,
        "other_events": None if withheld else facts.other_events,
        "resolution": view.resolution.kind if view.resolution else None,
        "external_effects_may_continue": view.external_effects_may_continue,
        "glossary": _glossary(view, code),
    }
    if data["glossary"] is None:
        data["glossary_note"] = UNKNOWN_CODE_NOTE
    reach = frozenset({"available", "run"}) if facts.run_read else frozenset({"available"})
    return Explained(data, reach)
