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

"""Alert rules, alerts, their history, email channels and settings as they cross the wire.

A rule compares one signal from a fixed list with a threshold. Which selector, window and threshold range a signal
allows is checked against the signal registry (``firefly_weave.operations.alert_signals.validate_rule``); this module
checks what holds for every signal. Alert change events and the environment summary carry numbers and states only:
never a rule name, a selector value or a recipient.
"""

import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, field_validator, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.email import address
from firefly_weave.contracts.run_views import Cursor, _digest, utc_time
from firefly_weave.contracts.values import UnicodeString

# The signals this server evaluates; firefly_weave.operations.alert_signals.ALERT_SIGNALS describes each one. A
# signal's name is never reused for another meaning, so a stored rule keeps meaning what it meant.
AlertSignal = Literal[
    "run.failure_rate",
    "run.failures",
    "run.duration_p95",
    "queue.oldest_ready_seconds",
    "queue.ready_tasks",
    "outbox.pending",
    "provider.pending",
    "incidents.active",
    "workers.online",
    "workers.free_slots",
    "targets.runner_offline",
    "deployments.reconciliation_required",
    "health.degraded",
]
# Any signal name, for records a newer or older server may have written.
type SignalName = Annotated[str, Field(min_length=3, max_length=100, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
type AlertUnit = Literal["count", "ratio", "seconds"]
type Comparator = Literal["gt", "gte", "lt", "lte"]
type Severity = Literal["info", "warning", "critical"]
type ResolvedReason = Literal["threshold", "no_data", "rule_changed", "rule_disabled", "rule_deleted"]
type Count = Annotated[int, Field(ge=0)]
type Revision = Annotated[int, Field(ge=1)]
type Threshold = Annotated[float, Field(ge=0, le=1e9, allow_inf_nan=False)]
type Reading = Annotated[float, Field(allow_inf_nan=False)]
type PresetId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")]
type TaskTypeName = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")]
type NotificationCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z_]*$")]
type ErrorCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
# A domain as stored: IDNA (A-labels), lowercase, at least two labels.
type Domain = Annotated[
    str,
    Field(
        min_length=3,
        max_length=253,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
    ),
]
# A domain as an administrator types it; the server normalizes it to a Domain.
type DomainInput = Annotated[UnicodeString, Field(min_length=1, max_length=253, pattern=r"^[^\s/@:]+$")]

RULE_NAME_LENGTH = 120
CHANNEL_NAME_LENGTH = 80
RULE_CHANNELS = 5
CHANNEL_RECIPIENTS = 10
RECIPIENT_DOMAINS = 20
APPROVAL_CHANNELS = 5
ALERT_NOTIFICATIONS = 20
PAGE_LIMIT = 100
# Repeat notifications wait at least this long; 0 means never repeat.
RENOTIFY_MIN_SECONDS = 900
RENOTIFY_MAX_SECONDS = 86_400


def plain_text(value: str) -> str:
    """Names reach mail subjects and screens: visible text only, never control, format or line-separator
    characters."""
    if not value.strip() or any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value):
        raise ValueError("Use visible text without control or format characters")
    return value


def mailbox(value: str) -> str:
    return address(value)


def renotify(value: int) -> int:
    if value != 0 and not RENOTIFY_MIN_SECONDS <= value <= RENOTIFY_MAX_SECONDS:
        raise ValueError("Repeat notifications wait 900 to 86400 seconds, or 0 for never")
    return value


type RuleName = Annotated[UnicodeString, Field(min_length=1, max_length=RULE_NAME_LENGTH), AfterValidator(plain_text)]
type ChannelName = Annotated[
    UnicodeString, Field(min_length=1, max_length=CHANNEL_NAME_LENGTH), AfterValidator(plain_text)
]
type EmailAddress = Annotated[UnicodeString, Field(min_length=3, max_length=254), AfterValidator(mailbox)]
# How an outside recipient agreed to get alert mail, as the administrator who records the opt-in states it.
type ConsentStatement = Annotated[UnicodeString, Field(min_length=1, max_length=200), AfterValidator(plain_text)]
type WindowSeconds = Literal[60, 300, 900, 3600]
type ForSeconds = Annotated[int, Field(ge=0, le=3600, multiple_of=30)]
type MinSamples = Annotated[int, Field(ge=1, le=10_000)]
type RenotifySeconds = Annotated[int, Field(ge=0), AfterValidator(renotify)]


def distinct(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    if len(set(values)) != len(values):
        raise ValueError("Name each channel once")
    return values


class AlertSelector(ContractModel):
    """Which series of the signal a rule reads: at most one key, and only a key the signal allows."""

    workflow: ResourceName | None = Field(default=None, exclude_if=lambda value: value is None)
    task_type: TaskTypeName | None = Field(default=None, exclude_if=lambda value: value is None)
    target_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def one_key(self) -> "AlertSelector":
        if len(self.names()) > 1:
            raise ValueError("A selector names at most one of workflow, task_type and target_id")
        return self

    def names(self) -> frozenset[str]:
        return frozenset(name for name in type(self).model_fields if getattr(self, name) is not None)


class _AlertRuleFields(ContractModel):
    name: RuleName
    enabled: bool = True
    signal: SignalName
    selector: AlertSelector = AlertSelector()
    comparator: Comparator
    threshold: Threshold
    # Required for window signals and null for gauges (alert_signals.validate_rule).
    window_seconds: WindowSeconds | None = None
    for_seconds: ForSeconds = 0
    # Rate and quantile signals compare only once at least this many runs ended in the window.
    min_samples: MinSamples = 1
    severity: Severity
    channel_ids: Annotated[tuple[UUID, ...], Field(max_length=RULE_CHANNELS), AfterValidator(distinct)] = ()
    renotify_seconds: RenotifySeconds = 0


class AlertRuleDraft(_AlertRuleFields):
    signal: AlertSignal


class AlertRuleError(ContractModel):
    code: ErrorCode
    at: AwareDatetime


class AlertRuleState(ContractModel):
    """Where a rule stands. ``unsupported``: this server doesn't know its signal; ``unauthorized``: the access its
    target needs is gone. Neither is evaluated."""

    state: Literal["ok", "pending", "firing", "unsupported", "unauthorized"]
    since: AwareDatetime
    value: Reading | None = Field(default=None, exclude_if=lambda value: value is None)
    # Unknown while the server doesn't know the signal.
    unit: AlertUnit | None = Field(default=None, exclude_if=lambda value: value is None)
    evaluated_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    error: AlertRuleError | None = Field(default=None, exclude_if=lambda value: value is None)
    alert_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    no_data: bool


class AlertRuleView(_AlertRuleFields):
    id: UUID
    revision: Revision
    preset_id: PresetId | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # None for a rule Weave installed from a preset and nobody has changed since.
    updated_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    state: AlertRuleState


class AlertNotificationView(ContractModel):
    channel_id: UUID
    kind: Literal["firing", "resolved", "repeat", "test"]
    state: Literal["pending", "sent", "failed", "skipped_limit"]
    # A fixed code, never a mail server's text.
    code: NotificationCode | None = Field(default=None, exclude_if=lambda value: value is None)
    at: AwareDatetime


class AlertView(ContractModel):
    """One firing period of one rule, from firing to resolved."""

    id: UUID
    rule_id: UUID
    rule_name: RuleName
    rule_revision: Revision
    signal: SignalName
    severity: Severity
    state: Literal["firing", "resolved"]
    started_at: AwareDatetime
    resolved_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    resolved_reason: ResolvedReason | None = Field(default=None, exclude_if=lambda value: value is None)
    comparator: Comparator
    threshold: Threshold
    unit: AlertUnit
    value_at_start: Reading
    last_value: Reading | None = Field(default=None, exclude_if=lambda value: value is None)
    acknowledged_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    acknowledged_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    notifications: list[AlertNotificationView] = Field(default_factory=list, max_length=ALERT_NOTIFICATIONS)


class AlertPage(ContractModel):
    items: list[AlertView] = Field(max_length=PAGE_LIMIT)
    next_cursor: Cursor | None
    firing: Count
    # Firing alerts with severity critical, in the same scope and query as ``firing``.
    critical: Count


class AlertEvent(ContractModel):
    id: UUID
    alert_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    rule_id: UUID
    rule_revision: Revision
    at: AwareDatetime
    kind: Literal["firing", "resolved", "acknowledged"]
    value: Reading | None = Field(default=None, exclude_if=lambda value: value is None)
    reason: ResolvedReason | None = Field(default=None, exclude_if=lambda value: value is None)


class MemberRecipient(ContractModel):
    """A person with a grant in the project, reached at the alert address they saved for themselves."""

    member: UUID


class AddressRecipient(ContractModel):
    """An outside address: it needs an allowed domain and an opt-in an administrator recorded."""

    address: EmailAddress


type AlertRecipient = MemberRecipient | AddressRecipient


class AlertChannelDraft(ContractModel):
    kind: Literal["email"]
    name: ChannelName
    connection_revision_id: UUID
    recipients: list[AlertRecipient] = Field(min_length=1, max_length=CHANNEL_RECIPIENTS)


class AlertRecipientState(ContractModel):
    index: Annotated[int, Field(ge=0, lt=CHANNEL_RECIPIENTS)]
    state: Literal["ok", "not_member", "without_address", "domain_not_allowed", "without_consent"]


class AlertChannel(AlertChannelDraft):
    id: UUID
    owner_id: UUID
    revision: Revision
    recipient_states: list[AlertRecipientState] = Field(max_length=CHANNEL_RECIPIENTS)
    status: Literal["ok", "owner_unauthorized"]


class AlertRecipientConsentDraft(ContractModel):
    address: EmailAddress
    statement: ConsentStatement


class AlertRecipientConsent(ContractModel):
    """An outside address's opt-in for one environment; it can only be revoked."""

    id: UUID
    domain: Domain
    address: EmailAddress
    method: Literal["administrator_attested", "recipient_confirmed"]
    statement: ConsentStatement
    created_by: UUID
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    revoked_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)


class MyAlertAddress(ContractModel):
    """The address alerts sent to the signed-in person as a project member go to; only they set it."""

    address: EmailAddress | None = Field(default=None, exclude_if=lambda value: value is None)
    updated_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)


class MyAlertAddressUpdate(ContractModel):
    # None removes the address.
    address: EmailAddress | None = None


class AlertSettings(ContractModel):
    recipient_domains: list[Domain] = Field(max_length=RECIPIENT_DOMAINS)
    approval_channel_ids: list[UUID] = Field(max_length=APPROVAL_CHANNELS)
    # 0 until the settings are first saved.
    revision: Annotated[int, Field(ge=0)]


class AlertSettingsUpdate(ContractModel):
    recipient_domains: list[DomainInput] = Field(max_length=RECIPIENT_DOMAINS)
    approval_channel_ids: list[UUID] = Field(max_length=APPROVAL_CHANNELS)

    @field_validator("approval_channel_ids")
    @classmethod
    def distinct_channels(cls, values: list[UUID]) -> list[UUID]:
        distinct(tuple(values))
        return values


class AlertPreview(ContractModel):
    value: Reading | None
    unit: AlertUnit
    condition_met: bool
    samples: Count | None = Field(default=None, exclude_if=lambda value: value is None)
    evaluated_at: AwareDatetime
    # Why there is no value: nothing to read, or fewer ended runs than the rule's min_samples.
    no_data_reason: Literal["no_data", "too_few_samples"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class AlertPreset(ContractModel):
    id: PresetId
    version: Revision
    # Default presets are installed once in each environment; suggested ones are only offered.
    install: Literal["default", "suggested"]
    name: RuleName
    rule: AlertRuleDraft
    installed_rule_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def no_channel(self) -> "AlertPreset":
        if self.rule.channel_ids:
            raise ValueError("A preset sends nowhere until someone picks a channel")
        return self


class AlertEventFacts(ContractModel):
    """What an alert change event says about the alert: numbers and states, never names, selectors or recipients."""

    state: Literal["firing", "resolved"]
    severity: Severity
    signal: SignalName
    value: Reading | None
    threshold: Threshold


class AlertsSection(ContractModel):
    firing: Count
    critical: Count


class _AlertHistoryQuery(ContractModel):
    """A list of alerts or of their history, newest first; the cursor is bound to every filter."""

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    rule_id: UUID | None = None
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None
    limit: Annotated[int, Field(ge=1, le=PAGE_LIMIT)] = 50
    cursor: Cursor | None = None

    @field_validator("from_", "to", mode="before")
    @classmethod
    def utc(cls, value: object) -> datetime | None:
        return utc_time(value)

    @model_validator(mode="after")
    def ordered(self) -> "_AlertHistoryQuery":
        if self.from_ is not None and self.to is not None and self.from_ >= self.to:
            raise ValueError("from must be earlier than to")
        return self

    def cursor_collection(self, name: str) -> str:
        filters = self.model_dump(mode="json", exclude={"limit", "cursor"})
        return f"{name}:{_digest(filters)}"


class AlertQuery(_AlertHistoryQuery):
    state: Literal["firing", "resolved"] | None = None
    severity: Severity | None = None


class AlertEventQuery(_AlertHistoryQuery):
    pass


type RetainedRecord = Literal[
    "run_statistics_5m",
    "run_statistics_1h",
    "run_statistics_1d",
    "samples_1m",
    "samples_1h",
    "change_requests",
    "alert_events",
    "alerts",
]


class RetentionPeriod(ContractModel):
    record: RetainedRecord
    seconds: Annotated[int, Field(ge=1)]


class RetentionPeriods(ContractModel):
    """How long the platform keeps each kind of operations record."""

    items: list[RetentionPeriod] = Field(max_length=32)
