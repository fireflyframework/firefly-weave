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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, get_args
from uuid import UUID

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import PlanIntent
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN
from firefly_weave.contracts.language import STEP_KINDS
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.operations_summary import ForbiddenSection, UnavailableSection
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import WorkerStatus
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.incident_codes import INCIDENT_CODES
from firefly_weave.operations.incidents import IncidentFacts
from firefly_weave.operations.summary import OperationsSummaryService, SummaryFacts
from firefly_weave.runtime.transactions import HELD_INCIDENT

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
    summary: OperationsSummaryService | None


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


_NAME = r"(?P<name>\S+)"
_INTENTS = "|".join(re.escape(intent.replace("_", " ")) for intent in get_args(PlanIntent.__value__))
# The titles the operations summary writes, one anchored template per attention kind. A title is sent only when it
# fullmatches its kind's template and its name slot is a name token; anything else, a truncated title included, is
# sent as null with its kind. Every attention kind is either here or in UNTITLED_KINDS.
ATTENTION_TITLES: Final[Mapping[str, re.Pattern[str]]] = MappingProxyType(
    {
        "job": re.compile(rf"Deployment job on {_NAME} needs reconciliation"),
        "approval": re.compile(rf"Approve the plan to (?P<intent>{_INTENTS}) on {_NAME}"),
        "worker": re.compile(r"Offline worker still holds (?P<count>[0-9]{1,9}) active leases?"),
        "incident": re.compile(rf"Active incident(?: in {_NAME})?"),
        "runner": re.compile(rf"No runner is online for {_NAME}"),
        "compatibility": re.compile(r"Execution compatibility is restricted"),
        "run": re.compile(rf"A run(?: of {_NAME})? failed"),
    }
)
# Attention kinds whose titles are never sent, such as titles a person writes.
UNTITLED_KINDS: Final[frozenset[str]] = frozenset()
# Kinds whose title names a workflow only when the caller may see the run.
WORKFLOW_TITLED: Final = frozenset({"incident", "run"})
HEALTH_ITEMS: Final = 16
ATTENTION_ITEMS: Final = 20
LOAD_COUNTS: Final = (
    "runs_active",
    "tasks_ready",
    "tasks_leased",
    "waits_active",
    "waits_overdue",
    "outbox_pending",
    "provider_pending",
    "sources_enabled",
)
LOAD_SECONDS: Final = ("oldest_ready_seconds", "oldest_overdue_seconds", "outbox_oldest_seconds")
WORKER_COUNTS: Final = ("online", "offline", "draining", "capacity", "active_leases")
TARGET_COUNTS: Final = ("total", "runner_online", "busy", "reconciliation_required", "awaiting_approval")
INCIDENT_COUNTS: Final = ("active", "opened_24h")
RUN_COUNTS: Final = ("started", "succeeded", "failed", "failure_rate")


def _section_paths(name: str, fields: tuple[str, ...]) -> set[str]:
    return {f"/{name}", f"/{name}/state"} | {f"/{name}/{field}" for field in fields}


ENVIRONMENT_FIELDS: Final = frozenset(
    {
        "/api",
        "/api/version",
        "/api/uptime_seconds",
        "/api/compatibility",
        "/api/compatibility/mode",
        "/api/compatibility/findings",
        "/health",
        "/health/*/component",
        "/health/*/state",
        "/health/*/last_ok_seconds",
        "/health/*/consecutive_failures",
        "/more_health",
        "/attention",
        "/attention/state",
        "/attention/*/kind",
        "/attention/*/title",
        "/attention/*/expires_in_seconds",
        "/more_attention",
    }
    | _section_paths("load", LOAD_COUNTS + LOAD_SECONDS)
    | _section_paths("workers", WORKER_COUNTS)
    | _section_paths("targets", TARGET_COUNTS)
    | _section_paths("incidents", INCIDENT_COUNTS)
    | _section_paths("runs_24h", RUN_COUNTS)
)


def _match(kind: str, title: str) -> re.Match[str] | None:
    template = ATTENTION_TITLES.get(kind)
    found = template.fullmatch(title) if template is not None and kind not in UNTITLED_KINDS else None
    if found is None or ("name" in found.groupdict() and found["name"] is not None and not name_or_none(found["name"])):
        return None
    return found


def title_or_none(kind: str, title: str) -> str | None:
    """The summary's title when it is its kind's template with token names; otherwise None."""
    return title if _match(kind, title) is not None else None


def _whole(value: float | None) -> int | None:
    return None if value is None else int(value)


def _counts(section: ContractModel, counts: tuple[str, ...], seconds: tuple[str, ...] = ()) -> JsonObject:
    if isinstance(section, ForbiddenSection | UnavailableSection):
        return {"state": section.state}
    picked: JsonObject = {name: getattr(section, name) for name in counts}
    return picked | {name: _whole(getattr(section, name)) for name in seconds}


def environment_context(facts: SummaryFacts) -> Explained:
    """Platform health as the summary computed it for this caller: counts, states, ages and checked titles only."""
    summary = facts.summary
    observed_at = summary.observed_at
    api = summary.api
    health: list[JsonValue] = [
        {
            "component": item.component,
            "state": item.state,
            "last_ok_seconds": age_seconds(item.last_ok_at, observed_at),
            "consecutive_failures": item.consecutive_failures,
        }
        for item in summary.health[:HEALTH_ITEMS]
    ]
    named: set[str] = set()
    attention: JsonValue
    more_attention: int | None
    if isinstance(summary.attention, UnavailableSection):
        attention, more_attention = {"state": summary.attention.state}, None
    else:
        items: list[JsonValue] = []
        for item in summary.attention[:ATTENTION_ITEMS]:
            found = _match(item.kind, item.title)
            entry: JsonObject = {"kind": item.kind, "title": item.title if found else None}
            if found is not None and item.kind in WORKFLOW_TITLED and found.groupdict().get("name"):
                named.add(f"named:{item.id}")
            if item.kind == "approval":
                entry["expires_in_seconds"] = (
                    None if item.expires_at is None else max(0, int((item.expires_at - observed_at).total_seconds()))
                )
            items.append(entry)
        attention, more_attention = items, max(0, len(summary.attention) - ATTENTION_ITEMS)
    data: JsonObject = {
        "api": {
            "version": name_or_none(api.version),
            "uptime_seconds": int(api.uptime_seconds),
            "compatibility": {"mode": api.compatibility.mode, "findings": api.compatibility.findings},
        },
        "health": health,
        "more_health": max(0, len(summary.health) - HEALTH_ITEMS),
        "load": _counts(summary.load, LOAD_COUNTS, LOAD_SECONDS),
        "workers": _counts(summary.workers, WORKER_COUNTS),
        "targets": _counts(summary.targets, TARGET_COUNTS),
        "incidents": _counts(summary.incidents, INCIDENT_COUNTS),
        "attention": attention,
        "more_attention": more_attention,
        "runs_24h": None if summary.runs_24h is None else _counts(summary.runs_24h, RUN_COUNTS),
    }
    return Explained(data, facts.reach | named)


async def read_environment(request: ExplainRequest) -> Explained:
    """The environment's summary read as the caller; another environment's ID is not found, and nothing is read."""
    if request.id != request.scope.environment_id:
        raise CatalogError(404, "WV-NOT-FOUND", "Environment not found")
    if request.summary is None:
        raise CatalogError(503, "WV-UNAVAILABLE", "The platform summary is not available yet")
    facts = await request.summary.explanation_facts(request.actor, request.scope, context=request.context)
    return environment_context(facts)
