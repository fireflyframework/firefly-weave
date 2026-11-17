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

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, Protocol, TypeAliasType, get_args, get_origin
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import (
    ChangeHistory,
    ChangeRequest,
    DeploymentJob,
    DeploymentPlan,
    DrainSetting,
    PlanIntent,
    SafeDeploymentReceipt,
)
from firefly_weave.contracts.instance_keys import INSTANCE_KEY_PATTERN
from firefly_weave.contracts.language import STEP_KINDS
from firefly_weave.contracts.operations import IncidentView, RecordedResolution
from firefly_weave.contracts.operations_summary import (
    AttentionItem,
    CompatibilitySummary,
    ForbiddenSection,
    HealthComponent,
    UnavailableSection,
)
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import WorkerIdentity, WorkerStatus
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_service import ChangeRequestService
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.incident_codes import INCIDENT_CODES
from firefly_weave.operations.incidents import RUNTIME_EVENT_TYPES, IncidentFacts, IncidentService
from firefly_weave.operations.lumi_context import Explanation, Shorten, estimate_tokens
from firefly_weave.operations.lumi_deployments import COLLECTIONS, LIMITATION
from firefly_weave.operations.summary import OperationsSummaryService, SummaryFacts
from firefly_weave.runtime.transactions import HELD_INCIDENT
from firefly_weave.workers.service import WorkerService

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


def _literals(annotation: object) -> frozenset[str]:
    """The string literals inside an annotation, through type aliases, unions (an optional field) and list items."""
    if isinstance(annotation, TypeAliasType):
        return _literals(annotation.__value__)
    if get_origin(annotation) is Literal:
        return frozenset(value for value in get_args(annotation) if isinstance(value, str))
    return frozenset().union(*(_literals(arm) for arm in get_args(annotation)))


def values_of(model: type[ContractModel], field: str) -> frozenset[str]:
    """The fixed values a contract field accepts."""
    found = _literals(model.model_fields[field].annotation)
    if not found:
        raise TypeError(f"{model.__name__}.{field} has no fixed values")
    return found


def one_of(value: object, allowed: frozenset[str]) -> str | None:
    """A fixed-value field as sent: one of its contract's values, or None for anything a record should not hold."""
    return value if isinstance(value, str) and value in allowed else None


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


@dataclass(frozen=True, slots=True)
class Explainer:
    fields: frozenset[str]
    read: Reader
    limitation: str
    shorten: Shorten


PRESENCE: Final = values_of(WorkerStatus, "presence")
RUNTIMES: Final = values_of(WorkerIdentity, "runtime")
PLATFORM_HINTS: Final = values_of(WorkerIdentity, "platform_hint")
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
        "presence": one_of(status.presence, PRESENCE),
        "last_contact_seconds": age_seconds(status.last_seen_at, status.observed_at),
        "draining": status.draining,
        "capacity": status.capacity,
        "active_leases": status.active_leases,
        "task_types": task_types,
        "more_task_types": max(0, len(status.task_types) - TASK_TYPES),
        "runtime": one_of(identity.runtime, RUNTIMES) if identity else None,
        "platform_hint": one_of(identity.platform_hint, PLATFORM_HINTS) if identity else None,
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
INCIDENT_STATUSES: Final = values_of(IncidentView, "status")
RUN_STATUSES: Final = values_of(IncidentView, "run_status")
RESOLUTIONS: Final = values_of(RecordedResolution, "kind")
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
    events: JsonValue = (
        "withheld"
        if facts.events == "withheld"
        else [one_of(event, RUNTIME_EVENT_TYPES) for event in facts.events[-EVENT_TYPES:]]
    )
    data: JsonObject = {
        "code": code,
        "origin_code": code_or_none(view.origin_code),
        "status": one_of(view.status, INCIDENT_STATUSES),
        "node_id": key_or_none(view.node_id),
        "instance_key": key_or_none(view.instance_key) if view.instance_key else None,
        "step_kind": step_kind if step_kind == "withheld" or step_kind in STEP_KIND_NAMES else None,
        "generation": view.generation,
        "opened_seconds": age_seconds(view.opened_at, facts.observed_at),
        "workflow": (
            {"name": name_or_none(workflow.name), "version": name_or_none(workflow.version)} if workflow else None
        ),
        "run_status": one_of(view.run_status, RUN_STATUSES),
        "recent_events": events,
        "more_recent_events": None if withheld else facts.more_events,
        "other_events": None if withheld else facts.other_events,
        "resolution": one_of(view.resolution.kind, RESOLUTIONS) if view.resolution else None,
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
# Attention kinds whose titles are never sent, such as titles a person writes: an alert's title is its rule's name.
UNTITLED_KINDS: Final[frozenset[str]] = frozenset({"alert"})
# Kinds whose title names a workflow only when the caller may see the run.
WORKFLOW_TITLED: Final = frozenset({"incident", "run"})
HEALTH_COMPONENTS: Final = values_of(HealthComponent, "component")
HEALTH_STATES: Final = values_of(HealthComponent, "state")
ATTENTION_KINDS: Final = values_of(AttentionItem, "kind")
COMPATIBILITY_MODES: Final = values_of(CompatibilitySummary, "mode")
SECTION_STATES: Final = values_of(ForbiddenSection, "state") | values_of(UnavailableSection, "state")
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
        return {"state": one_of(section.state, SECTION_STATES)}
    picked: JsonObject = {name: getattr(section, name) for name in counts}
    return picked | {name: _whole(getattr(section, name)) for name in seconds}


def environment_context(facts: SummaryFacts) -> Explained:
    """Platform health as the summary computed it for this caller: counts, states, ages and checked titles only."""
    summary = facts.summary
    observed_at = summary.observed_at
    api = summary.api
    health: list[JsonValue] = [
        {
            "component": one_of(item.component, HEALTH_COMPONENTS),
            "state": one_of(item.state, HEALTH_STATES),
            "last_ok_seconds": age_seconds(item.last_ok_at, observed_at),
            "consecutive_failures": item.consecutive_failures,
        }
        for item in summary.health[:HEALTH_ITEMS]
    ]
    named: set[str] = set()
    attention: JsonValue
    more_attention: int | None
    if isinstance(summary.attention, UnavailableSection):
        attention, more_attention = {"state": one_of(summary.attention.state, SECTION_STATES)}, None
    else:
        items: list[JsonValue] = []
        for item in summary.attention[:ATTENTION_ITEMS]:
            found = _match(item.kind, item.title)
            entry: JsonObject = {"kind": one_of(item.kind, ATTENTION_KINDS), "title": item.title if found else None}
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
            "compatibility": {
                "mode": one_of(api.compatibility.mode, COMPATIBILITY_MODES),
                "findings": api.compatibility.findings,
            },
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


CHANGE_INTENTS: Final = values_of(ChangeRequest, "intent")
CHANGE_STATES: Final = values_of(ChangeRequest, "state")
HISTORY_STATES: Final = values_of(ChangeHistory, "state")
PLAN_RISKS: Final = values_of(DeploymentPlan, "risks")
DRAIN_MODES: Final = values_of(DrainSetting, "mode")
RECEIPT_CODES: Final = values_of(SafeDeploymentReceipt, "code")
COMPONENTS: Final = 20
HISTORY: Final = 20
WARNINGS: Final = 10
DESIRED_COUNTS: Final = ("replicas", "cpu_millis", "memory_mib")
DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
CHANGE_REQUEST_FIELDS: Final = frozenset(
    {
        "/intent",
        "/state",
        "/components",
        "/components/*/name",
        "/components/*/replicas",
        "/components/*/cpu_millis",
        "/components/*/memory_mib",
        "/components/*/image_digest",
        "/more_components",
        "/drain",
        "/drain/mode",
        "/drain/timeout_seconds",
        "/drain_set_size",
        "/four_eyes",
        "/plan_expires_in_seconds",
        "/risks",
        "/history",
        "/history/*/state",
        "/history/*/age_seconds",
        "/more_history",
        "/receipt",
        "/receipt/code",
        "/receipt/external_effects_may_continue",
        "/receipt/changed_resource_count",
        "/failure_code",
        "/warnings",
    }
)


def _digest(image: str | None) -> str | None:
    """An image reference reduced to its digest: never its registry or repository."""
    if image is None:
        return None
    digest = image.rsplit("@", 1)[-1]
    return digest if DIGEST.fullmatch(digest) else None


def change_request_context(
    record: ChangeRequest, plan: DeploymentPlan | None, job: DeploymentJob | None, *, observed_at: datetime
) -> Explained:
    """A change request's state, plan and progress: never its title, reason, people, hosts, releases or IDs."""
    desired = {item.name: item for item in record.desired or []}
    components: list[JsonValue] = []
    for name in record.components[:COMPONENTS]:
        entry: JsonObject = {"name": name_or_none(name)}
        values = desired.get(name)
        if values is not None:
            entry |= {field: getattr(values, field) for field in DESIRED_COUNTS if getattr(values, field) is not None}
            if (digest := _digest(values.image)) is not None:
                entry["image_digest"] = digest
        components.append(entry)
    history: list[JsonValue] = [
        {"state": one_of(item.state, HISTORY_STATES), "age_seconds": age_seconds(item.at, observed_at)}
        for item in record.history[-HISTORY:]
    ]
    receipt = job.receipt if job is not None else None
    risks: list[JsonValue] = [one_of(risk, PLAN_RISKS) for risk in plan.risks] if plan is not None else []
    data: JsonObject = {
        "intent": one_of(record.intent, CHANGE_INTENTS),
        "state": one_of(record.state, CHANGE_STATES),
        "components": components,
        "more_components": max(0, len(record.components) - COMPONENTS),
        "drain": {"mode": one_of(record.drain.mode, DRAIN_MODES), "timeout_seconds": record.drain.timeout_seconds},
        "drain_set_size": len(record.drain_set),
        "four_eyes": record.approval_policy.distinct_approver,
        "plan_expires_in_seconds": (
            None
            if record.plan_expires_at is None
            else max(0, int((record.plan_expires_at - observed_at).total_seconds()))
        ),
        "risks": risks,
        "history": history,
        "more_history": max(0, len(record.history) - HISTORY),
        "receipt": (
            {
                "code": one_of(receipt.code, RECEIPT_CODES),
                "external_effects_may_continue": receipt.external_effects_may_continue,
                "changed_resource_count": len(receipt.changed_resources),
            }
            if receipt is not None
            else None
        ),
        "failure_code": code_or_none(record.failure.code) if record.failure else None,
        "warnings": [code_or_none(code) for code in record.warnings[:WARNINGS]],
    }
    return Explained(data, frozenset())


class ChangeReads(Protocol):
    async def read_observed(
        self, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> tuple[ChangeRequest, datetime]: ...


class DeploymentReads(Protocol):
    async def read(
        self, collection: str, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> ContractModel: ...


async def read_change_request(changes: ChangeReads, deployments: DeploymentReads, request: ExplainRequest) -> Explained:
    """The change request, its current plan and its apply job, each read as the caller; a refusal propagates."""
    actor, scope, context = request.actor, request.scope, request.context
    record, observed_at = await changes.read_observed(request.id, actor=actor, scope=scope, context=context)

    async def related(collection: str, identifier: UUID | None) -> ContractModel | None:
        if identifier is None:
            return None
        return await deployments.read(collection, identifier, actor=actor, scope=scope, context=context)

    plan = await related("plans", record.plan_id)
    job = await related("jobs", record.apply_job_id)
    return change_request_context(
        record,
        plan if isinstance(plan, DeploymentPlan) else None,
        job if isinstance(job, DeploymentJob) else None,
        observed_at=observed_at,
    )


# One sentence per kind, sent once for each kind present, after the deployment kinds' sentence.
# Sent with every operations explanation: the records hold no logs or free text, and the answer should say so.
NOT_IN_RECORDS: Final = "Say when a fact, such as a log, is not in these records."
LIMITATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "worker": (
            "Worker facts are the platform's last observation of its registration and contact; "
            "they don't show the container's own health. Name its presence, and its component when it is placed."
        ),
        "incident": (
            "Incident facts include no inputs, outputs, reasons or logs; "
            "the decisions listed are the ones that can apply to this code. "
            "Name the incident's code and the decisions you mean."
        ),
        "environment": (
            "Platform health is a summary for this caller; "
            "a section marked forbidden or unavailable is unknown, not zero. "
            "Give the counts and component names you rely on."
        ),
        "change-request": (
            "Change request facts show its state and plan, not who requested or approved it. "
            "Name the state it is in and the risks its plan lists."
        ),
    }
)


def _fits(data: JsonObject, room: int | None) -> bool:
    return room is None or estimate_tokens(json.dumps(data, ensure_ascii=False)) <= room


def _drop(
    data: JsonObject,
    room: int | None,
    trimmed: list[str],
    *,
    items: str,
    more: str,
    note: str,
    oldest: bool,
    keep: Callable[[JsonValue], bool] = lambda item: False,
) -> JsonObject:
    """Leave out list items one at a time (the oldest, or from the end), counting them in ``more``, until the data
    fits; items ``keep`` names stay. The list is at most twenty long, so this loop is short."""
    listed = data.get(items)
    if _fits(data, room) or not isinstance(listed, list):
        return data
    remaining = list(listed)
    order = range(len(remaining)) if oldest else range(len(remaining) - 1, -1, -1)
    removable = [index for index in order if not keep(remaining[index])]
    dropped: set[int] = set()
    shorter = data
    for index in removable:
        dropped.add(index)
        counted = data.get(more)
        left = [item for position, item in enumerate(remaining) if position not in dropped]
        if isinstance(counted, bool):
            extra: JsonValue = True
        else:
            extra = (counted if isinstance(counted, int) else 0) + len(dropped)
        shorter = {**data, items: left, more: extra}
        if _fits(shorter, room):
            break
    if shorter is not data and note not in trimmed:
        trimmed.append(note)
    return shorter


def _shorter_worker(data: JsonObject, room: int | None, trimmed: list[str]) -> JsonObject:
    return _drop(data, room, trimmed, items="task_types", more="more_task_types", note="task_types", oldest=False)


def _shorter_incident(data: JsonObject, room: int | None, trimmed: list[str]) -> JsonObject:
    return _drop(
        data, room, trimmed, items="recent_events", more="more_recent_events", note="incident_events", oldest=True
    )


def _shorter_environment(data: JsonObject, room: int | None, trimmed: list[str]) -> JsonObject:
    data = _drop(data, room, trimmed, items="attention", more="more_attention", note="attention", oldest=False)
    return _drop(
        data,
        room,
        trimmed,
        items="health",
        more="more_health",
        note="health",
        oldest=False,
        keep=lambda item: not (isinstance(item, dict) and item.get("state") == "ok"),
    )


def _shorter_change_request(data: JsonObject, room: int | None, trimmed: list[str]) -> JsonObject:
    data = _drop(data, room, trimmed, items="history", more="more_history", note="change_history", oldest=True)
    return _drop(data, room, trimmed, items="components", more="more_components", note="components", oldest=False)


SHORTENERS: Final[Mapping[str, Shorten]] = MappingProxyType(
    {
        "worker": _shorter_worker,
        "incident": _shorter_incident,
        "environment": _shorter_environment,
        "change-request": _shorter_change_request,
    }
)


def explanation_for(kinds: Iterable[str]) -> Explanation:
    """The limitations of the kinds present, once each and in a fixed order, and every kind's shortening rule."""
    present = set(kinds)
    sentences = [LIMITATION] if present & set(COLLECTIONS) else []
    sentences += [LIMITATIONS[kind] for kind in LIMITATIONS if kind in present]
    if present & set(LIMITATIONS):
        sentences.append(NOT_IN_RECORDS)
    return Explanation(limitations=tuple(sentences), shorten=SHORTENERS)


# A grant that covers every target's grant of the same kind, within one attachment.
_EVERY: Final = {"targets": "targets:*", "approvals": "approvals:*"}


def _covered(grant: str, reach: frozenset[str]) -> bool:
    if grant in reach:
        return True
    position, _, rest = grant.partition(":")
    kind = rest.partition(":")[0]
    return kind in _EVERY and f"{position}:{_EVERY[kind]}" in reach


def narrowed(first: frozenset[str], second: frozenset[str]) -> bool:
    """Whether the second read reached less than the first: a grant of the first that the second neither holds nor
    covers with its every-target grant. Grants are prefixed with their attachment's position."""
    return any(not _covered(grant, second) for grant in first)


@service
class OperationsExplainer:
    """Reads each operations record through its owning service, as the caller, and builds what Weave AI may see.

    Each kind's knowledge sits in one ``Explainer``: its paths, its reader, its limitation and how it shortens.
    """

    def __init__(
        self,
        workers: WorkerService,
        incidents: IncidentService,
        changes: ChangeRequestService,
        deployments: DeploymentService,
    ) -> None:
        self.workers, self.incidents, self.changes, self.deployments = workers, incidents, changes, deployments
        self.explainers: Mapping[str, Explainer] = MappingProxyType(
            {
                "worker": Explainer(WORKER_FIELDS, self._worker, LIMITATIONS["worker"], SHORTENERS["worker"]),
                "incident": Explainer(INCIDENT_FIELDS, self._incident, LIMITATIONS["incident"], SHORTENERS["incident"]),
                "environment": Explainer(
                    ENVIRONMENT_FIELDS, read_environment, LIMITATIONS["environment"], SHORTENERS["environment"]
                ),
                "change-request": Explainer(
                    CHANGE_REQUEST_FIELDS,
                    self._change_request,
                    LIMITATIONS["change-request"],
                    SHORTENERS["change-request"],
                ),
            }
        )

    async def read(self, kind: str, request: ExplainRequest) -> Explained:
        return await self.explainers[kind].read(request)

    async def _worker(self, request: ExplainRequest) -> Explained:
        status = await self.workers.read(request.actor, request.scope, request.id, context=request.context)
        assert isinstance(status, WorkerStatus)
        return worker_context(status)

    async def _incident(self, request: ExplainRequest) -> Explained:
        facts = await self.incidents.explanation_facts(
            request.actor, request.scope, request.id, context=request.context
        )
        return incident_context(facts)

    async def _change_request(self, request: ExplainRequest) -> Explained:
        return await read_change_request(self.changes, self.deployments, request)
