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

"""Scoped, bounded operational metadata reads; execution remains runtime-owned."""

import asyncio
import heapq
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from firefly_weave.compiler.ir import accepted_ir_versions
from firefly_weave.contracts import language_features
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.instance_keys import split_instance
from firefly_weave.contracts.operations import MAX_WAITS_ON, IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import (
    RunListQuery,
    RunStepQuery,
    RunSummary,
    RunSummaryQuery,
    StepFact,
    StepFactWithOutput,
    StepKind,
)
from firefly_weave.contracts.transactions import IN_DOUBT_CODES, PHASE_SEGMENTS
from firefly_weave.definitions.models import CatalogError, ir_unsupported
from firefly_weave.operations.fact_positions import RunPosition, StepPosition
from firefly_weave.operations.facts import FactRepository, author_instance
from firefly_weave.operations.redaction import Omission, SafeProjection
from firefly_weave.persistence.paging import PAGE_BYTES
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime import admission

_RUN_SCOPE = "f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment"
# Summary columns only: node kinds (up to 1 MiB) are read by the one timeline metadata query that needs them.
_RUN_COLUMNS = (
    "f.run_id,f.definition_version_id,f.workflow_name,f.workflow_version,f.activation_id,f.activation_name,"
    "f.activation_revision,f.status,f.paused,f.test,f.origin,f.caller_run_id,f.caller_node_id,"
    "f.caller_instance_key,f.retried_from_run_id,f.business_key,f.correlation_key,f.started_at,f.updated_at,"
    "f.ended_at,f.failed_node_id,f.failed_instance_key,f.failed_error_code,f.active_incidents,f.handled_errors,"
    "f.transaction_state,f.classification_state,f.classification_policy,f.pinned_ir_version,f.pinned_features"
)
_RUN_FROM = (
    ",coalesce(a.archived,false) AS archived,EXISTS(SELECT 1 FROM run_policy_blocks b "
    "WHERE b.tenant_id=f.tenant_id AND b.project_id=f.project_id AND b.environment_id=f.environment_id "
    "AND b.run_id=f.run_id) AS policy_blocked FROM run_facts f LEFT JOIN run_archives a ON "
    "a.tenant_id=f.tenant_id AND a.project_id=f.project_id AND a.environment_id=f.environment_id "
    "AND a.run_id=f.run_id WHERE " + _RUN_SCOPE
)
_RUN_SELECT = "SELECT " + _RUN_COLUMNS + _RUN_FROM
_METADATA_SELECT = "SELECT " + _RUN_COLUMNS + ",f.node_kinds" + _RUN_FROM
_MISSING = (
    "s.tenant_id=:tenant AND s.project_id=:project AND s.environment_id=:environment AND s.run_id=:run "
    "AND left(s.node_id,1)<>'@' AND NOT EXISTS(SELECT 1 FROM step_facts f "
    "WHERE f.tenant_id=s.tenant_id AND f.project_id=s.project_id AND f.environment_id=s.environment_id "
    "AND f.run_id=s.run_id AND (f.instance_key=s.node_id OR (f.instance_key='' AND f.node_id=s.node_id)))"
)
_STEP_ORDER = 'scheduled_at ASC NULLS LAST,node_id COLLATE "C",instance_key COLLATE "C"'
_INCIDENT_SCOPE = "i.tenant_id=:tenant AND i.project_id=:project AND i.environment_id=:environment"
_INCIDENT_FACTS = (
    " LEFT JOIN incident_facts f ON f.tenant_id=i.tenant_id AND f.project_id=i.project_id "
    "AND f.environment_id=i.environment_id AND f.incident_id=i.id"
)
# Ordering keys only: filters and sorting never read the incident's resolution or its run.
_INCIDENT_KEYS = "SELECT i.id,f.opened_at FROM incidents i" + _INCIDENT_FACTS + " WHERE " + _INCIDENT_SCOPE
_SAME = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"


def _call_keys(participant: str) -> str:
    """The keys the calls of a participant (a JSON element) run under: its own, and its undo, release and confirm."""
    key = f"({participant}->>'instance_key')"
    return "ARRAY[" + ",".join([key, *(f"{key}||'#{segment}'" for segment in sorted(PHASE_SEGMENTS.values()))]) + "]"


def participants_with_children(record: str) -> str:
    """SQL for the participants of the transaction record ``record`` (a ``run_transactions`` alias), each called one
    with ``child_run_id``: the run of its latest call, its undo, release or confirm call when one ran, else its own.
    A latest call that has not started yet names no run."""
    return (
        "(SELECT coalesce(jsonb_agg(CASE WHEN k.child IS NULL THEN e.item ELSE e.item||jsonb_build_object("
        "'child_run_id',k.child) END ORDER BY e.n),'[]'::jsonb) FROM jsonb_array_elements("
        f"{record}.participants) WITH ORDINALITY e(item,n) LEFT JOIN LATERAL (SELECT c.child_run_id AS child "
        f"FROM run_calls c WHERE {_SAME.format(a='c', b=record)} AND c.caller_run_id={record}.run_id "
        f"AND c.node_id=ANY({_call_keys('e.item')}) ORDER BY c.node_id<>(e.item->>'instance_key') DESC,"
        "c.requested_at DESC,c.generation DESC LIMIT 1) k ON true)"
    )


def _linked(incident: str, facts: str, run: str) -> str:
    """A linked incident as a link (its keys, code, status and facts) and its run's classification metadata, which
    decides whether the link may be shown; only the link counts toward the page budget."""
    return (
        f"jsonb_build_object('link',jsonb_build_object('id',{incident}.id,'run_id',{incident}.run_id,'code',"
        f"{incident}.code,'status',{incident}.status,'node_id',{incident}.node_id,'static_node_id',{facts}.node_id,"
        f"'instance_key',{facts}.instance_key,'projected',{facts}.incident_id IS NOT NULL,'workflow_name',"
        f"{facts}.workflow_name,'workflow_version',{facts}.workflow_version),'run',jsonb_build_object("
        f"'classification_state',{run}.classification_state,'classification_policy',{run}.classification_policy,"
        f"'pinned_ir_version',{run}.pinned_ir_version,'pinned_features',{run}.pinned_features,'policy_blocked',"
        f"EXISTS(SELECT 1 FROM run_policy_blocks b WHERE {_SAME.format(a='b', b=incident)} "
        f"AND b.run_id={incident}.run_id)))"
    )


# An active settle timeout waits on the active in-doubt incidents of the runs its transaction's steps called (the
# latest call of each key), oldest first; one more than the contract allows is read to tell whether there are more.
_WAITS_ON = (
    "LEFT JOIN LATERAL (SELECT jsonb_agg(w.item ORDER BY w.opened_at ASC NULLS LAST,w.id) AS links,count(*) AS found,"
    "sum(octet_length((w.item->'link')::text)) AS bytes FROM (SELECT ci.id,cf.opened_at,"
    + _linked("ci", "cf", "cr")
    + " AS item FROM jsonb_array_elements(t.participants) p CROSS JOIN LATERAL unnest("
    + _call_keys("p.value")
    + ") k(node) JOIN LATERAL (SELECT c.child_run_id FROM run_calls c WHERE "
    + _SAME.format(a="c", b="i")
    + " AND c.caller_run_id=i.run_id AND c.node_id=k.node ORDER BY c.generation DESC LIMIT 1) c ON true "
    "JOIN incidents ci ON "
    + _SAME.format(a="ci", b="i")
    + " AND ci.run_id=c.child_run_id AND ci.status='active' AND ci.code=ANY(:in_doubt) LEFT JOIN incident_facts cf ON "
    + _SAME.format(a="cf", b="ci")
    + " AND cf.incident_id=ci.id LEFT JOIN run_facts cr ON "
    + _SAME.format(a="cr", b="ci")
    + " AND cr.run_id=ci.run_id WHERE cast(:links AS boolean) AND i.status='active' "
    "AND i.code='WV-TXN-SETTLE-TIMEOUT' ORDER BY cf.opened_at ASC NULLS LAST,ci.id LIMIT :waits_limit) w) w ON true "
)
# An active in-doubt incident of a called run is held by its caller's active settle timeout, when that timeout's
# transaction lists the step whose latest call started this run. A settle timeout is at its transaction's own key; the
# transaction's record names only the first incident that put it in doubt.
_HELD_BY = (
    "LEFT JOIN LATERAL (SELECT "
    + _linked("pi", "pf", "pr")
    + " AS item FROM run_calls c JOIN incidents pi ON "
    + _SAME.format(a="pi", b="c")
    + " AND pi.run_id=c.caller_run_id AND pi.status='active' AND pi.code='WV-TXN-SETTLE-TIMEOUT' "
    "JOIN run_transactions pt ON "
    + _SAME.format(a="pt", b="pi")
    + " AND pt.run_id=pi.run_id AND pt.instance_key=pi.node_id LEFT JOIN incident_facts pf ON "
    + _SAME.format(a="pf", b="pi")
    + " AND pf.incident_id=pi.id LEFT JOIN run_facts pr ON "
    + _SAME.format(a="pr", b="pi")
    + " AND pr.run_id=pi.run_id WHERE cast(:links AS boolean) AND i.status='active' AND i.code=ANY(:in_doubt) AND "
    + _SAME.format(a="c", b="i")
    + " AND c.child_run_id=i.run_id AND NOT EXISTS(SELECT 1 FROM run_calls l WHERE "
    + _SAME.format(a="l", b="c")
    + " AND l.caller_run_id=c.caller_run_id AND l.node_id=c.node_id AND l.generation>c.generation) "
    "AND EXISTS(SELECT 1 FROM jsonb_array_elements(pt.participants) p WHERE c.node_id=ANY("
    + _call_keys("p.value")
    + ")) ORDER BY pf.opened_at DESC NULLS LAST,pi.id LIMIT 1) h ON true "
)
# The steps an in-doubt incident is at: its own, without the phase it is at.
_INCIDENT_STEP = "regexp_replace(i.node_id,'#(confirm|cancel|undo)$','')"
# The loop indexes of that step (``[2][0]``), and how many there are.
_INCIDENT_LOOPS = r"coalesce(substring(i.node_id from '^[^\[#~@]+((?:\[[0-9]+\])+)'),'')"
_INCIDENT_DEPTH = f"char_length({_INCIDENT_LOOPS})-char_length(replace({_INCIDENT_LOOPS},'[',''))"
_SAME_RUN = (
    "{0}.tenant_id=i.tenant_id AND {0}.project_id=i.project_id AND {0}.environment_id=i.environment_id "
    "AND {0}.run_id=i.run_id"
)
# The transaction an in-doubt incident belongs to. Its key is the key of a transaction step of the run with the first
# loop indexes of the incident's own step, so each candidate is one lookup by key however many transactions the run
# has. It is the one that timed out or whose steps hold the incident's step; or the only one the incident can be of,
# when its step is no step of a transaction (a read in its body). Never a guess: two candidates give none.
_INCIDENT_TRANSACTION = (
    " LEFT JOIN LATERAL (SELECT c.* FROM (SELECT o.tenant_id,o.project_id,o.environment_id,o.run_id,"
    "o.instance_key,o.state,o.business_key,o.participants,"
    f"o.instance_key=i.node_id OR o.participants @> jsonb_build_array(jsonb_build_object('instance_key',"
    f"{_INCIDENT_STEP})) AS own,o.incident_key=i.incident_key AS named,"
    # One with work left, or that a person ended, may have an open incident in doubt; any may have had one.
    "i.status<>'active' OR o.state IN ('committing','compensating','in_doubt','reconciled','force_ended') AS possible,"
    "count(*) FILTER (WHERE i.status<>'active' OR o.state IN "
    "('committing','compensating','in_doubt','reconciled','force_ended')) OVER () AS candidates "
    "FROM run_transactions o WHERE i.code=ANY(cast(:in_doubt AS text[])) AND left(i.node_id,1)<>'@' AND "
    + _SAME_RUN.format("o")
    + " AND o.instance_key=ANY(ARRAY(SELECT k.node||substring("
    + _INCIDENT_LOOPS
    + r" from '^(?:\[[0-9]+\])'||'{'||loops.depth||'}') FROM run_facts n "
    "CROSS JOIN LATERAL jsonb_each_text(CASE WHEN jsonb_typeof(n.node_kinds)='object' "
    "THEN n.node_kinds ELSE '{}'::jsonb END) AS k(node,kind) CROSS JOIN LATERAL generate_series(0,"
    + _INCIDENT_DEPTH
    + ") AS loops(depth) WHERE k.kind='transaction' AND "
    + _SAME_RUN.format("n")
    + "))"
    + ") c WHERE c.own OR c.named OR (c.possible AND c.candidates=1) "
    "ORDER BY c.own DESC,c.named DESC LIMIT 1) t ON true"
)
# What an incident's transaction adds to it: the steps, whose keys are also among the pending and the stuck ones.
TRANSACTION_BYTES = (
    "octet_length({0}.participants::text)"
    "+2*octet_length(jsonb_path_query_array({0}.participants,'$[*].instance_key')::text)"
)
# One statement for the selected incidents: their columns, measured in page order against the page budget before the
# resolutions are returned, their facts and their run's classification metadata. The page ends before the incident that
# would take it past the budget; its first one is returned whatever its size.
_INCIDENT_DETAILS = (
    # An in-doubt incident brings the transaction it belongs to, with the runs its steps called, and the incidents of
    # its case, all measured with it.
    "WITH measured AS (SELECT i.*,t.instance_key AS txn_key,t.state AS txn_state,t.business_key AS txn_business_key,"
    "tp.participants AS txn_participants,w.links AS waits_on,w.found AS waits_found,h.item AS held_by,"
    "array_position(cast(:ids AS uuid[]),i.id) AS page_position,sum(octet_length(to_jsonb(i)::text)+coalesce("
    + TRANSACTION_BYTES.format("tp")
    + ",0)+coalesce(w.bytes,0)+coalesce(octet_length((h.item->'link')::text),0)) "
    "OVER (ORDER BY array_position(cast(:ids AS uuid[]),i.id)) AS page_bytes FROM incidents i"
    + _INCIDENT_TRANSACTION
    + " LEFT JOIN LATERAL (SELECT "
    + participants_with_children("t")
    + " AS participants WHERE t.run_id IS NOT NULL) tp ON true "
    + _WAITS_ON
    + _HELD_BY
    + "WHERE "
    + _INCIDENT_SCOPE
    + " AND i.id=ANY(:ids)),page AS (SELECT * FROM measured WHERE page_bytes<=:budget OR page_position=1) "
    "SELECT i.id,i.run_id,i.incident_key,i.node_id,i.generation,i.origin_code,i.code,i.status,i.revision,"
    "i.txn_key,i.txn_state,i.txn_business_key,i.txn_participants,i.waits_on,i.waits_found,i.held_by,"
    # Its steps with an open incident after the point of no return: a person must say what became of each.
    "CASE WHEN i.txn_key IS NOT NULL THEN ARRAY(SELECT s.node_id FROM incidents s WHERE "
    + _SAME_RUN.format("s")
    + " AND s.status='active' AND s.code='WV-TXN-AFTER-PIVOT-STUCK' AND i.txn_participants @> "
    "jsonb_build_array(jsonb_build_object('instance_key',s.node_id))) END AS txn_stuck,"
    "i.actor_id,i.resolved_at,CASE WHEN i.page_bytes<=:budget THEN i.resolution END AS resolution,"
    "i.page_bytes>:budget AS oversized,f.incident_id IS NOT NULL AS projected,f.node_id AS static_node_id,"
    "f.instance_key,f.opened_at,f.updated_at,f.opened_at_estimated,f.workflow_name,f.workflow_version,f.test,"
    "r.status AS run_status,r.classification_state,r.classification_policy,r.pinned_ir_version,r.pinned_features,"
    "EXISTS(SELECT 1 FROM run_policy_blocks b WHERE b.tenant_id=i.tenant_id AND b.project_id=i.project_id "
    "AND b.environment_id=i.environment_id AND b.run_id=i.run_id) AS policy_blocked FROM page i"
    + _INCIDENT_FACTS
    + " LEFT JOIN run_facts r ON r.tenant_id=i.tenant_id AND r.project_id=i.project_id "
    "AND r.environment_id=i.environment_id AND r.run_id=i.run_id"
)
LEGACY_KEYS = 100_000
LEGACY_SECONDS = 2
_KIND: TypeAdapter[StepKind] = TypeAdapter(StepKind)


def unavailable(identifier: UUID) -> UnavailableResource:
    return UnavailableResource(id=identifier, omissions=[Omission(path="", reason="classification_unavailable")])


def _pinned_features(row: Mapping[str, Any]) -> list[str] | None:
    features = row.get("pinned_features")
    if (
        isinstance(features, list)
        and len(features) <= 256
        and all(type(value) is str for value in features)
        and len(json.dumps(features, ensure_ascii=False).encode()) <= 16384
    ):
        return features
    return None


def _supported(row: Mapping[str, Any], features: list[str]) -> bool:
    advertised = language_features.ADVERTISED_FEATURES
    return set(features) <= set(advertised) and row.get("pinned_ir_version") in accepted_ir_versions(advertised)


def available(row: Mapping[str, Any]) -> bool:
    features = _pinned_features(row)
    return (
        row.get("classification_state") == "available"
        and row.get("classification_policy") == admission.POLICY
        and row.get("policy_blocked") is False
        and features is not None
        and _supported(row, features)
    )


def withheld(row: Mapping[str, Any]) -> CatalogError:
    """The answer for a run whose metadata is withheld, matching what ``runs.read`` answers for it.

    A run this platform does not run yet (cached as unsupported, or pinning an IR version or feature no longer
    advertised) answers ``ir_unsupported``; it is transient. Legacy or blocked evidence stays unavailable.
    """
    features = _pinned_features(row)
    if (
        features is not None
        and row.get("policy_blocked") is False
        and row.get("classification_policy") == admission.POLICY
        and (
            row.get("classification_state") == "unsupported"
            or (row.get("classification_state") == "available" and not _supported(row, features))
        )
    ):
        return ir_unsupported(tuple(sorted(set(features) - set(language_features.ADVERTISED_FEATURES))))
    return legacy_unavailable()


def milliseconds(start: datetime | None, end: datetime | None) -> int | None:
    return int((end - start).total_seconds() * 1000) if start is not None and end is not None else None


def summary_of(row: Mapping[str, Any]) -> RunSummary | UnavailableResource:
    identifier = row["run_id"]
    if not available(row):
        return unavailable(identifier)
    try:
        return RunSummary.model_validate(
            dict(
                id=identifier,
                workflow=dict(
                    name=row["workflow_name"],
                    version=row["workflow_version"],
                    definition_version_id=row["definition_version_id"],
                ),
                activation_id=row["activation_id"],
                activation=dict(name=row["activation_name"], revision=row["activation_revision"]),
                status=row["status"],
                paused=row["paused"],
                test=row["test"],
                origin=row["origin"],
                caller=(
                    dict(
                        run_id=row["caller_run_id"],
                        node_id=row["caller_node_id"],
                        instance_key=row["caller_instance_key"],
                    )
                    if row["caller_run_id"] is not None
                    else None
                ),
                retried_from_run_id=row["retried_from_run_id"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                ended_at=row["ended_at"],
                duration_ms=milliseconds(row["started_at"], row["ended_at"]),
                business_key=row["business_key"],
                correlation_key=row["correlation_key"],
                failed_step=(
                    dict(
                        node_id=row["failed_node_id"],
                        instance_key=row["failed_instance_key"],
                        error_code=row["failed_error_code"],
                    )
                    if row["failed_node_id"] is not None
                    else None
                ),
                active_incidents=row["active_incidents"],
                handled_errors=row["handled_errors"],
                archived=row["archived"],
                transaction_state=row.get("transaction_state"),
            )
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return unavailable(identifier)


def step_of(row: Mapping[str, Any], *, output: SafeProjection | None = None) -> StepFact | StepFactWithOutput:
    try:
        values: dict[str, Any] = {
            key: row.get(key)
            for key in (
                "node_id",
                "instance_key",
                "kind",
                "status",
                "scheduled_at",
                "started_at",
                "ended_at",
                "attempts",
                "worker_id",
                "error_code",
                "handled",
                "child_run_id",
            )
        }
        values.update(
            iteration=list(split_instance(row["instance_key"] or row["node_id"]).indexes),
            duration_ms=milliseconds(row["started_at"], row["ended_at"]),
            queue_wait_ms=milliseconds(row["scheduled_at"], row["started_at"]),
            log_entries=0,
        )
        if output is not None:
            if output.available:
                return StepFactWithOutput(**values, output=output.value)
            values["omissions"] = [
                omission.model_copy(update={"path": "/output" + omission.path}) for omission in output.omissions
            ]
        return StepFact(**values)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise legacy_unavailable() from None


def _json(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _step_identity(source: str | None, static: str | None, instance: str | None) -> tuple[str | None, str] | None:
    """The served ``node_id`` and ``instance_key``, or None when the incident and its facts disagree."""
    if source is None:
        # A run-level incident has no step facts.
        return (None, "") if static is None and not instance else None
    if static is None:
        # Facts never attribute a control step (a branch output or join) to an author step; it keeps its own ID.
        return (source, "") if source.startswith("@") and not instance else None
    # The facts normalized the incident's own key: its full instance key, or the static step ID for a plain step.
    return (static, instance or "") if source == (instance or static) else None


def transaction_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    """``priority`` and ``transaction`` of an in-doubt incident, from the record of the transaction it belongs to."""
    from firefly_weave.runtime.settlement import pending

    if row["code"] not in IN_DOUBT_CODES:
        return {}
    facts: dict[str, Any] = {"priority": "high"}
    if row.get("txn_key") is not None:
        participants = row["txn_participants"] or []
        open_at = set(row.get("txn_stuck") or ())
        stuck = [item["instance_key"] for item in participants if item["instance_key"] in open_at]
        facts["transaction"] = {
            "instance_key": row["txn_key"],
            "state": row["txn_state"],
            "participants": participants,
            "pending": pending(row["txn_state"], participants),
            **({"business_key": row["txn_business_key"]} if row.get("txn_business_key") is not None else {}),
            **({"stuck_steps": stuck} if stuck else {}),
        }
    return facts


def _link(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """A linked incident as its own view would name it, or None when that view is withheld: its run's metadata is
    not available, or its facts are missing or disagree with it."""
    found, run = item["link"], item["run"]
    identity = _step_identity(found["node_id"], found["static_node_id"], found["instance_key"])
    if not found["projected"] or identity is None or not available(run):
        return None
    node_id, instance_key = identity
    workflow = found["workflow_name"]
    return {
        "id": found["id"],
        "run_id": found["run_id"],
        "code": found["code"],
        "status": found["status"],
        "node_id": node_id,
        "instance_key": instance_key,
        **({"workflow": {"name": workflow, "version": found["workflow_version"]}} if workflow is not None else {}),
    }


def case_links(row: Mapping[str, Any]) -> dict[str, Any]:
    """``waits_on``, ``waits_on_more`` and ``held_by`` of an incident, from the linked incidents its read found."""
    links = [found for item in row.get("waits_on") or () if (found := _link(item)) is not None]
    held = _link(row["held_by"]) if row.get("held_by") else None
    return {
        **({"waits_on": links[:MAX_WAITS_ON]} if links else {}),
        **({"waits_on_more": True} if (row.get("waits_found") or 0) > MAX_WAITS_ON else {}),
        **({"held_by": held} if held is not None else {}),
    }


def incident_of(row: Mapping[str, Any]) -> IncidentView | UnavailableResource:
    """An incident from its own columns and its facts; withheld unless its run's metadata is available.

    The facts carry the static step ID and full instance key normalized when they were written; a control step keeps
    the step ID stored on the incident. An incident whose facts are missing, or whose step identity disagrees with its
    facts, is unavailable rather than guessed.
    """
    identifier = row["id"]
    identity = _step_identity(row["node_id"], row["static_node_id"], row["instance_key"])
    if not row["projected"] or not available(row) or identity is None:
        return unavailable(identifier)
    node_id, instance_key = identity
    resolution = row["resolution"]
    values = dict(
        id=identifier,
        run_id=row["run_id"],
        incident_key=row["incident_key"],
        node_id=node_id,
        generation=row["generation"],
        origin_code=row["origin_code"],
        code=row["code"],
        status=row["status"],
        revision=row["revision"],
        actor_id=row["actor_id"],
        resolved_at=row["resolved_at"],
        resolution=resolution,
        external_effects_may_continue=isinstance(resolution, dict) and resolution.get("kind") == "terminate",
        instance_key=instance_key,
        opened_at=row["opened_at"],
        # A time is only estimated when it is known; an unknown opening time stays unknown.
        opened_at_estimated=row["opened_at_estimated"] is True and row["opened_at"] is not None,
        updated_at=row["updated_at"],
        workflow=dict(name=row["workflow_name"], version=row["workflow_version"]),
        run_status=row["run_status"],
        test=row["test"],
        **transaction_facts(row),
        **case_links(row),
    )
    try:
        return IncidentView.model_validate_json(json.dumps(values, default=_json))
    except (ValueError, TypeError, KeyError, OverflowError):
        return unavailable(identifier)


def legacy_unavailable() -> CatalogError:
    return CatalogError(409, "WV-LEGACY-UNAVAILABLE", "Step classification is unavailable")


def _segmented(key: str) -> bool:
    parsed = split_instance(key)
    return bool(parsed.segments) or parsed.count is not None


def step_order(row: Mapping[str, Any]) -> tuple[bool, datetime, str, str]:
    at = row["scheduled_at"]
    return at is None, at or datetime.max.replace(tzinfo=UTC), row["node_id"], row["instance_key"]


@dataclass
class _Candidate:
    order: tuple[bool, datetime, str, str]
    row: dict[str, Any] = field(compare=False)

    def __lt__(self, other: "_Candidate") -> bool:
        return self.order > other.order


class FactReadRepository(FactRepository):
    def __init__(self, tx: Transaction) -> None:
        super().__init__(tx)
        # One read unit is one snapshot, so a timeline reads its run metadata once.
        self._metadata: dict[UUID, dict[str, Any]] = {}

    async def summary_rows(self, query: RunSummaryQuery, position: RunPosition | None) -> list[dict[str, Any]]:
        return await self._run_rows(query, position)

    async def summary_row(self, run_id: UUID) -> dict[str, Any] | None:
        """One run's summary columns, archived or not; None when the run has no facts."""
        rows = await self.rows(_RUN_SELECT + " AND f.run_id=:run_id", run_id=run_id)
        return rows[0] if rows else None

    async def legacy_rows(self, query: RunListQuery, position: RunPosition | UUID | None) -> list[dict[str, Any]]:
        return await self._run_rows(query, position)

    async def _run_rows(self, query: RunSummaryQuery, position: RunPosition | UUID | None) -> list[dict[str, Any]]:
        clauses = ["(:include_archived OR NOT coalesce(a.archived,false))"]
        params: dict[str, Any] = dict(include_archived=query.include_archived, limit=query.limit + 1)
        for name, column in (
            ("workflow", "workflow_name"),
            ("version", "workflow_version"),
            ("origin", "origin"),
            ("caller_run_id", "caller_run_id"),
            ("retried_from_run_id", "retried_from_run_id"),
            ("business_key", "business_key"),
            ("correlation_key", "correlation_key"),
            ("activation_id", "activation_id"),
        ):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.{column}=:{name}")
                params[name] = value
        if query.status:
            clauses.append("f.status=ANY(:statuses)")
            params["statuses"] = query.status
        if query.transaction_state:
            clauses.append("f.transaction_state=ANY(:transaction_states)")
            params["transaction_states"] = query.transaction_state
        if not query.include_test:
            clauses.append("NOT f.test")
        if query.top_level_only:
            clauses.append("f.caller_run_id IS NULL")
        for name, column in (("has_active_incident", "active_incidents"), ("has_handled_errors", "handled_errors")):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.{column}>0" if value else f"f.{column}=0")
        for name, operator in (("started_after", ">="), ("started_before", "<")):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.started_at{operator}:{name}")
                params[name] = value
        column, direction, operator = {
            "id": ("run_id", "ASC", ">"),
            "started_desc": ("started_at", "DESC", "<"),
            "started_asc": ("started_at", "ASC", ">"),
            "updated_desc": ("updated_at", "DESC", "<"),
        }[query.order]
        if column == "run_id":
            if position is not None:
                if not isinstance(position, UUID):
                    raise ValueError("ID order requires an ID position")
                clauses.append("f.run_id>:position_id")
                params["position_id"] = position
            return await self.rows(
                _RUN_SELECT + " AND " + " AND ".join(clauses) + " ORDER BY f.run_id ASC LIMIT :limit", **params
            )
        if position is not None and not isinstance(position, RunPosition):
            raise ValueError("Chronological order requires a time position")
        # Known times, then the unknown-time tail, as two keyset scans in one snapshot. The known-time scan excludes
        # nulls, so the scoped DESC (nulls first) time indexes serve it in both directions with the row comparison
        # as an index bound; the tail is read only when known times do not fill the page.
        found: list[dict[str, Any]] = []
        if position is None or position.at is not None:
            known = [*clauses, f"f.{column} IS NOT NULL"]
            bounds: dict[str, Any] = {}
            if position is not None:
                known.append(f"(f.{column},f.run_id){operator}(:position_at,:position_id)")
                bounds = dict(position_at=position.at, position_id=position.id)
            found = await self.rows(
                _RUN_SELECT
                + " AND "
                + " AND ".join(known)
                + f" ORDER BY f.{column} {direction},f.run_id {direction} LIMIT :limit",
                **params,
                **bounds,
            )
            if len(found) > query.limit:
                return found
        unknown = [*clauses, f"f.{column} IS NULL"]
        bounds = {"limit": query.limit + 1 - len(found)}
        if position is not None and position.at is None:
            unknown.append(f"f.run_id{operator}:position_id")
            bounds["position_id"] = position.id
        return found + await self.rows(
            _RUN_SELECT + " AND " + " AND ".join(unknown) + f" ORDER BY f.run_id {direction} LIMIT :limit",
            **{**params, **bounds},
        )

    async def incident_rows(
        self, query: IncidentQuery, position: RunPosition | UUID | None, run_id: UUID | None
    ) -> list[dict[str, Any]]:
        """The ordering keys of one incident page and its lookahead: incident IDs and opening times."""
        clauses: list[str] = []
        params: dict[str, Any] = dict(limit=query.limit + 1)
        for name, column, value in (
            ("run", "i.run_id", run_id),
            ("code", "i.code", query.code),
            ("workflow", "f.workflow_name", query.workflow),
            ("node", "f.node_id", query.node_id),
        ):
            if value is not None:
                clauses.append(f"{column}=:{name}")
                params[name] = value
        if query.status:
            clauses.append("i.status=ANY(:statuses)")
            params["statuses"] = query.status
        if query.priority is not None:
            clauses.append("i.code=ANY(:in_doubt)" if query.priority == "high" else "NOT (i.code=ANY(:in_doubt))")
            params["in_doubt"] = sorted(IN_DOUBT_CODES)
        if not query.include_test:
            clauses.append("NOT coalesce(f.test,false)")
        for name, operator in (("opened_after", ">="), ("opened_before", "<")):
            value = getattr(query, name)
            if value is not None:
                clauses.append(f"f.opened_at{operator}:{name}")
                params[name] = value
        if query.order == "id":
            if isinstance(position, RunPosition):
                raise ValueError("ID order requires an ID position")
            if position is not None:
                clauses.append("i.id>:position_id")
                params["position_id"] = position
            return await self.rows(
                _INCIDENT_KEYS + "".join(" AND " + clause for clause in clauses) + " ORDER BY i.id ASC LIMIT :limit",
                **params,
            )
        if isinstance(position, UUID):
            raise ValueError("Chronological order requires a time position")
        direction, operator = ("DESC", "<") if query.order == "opened_desc" else ("ASC", ">")
        # Known opening times, then the unknown-time tail, as two keyset scans in one snapshot (as run pages are
        # read). The known-time scan excludes nulls, so the scoped opening-time index serves it in both directions
        # with the row comparison as an index bound; the tail is read only when known times do not fill the page.
        found: list[dict[str, Any]] = []
        if position is None or position.at is not None:
            known = [*clauses, "f.opened_at IS NOT NULL"]
            bounds: dict[str, Any] = {}
            if position is not None:
                known.append(f"(f.opened_at,f.incident_id){operator}(:position_at,:position_id)")
                bounds = dict(position_at=position.at, position_id=position.id)
            found = await self.rows(
                _INCIDENT_KEYS
                + "".join(" AND " + clause for clause in known)
                + f" ORDER BY f.opened_at {direction},f.incident_id {direction} LIMIT :limit",
                **params,
                **bounds,
            )
            if len(found) > query.limit:
                return found
        unknown = [*clauses, "f.opened_at IS NULL"]
        tail: dict[str, Any] = {"limit": query.limit + 1 - len(found)}
        if position is not None and position.at is None:
            unknown.append(f"i.id{operator}:position_id")
            tail["position_id"] = position.id
        return found + await self.rows(
            _INCIDENT_KEYS
            + "".join(" AND " + clause for clause in unknown)
            + f" ORDER BY i.id {direction} LIMIT :limit",
            **{**params, **tail},
        )

    async def incident_details(self, ids: list[UUID], *, links: bool = False) -> list[dict[str, Any]]:
        """The selected incidents with their facts, in order, as many as fit the logical byte limit; a first one above
        it is refused unread.

        ``links`` reads the incidents of each one's case too; only a caller who reads every incident of the
        environment may see them. Both reads that pass it (one incident, and the list) already require
        ``incident.read`` for the whole environment, so it is always true there; it stays as defense in depth, so a
        read that ever admits a narrower grant shows no links.
        """
        if not ids:
            return []
        rows = await self.rows(
            _INCIDENT_DETAILS,
            ids=ids,
            budget=PAGE_BYTES,
            links=links,
            in_doubt=sorted(IN_DOUBT_CODES),
            waits_limit=MAX_WAITS_ON + 1,
        )
        if any(row["oversized"] for row in rows):
            raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
        return rows

    async def recent_event_types(self, run_id: UUID, limit: int) -> list[str]:
        """The newest ``limit`` event types of one run, oldest first: the type column only, never data or evidence."""
        rows = await self.rows(
            "SELECT type FROM run_events WHERE tenant_id=:tenant AND project_id=:project "
            "AND environment_id=:environment AND run_id=:run ORDER BY sequence DESC LIMIT :limit",
            run=run_id,
            limit=limit,
        )
        return [str(row["type"]) for row in reversed(rows)]

    async def incident_step_kind(self, run_id: UUID, node_id: str, instance_key: str) -> str | None:
        """The kind of the step instance an incident stopped at, from its step facts."""
        rows = await self.rows(
            "SELECT kind FROM step_facts WHERE tenant_id=:tenant AND project_id=:project "
            "AND environment_id=:environment AND run_id=:run AND node_id=:node AND instance_key=:key",
            run=run_id,
            node=node_id,
            key=instance_key,
        )
        return str(rows[0]["kind"]) if rows else None

    async def run_exists(self, run_id: UUID) -> bool:
        return bool(
            await self.tx.session.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM runs r WHERE r.tenant_id=:tenant AND r.project_id=:project "
                    "AND r.environment_id=:environment AND r.id=:run)"
                ),
                {**self.params, "run": run_id},
            )
        )

    async def page_bytes(self, run_ids: list[UUID]) -> int:
        """Logical bytes of the retained runs on one page, measured before any of them is fetched."""
        if not run_ids:
            return 0
        rows = await self.rows(
            "SELECT coalesce(sum(public.weave_runs_bytes(r)),0) AS bytes FROM runs r "
            "WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment "
            "AND r.id=ANY(:ids)",
            ids=run_ids,
        )
        return int(rows[0]["bytes"])

    async def metadata(self, run_id: UUID) -> dict[str, Any]:
        if run_id in self._metadata:
            return self._metadata[run_id]
        rows = await self.rows(_METADATA_SELECT + " AND f.run_id=:run", run=run_id)
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
        row = rows[0]
        if isinstance(summary_of(row), UnavailableResource):
            raise withheld(row)
        kinds = row.get("node_kinds")
        if not isinstance(kinds, dict) or len(kinds) > 10000 or len(json.dumps(kinds).encode()) > 1048576:
            raise legacy_unavailable()
        self._metadata[run_id] = row
        return row

    async def step_rows(
        self, run_id: UUID, query: RunStepQuery, position: StepPosition | None
    ) -> tuple[list[dict[str, Any]], bool]:
        metadata = await self.metadata(run_id)
        clauses = [_RUN_SCOPE, "f.run_id=:run", "left(f.node_id,1)<>'@'", "left(f.instance_key,1)<>'@'"]
        params: dict[str, Any] = dict(run=run_id, limit=query.limit + 1)
        if query.step is not None:
            clauses.append("f.node_id=:step")
            params["step"] = query.step
        if position is not None:
            params.update(node=position.node_id, instance=position.instance_key)
            identity_predicate = (
                '(f.node_id COLLATE "C",f.instance_key COLLATE "C")>'
                '(cast(:node AS text) COLLATE "C",cast(:instance AS text) COLLATE "C")'
            )
            if position.scheduled_at is None:
                clauses.append("(f.scheduled_at IS NULL AND " + identity_predicate + ")")
            else:
                params["scheduled"] = position.scheduled_at
                clauses.append(
                    "(f.scheduled_at>:scheduled OR f.scheduled_at IS NULL OR (f.scheduled_at=:scheduled AND "
                    + identity_predicate
                    + "))"
                )
        rows = await self.rows(
            "SELECT f.* FROM step_facts f WHERE "
            + " AND ".join(clauses)
            + " ORDER BY "
            + _STEP_ORDER
            + " LIMIT :limit",
            **params,
        )
        for row in rows:
            try:
                identity = author_instance(row["instance_key"] or row["node_id"])
            except ValueError:
                raise legacy_unavailable() from None
            if (
                identity is None
                or identity.node_id != row["node_id"]
                or metadata["node_kinds"].get(row["node_id"]) != row["kind"]
            ):
                raise legacy_unavailable()
        missing = await self.tx.session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM step_instances s WHERE " + _MISSING + ")"), {**self.params, "run": run_id}
        )
        if not missing:
            return rows, True
        historical, complete = await self.historical_steps(run_id, query.step, position, query.limit)
        return sorted([*rows, *historical], key=step_order)[: query.limit + 1], complete

    async def historical_steps(
        self, run_id: UUID, step: str | None, position: StepPosition | None, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        metadata = await self.metadata(run_id)
        boundary = step_order(vars(position)) if position is not None else None
        heap: list[_Candidate] = []
        stream = None
        scanned = 0
        try:
            async with asyncio.timeout(LEGACY_SECONDS):
                # Unsorted on purpose: the bounded heap orders candidates, so the key cap also bounds the database.
                stream = await self.tx.session.stream(
                    text("SELECT s.node_id,s.status FROM step_instances s WHERE " + _MISSING),
                    {**self.params, "run": run_id},
                    execution_options={"yield_per": 500},
                )
                async for batch in stream.mappings().partitions(500):
                    for source in batch:
                        scanned += 1
                        if scanned > LEGACY_KEYS:
                            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Legacy step scan exceeds its bounded budget")
                        identity = author_instance(source["node_id"])
                        if identity is None:
                            continue
                        kind: StepKind = _KIND.validate_python(
                            metadata["node_kinds"].get(identity.node_id), strict=True
                        )
                        if source["status"] not in {"completed", "waiting"}:
                            raise legacy_unavailable()
                        row = dict(
                            node_id=identity.node_id,
                            instance_key=identity.instance_key,
                            kind=kind,
                            status="succeeded" if source["status"] == "completed" else "waiting",
                            scheduled_at=None,
                            started_at=None,
                            ended_at=None,
                            attempts=0,
                            worker_id=None,
                            error_code=None,
                            handled=None,
                            child_run_id=None,
                        )
                        order = step_order(row)
                        if (step is not None and identity.node_id != step) or (
                            boundary is not None and order <= boundary
                        ):
                            continue
                        candidate = _Candidate(order, row)
                        if len(heap) < limit + 1:
                            heapq.heappush(heap, candidate)
                        elif order < heap[0].order:
                            heapq.heapreplace(heap, candidate)
                    await asyncio.sleep(0)
        except TimeoutError:
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Legacy step scan exceeds its bounded budget") from None
        except (ValueError, TypeError, KeyError):
            raise legacy_unavailable() from None
        finally:
            if stream is not None:
                await stream.close()
        return [candidate.row for candidate in sorted(heap, key=lambda value: value.order)], scanned == 0

    async def outputs(self, run_id: UUID, rows: list[dict[str, Any]]) -> dict[str, SafeProjection]:
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.compiler.ir import ActionNode, HumanTaskNode, SignalNode, WaitNode
        from firefly_weave.contracts.definitions import ActionDefinition, load_definition
        from firefly_weave.contracts.values import JsonObject
        from firefly_weave.operations.redaction import project
        from firefly_weave.runtime.kernel import action_schemas, workflow
        from firefly_weave.runtime.repository import RuntimeRepository

        await self.metadata(run_id)
        retained = await RuntimeRepository(self.tx).run(run_id)
        admission.require_available(retained)
        ir = workflow(import_artifact(retained["artifact"]))
        bundle = {
            dependency.reference: dependency.document for dependency in ir.dependencies if dependency.kind == "Schema"
        }
        nodes = {node.id: node for node in ir.graph.nodes}
        keys = [row["instance_key"] or row["node_id"] for row in rows]
        unclassified = SafeProjection(
            available=False, omissions=[Omission(path="", reason="classification_unavailable")]
        )
        result = dict.fromkeys(keys, unclassified)
        # An agent turn, tool call or loop yield has no schema of its own yet; the step's schema would classify
        # another value, so those outputs stay withheld and are never fetched.
        keys = [key for key in keys if not _segmented(key)]
        if not keys:
            return result
        predicate = (
            "tenant_id=:tenant AND project_id=:project AND environment_id=:environment AND run_id=:run "
            "AND left(node_id,1)<>'@' AND node_id=ANY(:keys)"
        )
        outputs = await self.rows(
            "WITH sized AS (SELECT node_id,output,sum(octet_length(output::text)) OVER () AS total_bytes "
            "FROM step_instances WHERE " + predicate + ") "
            "SELECT node_id,CASE WHEN total_bytes<=:budget THEN output ELSE NULL END AS output,"
            "total_bytes>:budget AS oversized FROM sized",
            run=run_id,
            keys=keys,
            budget=PAGE_BYTES,
        )
        if any(output["oversized"] for output in outputs):
            raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
        for output in outputs:
            identity = author_instance(output["node_id"])
            if identity is None:
                continue
            node = nodes.get(identity.node_id)
            schemas: list[JsonObject] = []
            if isinstance(node, ActionNode):
                definition = load_definition(
                    next(dependency.document for dependency in ir.dependencies if dependency.digest == node.dependency)
                )
                schemas = action_schemas(ir, cast(ActionDefinition, definition), "output", node=node)
            elif isinstance(node, SignalNode):
                schemas = [ir.schemas[node.schema_ref]]
            elif isinstance(node, HumanTaskNode):
                schemas = [{"type": "object", "properties": {"data": node.form_schema, "decision": {"type": "string"}}}]
            elif isinstance(node, WaitNode):
                schemas = [{"type": "null"}]
            elif node is not None:
                schemas = [
                    ir.schemas[guard.schema_ref]
                    for guard in ir.guards
                    if guard.path in {node.path, node.path + "/value"}
                    and guard.purpose in {"transform_output", "decision_output", "branch_output"}
                ]
            if schemas:
                projections = [project(output["output"], schema, bundle) for schema in schemas]
                result[output["node_id"]] = next(
                    (projection for projection in projections if not projection.available), projections[0]
                )
        return result
