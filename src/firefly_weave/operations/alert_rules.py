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

"""Alert rules: who may change them, how they are stored and read, previews, and the presets Weave offers.

A rule is a revisioned record that changes only at its current revision. Changing a rule's condition, disabling it or
deleting it resolves its firing alert and starts its state over; changing its name, severity, channels, For time or
repeat interval keeps the state. An environment holds at most 100 rules, and every change is audited with channel IDs,
never recipients. A stored rule this server can't evaluate (a signal or selector key from another server) reads as
unsupported and is never evaluated or deleted by it.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError
from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import (
    AlertEventFacts,
    AlertPreset,
    AlertPreview,
    AlertRuleDraft,
    AlertRuleError,
    AlertRuleState,
    AlertRuleView,
    AlertSelector,
)
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.public import Revoked
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_signals import ALERT_SIGNALS, SignalUnavailable, Value, read_signal, validate_rule
from firefly_weave.operations.alert_states import condition_met
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.operations.permissions import target_permissions
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork

RULE_LIMIT = 100
PREVIEW_TIMEOUT = "2000ms"
SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
# The fields that decide when a rule fires; changing any of them starts the rule's state over.
CONDITION = ("signal", "selector", "comparator", "threshold", "window_seconds", "min_samples")
_FIELDS = (
    "name",
    "enabled",
    "signal",
    "comparator",
    "threshold",
    "window_seconds",
    "for_seconds",
    "min_samples",
    "severity",
    "renotify_seconds",
)
_RULE = (
    "r.id,r.name,r.enabled,r.signal,r.selector,r.comparator,r.threshold,r.window_seconds,r.for_seconds,r.min_samples,"
    "r.severity,r.channel_ids,r.renotify_seconds,r.preset_id,r.revision,r.created_at,r.updated_at,r.updated_by"
)
_STATE = "s.state,s.since,s.last_value,s.evaluated_at,s.error_code,s.error_at,s.alert_id,s.revision AS state_revision"


def _preset(identifier: str, install: Literal["default", "suggested"], name: str, **rule: Any) -> AlertPreset:
    draft = AlertRuleDraft.model_validate_json(json.dumps({"name": name, **rule}))
    return AlertPreset(id=identifier, version=1, install=install, name=name, rule=draft)


# Default presets are installed once in each environment by the evaluator; suggested ones are only offered. A preset
# keeps its ID; a changed meaning gets a new version.
ALERT_PRESETS: tuple[AlertPreset, ...] = (
    _preset(
        "platform-degraded",
        "default",
        "A platform component is degraded",
        signal="health.degraded",
        comparator="gt",
        threshold=0,
        for_seconds=300,
        severity="warning",
    ),
    _preset(
        "run-failure-rate",
        "suggested",
        "Run failure rate above 10 %",
        signal="run.failure_rate",
        comparator="gt",
        threshold=0.1,
        window_seconds=900,
        min_samples=10,
        severity="warning",
    ),
    _preset(
        "tasks-waiting",
        "suggested",
        "Tasks waiting over 5 minutes",
        signal="queue.oldest_ready_seconds",
        comparator="gt",
        threshold=300,
        for_seconds=120,
        severity="warning",
    ),
    _preset(
        "no-workers",
        "suggested",
        "No workers online",
        signal="workers.online",
        comparator="lt",
        threshold=1,
        for_seconds=120,
        severity="critical",
    ),
    _preset(
        "runner-offline",
        "suggested",
        "A runner is offline",
        signal="targets.runner_offline",
        comparator="gt",
        threshold=0,
        for_seconds=120,
        severity="warning",
    ),
    _preset(
        "incidents-open",
        "suggested",
        "Incidents are open",
        signal="incidents.active",
        comparator="gt",
        threshold=0,
        severity="info",
    ),
)


def condition_changed(before: AlertRuleDraft, after: AlertRuleDraft) -> bool:
    return any(getattr(before, field) != getattr(after, field) for field in CONDITION)


def preview_of(rule: AlertRuleDraft, reading: Value, at: datetime) -> AlertPreview:
    """What a rule would see now: its value, or why it has none, and whether its condition holds."""
    met = condition_met(rule, reading.value, reading.samples)
    reason: Literal["no_data", "too_few_samples"] | None = None
    if reading.value is None:
        reason = "no_data"
    elif met is None:
        reason = "too_few_samples"
    return AlertPreview(
        value=None if met is None else reading.value,
        unit=ALERT_SIGNALS[rule.signal].unit,
        condition_met=bool(met),
        samples=reading.samples,
        evaluated_at=at,
        no_data_reason=reason,
    )


def _wire(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _document(row: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """A stored rule's fields in wire form, and whether this server knows every key of its selector."""
    selector = dict(row["selector"] or {})
    known = {key: value for key, value in selector.items() if key in AlertSelector.model_fields}
    fields = {name: row[name] for name in _FIELDS}
    fields["selector"] = known
    fields["channel_ids"] = [str(channel) for channel in row["channel_ids"] or ()]
    return fields, len(known) == len(selector)


def evaluable(row: Mapping[str, Any]) -> AlertRuleDraft | None:
    """The stored rule as this server evaluates it, or None when it can't: an unknown signal or selector key."""
    fields, known = _document(row)
    if not known:
        return None
    try:
        rule = AlertRuleDraft.model_validate_json(json.dumps(fields))
        validate_rule(rule)
    except (ValidationError, CatalogError):
        return None
    return rule


def rule_view(row: Mapping[str, Any], state: Mapping[str, Any] | None) -> AlertRuleView:
    """A stored rule with its evaluation state; ``state`` is None before the rule's state row exists."""
    fields, _ = _document(row)
    current = dict(state or {})
    spec = ALERT_SIGNALS.get(row["signal"])
    evaluated_at = current.get("evaluated_at")
    value = current.get("last_value")
    rule_state = AlertRuleState(
        state="unsupported" if evaluable(row) is None else current.get("state") or "ok",
        since=current.get("since") or row["created_at"],
        value=value,
        unit=None if spec is None else spec.unit,
        evaluated_at=evaluated_at,
        error=(
            AlertRuleError(code=current["error_code"], at=current["error_at"]) if current.get("error_code") else None
        ),
        alert_id=current.get("alert_id"),
        no_data=evaluated_at is not None and value is None,
    )
    document = {
        **fields,
        "id": row["id"],
        "revision": row["revision"],
        "preset_id": row["preset_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "state": rule_state.model_dump(mode="json"),
    }
    return AlertRuleView.model_validate_json(json.dumps(document, default=_wire))


def _audited(rule: AlertRuleDraft) -> dict[str, Any]:
    """What the audit keeps of a rule: its definition with channel IDs; rules never hold recipients."""
    return rule.model_dump(mode="json")


def _changed() -> CatalogError:
    return CatalogError(409, "WV-REVISION", "The alert rule changed; read it again before saving")


def _missing() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "Alert rule not found")


def _revision(expected: int | None) -> int:
    if expected is None:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision")
    return expected


class AlertRuleRepository:
    """Rules and their state rows in the transaction's environment."""

    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.keys = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.keys, **values})
        return [dict(row) for row in result.mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.keys, **values})

    async def now(self) -> datetime:
        value: datetime = (await self.rows("SELECT clock_timestamp() AS now"))[0]["now"]
        return value

    async def rule(self, rule_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
        found = await self.rows(
            f"SELECT {_RULE} FROM alert_rules r WHERE r.{SCOPED.replace(' AND ', ' AND r.')} AND r.id=:id"
            + (" FOR UPDATE" if lock else ""),
            id=rule_id,
        )
        return found[0] if found else None

    async def views(self, rule_ids: Iterable[UUID]) -> dict[UUID, AlertRuleView]:
        """The rules with their state, in one statement."""
        rows = await self.rows(
            f"SELECT {_RULE},{_STATE} FROM alert_rules r LEFT JOIN alert_rule_state s ON s.tenant_id=r.tenant_id "
            "AND s.project_id=r.project_id AND s.environment_id=r.environment_id AND s.rule_id=r.id "
            f"WHERE r.{SCOPED.replace(' AND ', ' AND r.')} AND r.id=ANY(:ids)",
            ids=list(rule_ids),
        )
        return {row["id"]: rule_view(row, row if row["state"] is not None else None) for row in rows}

    async def view(self, rule_id: UUID) -> AlertRuleView:
        found = await self.views((rule_id,))
        if rule_id not in found:
            raise _missing()
        return found[rule_id]

    async def count(self) -> int:
        return int((await self.rows(f"SELECT count(*) AS n FROM alert_rules WHERE {SCOPED}"))[0]["n"])

    async def missing_channels(self, channel_ids: Iterable[UUID]) -> list[UUID]:
        wanted = list(channel_ids)
        if not wanted:
            return []
        found = {
            row["id"]
            for row in await self.rows(f"SELECT id FROM alert_channels WHERE {SCOPED} AND id=ANY(:ids)", ids=wanted)
        }
        return [channel for channel in wanted if channel not in found]

    async def target_exists(self, target: UUID) -> bool:
        return bool(await self.rows(f"SELECT 1 FROM deployment_targets WHERE {SCOPED} AND id=:id", id=target))

    async def insert(
        self, rule_id: UUID, rule: AlertRuleDraft, actor: UUID | None, now: datetime, *, preset_id: str | None = None
    ) -> None:
        """A new rule and its state row; ``actor`` is None only for a rule Weave installs from ``preset_id``."""
        await self.execute(
            "INSERT INTO alert_rules(tenant_id,project_id,environment_id,id,name,enabled,signal,selector,comparator,"
            "threshold,window_seconds,for_seconds,min_samples,severity,channel_ids,renotify_seconds,preset_id,"
            "revision,created_at,updated_at,updated_by) VALUES(:tenant,:project,:environment,:id,:name,:enabled,"
            ":signal,cast(:selector AS jsonb),:comparator,:threshold,:window_seconds,:for_seconds,:min_samples,"
            ":severity,cast(:channel_ids AS uuid[]),:renotify_seconds,:preset,1,:now,:now,:actor)",
            **self._values(rule),
            id=rule_id,
            now=now,
            actor=actor,
            preset=preset_id,
        )
        await self.start_state(rule_id, now, insert=True)

    async def update(self, rule_id: UUID, rule: AlertRuleDraft, actor: UUID, now: datetime, expected: int) -> bool:
        updated = await self.rows(
            "UPDATE alert_rules SET name=:name,enabled=:enabled,signal=:signal,selector=cast(:selector AS jsonb),"
            "comparator=:comparator,threshold=:threshold,window_seconds=:window_seconds,for_seconds=:for_seconds,"
            "min_samples=:min_samples,severity=:severity,channel_ids=cast(:channel_ids AS uuid[]),"
            "renotify_seconds=:renotify_seconds,revision=revision+1,updated_at=:now,updated_by=:actor "
            f"WHERE {SCOPED} AND id=:id AND revision=:expected RETURNING revision",
            **self._values(rule),
            id=rule_id,
            now=now,
            actor=actor,
            expected=expected,
        )
        return bool(updated)

    async def delete(self, rule_id: UUID, expected: int) -> bool:
        deleted = await self.rows(
            f"DELETE FROM alert_rules WHERE {SCOPED} AND id=:id AND revision=:expected RETURNING id",
            id=rule_id,
            expected=expected,
        )
        return bool(deleted)

    async def start_state(self, rule_id: UUID, now: datetime, *, insert: bool = False) -> None:
        """A new rule, or one whose condition changed, starts ``ok`` with nothing evaluated."""
        if insert:
            await self.execute(
                "INSERT INTO alert_rule_state(tenant_id,project_id,environment_id,rule_id,state,since,revision) "
                "VALUES(:tenant,:project,:environment,:id,'ok',:now,1)",
                id=rule_id,
                now=now,
            )
            return
        await self.execute(
            "UPDATE alert_rule_state SET state='ok',since=:now,pending_since=NULL,clear_since=NULL,last_value=NULL,"
            "evaluated_at=NULL,error_code=NULL,error_at=NULL,alert_id=NULL,revision=revision+1 "
            f"WHERE {SCOPED} AND rule_id=:id",
            id=rule_id,
            now=now,
        )

    @staticmethod
    def _values(rule: AlertRuleDraft) -> dict[str, Any]:
        return {
            "name": rule.name,
            "enabled": rule.enabled,
            "signal": rule.signal,
            "selector": rule.selector.model_dump_json(),
            "comparator": rule.comparator,
            "threshold": rule.threshold,
            "window_seconds": rule.window_seconds,
            "for_seconds": rule.for_seconds,
            "min_samples": rule.min_samples,
            "severity": rule.severity,
            "channel_ids": list(rule.channel_ids),
            "renotify_seconds": rule.renotify_seconds,
        }


def event_id(alert_id: UUID, kind: str, sequence: int) -> UUID:
    """A retried transaction appends the same event, never a second one."""
    return uuid5(NAMESPACE_URL, f"weave-alert:{alert_id}:{kind}:{sequence}")


async def queue_notifications(
    tx: Transaction,
    alert_id: UUID,
    channel_ids: Iterable[UUID],
    kind: Literal["firing", "resolved", "repeat"],
    sequence: int,
    at: datetime,
) -> None:
    """One pending notification per channel that still exists; the sender checks recipients and budgets later.

    A notification is unique per alert, channel, kind and sequence, so a retried evaluation queues nothing twice.
    """
    channels = list(channel_ids)
    if not channels:
        return
    await AlertRuleRepository(tx).execute(
        "INSERT INTO alert_notifications(tenant_id,project_id,environment_id,id,alert_id,channel_id,kind,sequence,"
        "recipients_count,state,created_at) SELECT tenant_id,project_id,environment_id,gen_random_uuid(),:alert,"
        f"id,:kind,:sequence,jsonb_array_length(recipients),'pending',:at FROM alert_channels WHERE {SCOPED} "
        "AND id=ANY(:channels) ON CONFLICT DO NOTHING",
        alert=alert_id,
        kind=kind,
        sequence=sequence,
        channels=channels,
        at=at,
    )


async def record_transition(
    tx: Transaction,
    outbox: OutboxService,
    alert: Mapping[str, Any],
    *,
    kind: Literal["firing", "resolved"],
    reason: str | None,
    value: float | None,
    channel_ids: Iterable[UUID],
    at: datetime,
) -> None:
    """The alert's history event, a notification per channel and the alert change event, in the caller's transaction.

    ``alert`` is the alert row (``id``, ``rule_id``, ``rule_revision``, ``signal``, ``severity``, ``threshold``).
    """
    repository = AlertRuleRepository(tx)
    await repository.execute(
        "INSERT INTO alert_events(tenant_id,project_id,environment_id,id,alert_id,rule_id,rule_revision,at,kind,value,"
        "reason) VALUES(:tenant,:project,:environment,:id,:alert,:rule,:revision,:at,:kind,:value,:reason) "
        "ON CONFLICT DO NOTHING",
        id=event_id(alert["id"], kind, 0),
        alert=alert["id"],
        rule=alert["rule_id"],
        revision=alert["rule_revision"],
        at=at,
        kind=kind,
        value=value,
        reason=reason,
    )
    await queue_notifications(tx, alert["id"], channel_ids, kind, 0, at)
    facts = AlertEventFacts(
        state=kind, severity=alert["severity"], signal=alert["signal"], value=value, threshold=alert["threshold"]
    )
    await outbox.append(
        tx,
        IntegrationEvent(
            event_id=event_id(alert["id"], kind, 0),
            type="alert.transition",
            scope=tx.scope,
            resource_id=alert["id"],
            correlation_id=alert["rule_id"],
            emitted_at=at,
            payload=EventMetadata(alert=facts),
        ),
    )


async def resolve_firing(
    tx: Transaction,
    outbox: OutboxService,
    rule_id: UUID,
    reason: Literal["rule_changed", "rule_disabled", "rule_deleted"],
    channel_ids: Iterable[UUID],
    at: datetime,
) -> UUID | None:
    """Resolve the rule's firing alert, if any, because the rule itself changed."""
    found = await AlertRuleRepository(tx).rows(
        "SELECT id,rule_id,rule_revision,signal,severity,threshold,last_value FROM alerts "
        f"WHERE {SCOPED} AND rule_id=:rule AND state='firing' FOR UPDATE",
        rule=rule_id,
    )
    if not found:
        return None
    alert = found[0]
    await AlertRuleRepository(tx).execute(
        f"UPDATE alerts SET state='resolved',resolved_at=:at,resolved_reason=:reason WHERE {SCOPED} AND id=:id",
        id=alert["id"],
        at=at,
        reason=reason,
    )
    await record_transition(
        tx, outbox, alert, kind="resolved", reason=reason, value=alert["last_value"], channel_ids=channel_ids, at=at
    )
    alert_id: UUID = alert["id"]
    return alert_id


@service
class AlertRuleService:
    """Rules of one environment. Every call checks the caller's token and then the caller as stored now."""

    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization
        self.outbox = OutboxService()

    def _require(
        self,
        principal: Principal,
        scope: Scope,
        capabilities: tuple[str, ...],
        context: AuditContext,
        rule: AlertRuleDraft | None = None,
    ) -> None:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Alert rules require an environment")
        for capability in capabilities:
            self.authorization.require(principal, scope, capability, context=context)
        # A rule that watches a target needs read access to that target, as the person who sets it.
        if rule is not None and rule.selector.target_id is not None:
            self.authorization.require(
                principal, scope, "deployment.read", resource=str(rule.selector.target_id), context=context
            )

    async def _current(
        self,
        tx: Transaction,
        actor: Principal,
        capabilities: tuple[str, ...],
        context: AuditContext,
        rule: AlertRuleDraft | None = None,
    ) -> Principal:
        current = await load_principal(tx.session, actor.id)
        self._require(current, tx.scope, capabilities, context, rule)
        return current

    async def _check(self, repository: AlertRuleRepository, rule: AlertRuleDraft) -> None:
        missing = await repository.missing_channels(rule.channel_ids)
        if missing:
            raise CatalogError(
                422,
                "WV-ALERT-CHANNEL",
                "Pick email channels of this environment.",
                result={"channel_ids": [str(channel) for channel in missing]},
            )
        target = rule.selector.target_id
        if target is not None and not await repository.target_exists(target):
            raise CatalogError(
                422, "WV-ALERT-SIGNAL", "That target isn't in this environment.", result={"field": "selector"}
            )

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int, cursor: UUID | None, context: AuditContext
    ) -> dict[str, Any]:
        self._require(actor, scope, ("alert.read",), context)
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            repository = AlertRuleRepository(tx)
            after = "" if cursor is None else " AND id>:after"
            ids = [
                row["id"]
                for row in await repository.rows(
                    f"SELECT id FROM alert_rules WHERE {SCOPED}{after} ORDER BY id LIMIT :limit",
                    after=cursor,
                    limit=limit + 1,
                )
            ]
            views = await repository.views(ids[:limit])
            return {
                "items": [views[rule_id].model_dump(mode="json") for rule_id in ids[:limit] if rule_id in views],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def read(self, actor: Principal, scope: Scope, rule_id: UUID, *, context: AuditContext) -> AlertRuleView:
        self._require(actor, scope, ("alert.read",), context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            return await AlertRuleRepository(tx).view(rule_id)

    async def create(
        self, actor: Principal, scope: Scope, rule: AlertRuleDraft, idempotency_key: str, *, context: AuditContext
    ) -> AlertRuleView:
        self._require(actor, scope, ("alert.manage",), context, rule)
        validate_rule(rule)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, ("alert.manage",), context, rule)
            replay = Idempotency(
                tx,
                actor.id,
                f"alert_rules.create:{scope.environment_id}",
                idempotency_key,
                rule.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return AlertRuleView.model_validate_json(json.dumps(prior))
            # Concurrent changes in one environment take turns, so the limit and the channel check hold.
            await lock(tx, f"alert-rules:{scope.environment_id}")
            repository = AlertRuleRepository(tx)
            await self._check(repository, rule)
            if await repository.count() >= RULE_LIMIT:
                raise CatalogError(
                    409, "WV-ALERT-LIMIT", f"An environment holds at most {RULE_LIMIT} alert rules; delete one first."
                )
            rule_id, now = uuid4(), await repository.now()
            await repository.insert(rule_id, rule, actor.id, now)
            await audit(
                tx.session,
                actor,
                "alert_rule.create",
                str(rule_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={"rule": _audited(rule), "revision": 1},
            )
            view = await repository.view(rule_id)
            await replay.save(view.model_dump(mode="json"))
            return view

    async def update(
        self,
        actor: Principal,
        scope: Scope,
        rule_id: UUID,
        rule: AlertRuleDraft,
        expected_revision: int | None,
        *,
        context: AuditContext,
    ) -> AlertRuleView:
        self._require(actor, scope, ("alert.manage",), context, rule)
        expected_revision = _revision(expected_revision)
        validate_rule(rule)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, ("alert.manage",), context, rule)
            await lock(tx, f"alert-rules:{scope.environment_id}")
            repository = AlertRuleRepository(tx)
            before = await repository.rule(rule_id, lock=True)
            if before is None:
                raise _missing()
            if before["revision"] != expected_revision:
                raise _changed()
            await self._check(repository, rule)
            now = await repository.now()
            stored = evaluable(before)
            reason: Literal["rule_changed", "rule_disabled"] | None = None
            if before["enabled"] and not rule.enabled:
                reason = "rule_disabled"
            elif stored is None or condition_changed(stored, rule):
                reason = "rule_changed"
            if reason is not None:
                await resolve_firing(tx, self.outbox, rule_id, reason, before["channel_ids"] or (), now)
                await repository.start_state(rule_id, now)
            if not await repository.update(rule_id, rule, actor.id, now, expected_revision):
                raise _changed()
            await audit(
                tx.session,
                actor,
                "alert_rule.update",
                str(rule_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={
                    "old": _document(before)[0],
                    "new": _audited(rule),
                    "revision": expected_revision + 1,
                    "state_reset": reason is not None,
                },
            )
            return await repository.view(rule_id)

    async def delete(
        self, actor: Principal, scope: Scope, rule_id: UUID, expected_revision: int | None, *, context: AuditContext
    ) -> Revoked:
        self._require(actor, scope, ("alert.manage",), context)
        expected_revision = _revision(expected_revision)
        async with self.uow.open(scope) as tx:
            await self._current(tx, actor, ("alert.manage",), context)
            await lock(tx, f"alert-rules:{scope.environment_id}")
            repository = AlertRuleRepository(tx)
            before = await repository.rule(rule_id, lock=True)
            if before is None:
                raise _missing()
            if before["revision"] != expected_revision:
                raise _changed()
            now = await repository.now()
            await resolve_firing(tx, self.outbox, rule_id, "rule_deleted", before["channel_ids"] or (), now)
            if not await repository.delete(rule_id, expected_revision):
                raise _changed()
            await audit(
                tx.session,
                actor,
                "alert_rule.delete",
                str(rule_id),
                scope=scope,
                capability="alert.manage",
                context=context,
                details={"rule": _document(before)[0], "revision": expected_revision},
            )
            return Revoked()

    async def preview(
        self, actor: Principal, scope: Scope, rule: AlertRuleDraft, *, health: LoopHealth, context: AuditContext
    ) -> AlertPreview:
        """Evaluate the rule now without saving anything; it reveals operations figures, so it needs status.read."""
        needed = ("alert.manage", "status.read")
        self._require(actor, scope, needed, context, rule)
        validate_rule(rule)
        try:
            async with self.uow.open(scope, mutation=False) as tx:
                current = await self._current(tx, actor, needed, context, rule)
                targets = target_permissions(self.authorization, actor, current, scope, "deployment.read", context)
                await tx.session.execute(
                    text("SELECT set_config('statement_timeout',:value,true)"), {"value": PREVIEW_TIMEOUT}
                )
                repository = AlertRuleRepository(tx)
                await self._check(repository, rule.model_copy(update={"channel_ids": ()}))
                now = await repository.now()
                value = await read_signal(tx, health, rule, now, targets=targets)
        except SignalUnavailable:
            raise CatalogError(503, "WV-UNAVAILABLE", "The signal took too long to read. Try again.") from None
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "57014" or error.connection_invalidated:
                raise
            raise CatalogError(503, "WV-UNAVAILABLE", "The signal took too long to read. Try again.") from None
        return preview_of(rule, value, now)

    async def presets(self, actor: Principal, scope: Scope, *, context: AuditContext) -> tuple[AlertPreset, ...]:
        self._require(actor, scope, ("alert.read",), context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._current(tx, actor, ("alert.read",), context)
            installed: dict[str, UUID] = {}
            for row in await AlertRuleRepository(tx).rows(
                f"SELECT preset_id,id FROM alert_rules WHERE {SCOPED} AND preset_id IS NOT NULL ORDER BY created_at,id"
            ):
                installed.setdefault(row["preset_id"], row["id"])
        return tuple(
            preset.model_copy(update={"installed_rule_id": installed.get(preset.id)}) for preset in ALERT_PRESETS
        )
