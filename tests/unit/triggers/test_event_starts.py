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

"""Starting the workflows of platform events: which refusals wait for a later scan, which reject the start and
why, when a start that never got going expires, and how starts are counted."""

from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.contracts.event_triggers import EVENT_START_EXPIRY
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder
from firefly_weave.runtime.capacity import RuntimeCapacityError
from firefly_weave.triggers.event_starts import start_expired, start_refusal

REQUESTED = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


class Failure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def database(sqlstate: str) -> DBAPIError:
    return DBAPIError("UPDATE", {}, Failure(sqlstate))


@pytest.mark.parametrize(
    "error",
    [
        database("WQ001"),
        database("55P03"),
        CatalogError(429, "WV-OPERATION-CAPACITY", "Operation capacity unavailable"),
        CatalogError(429, "WV-REQUEST-CAPACITY", "Request capacity unavailable"),
        CatalogError(409, "WV-QUOTA-LIMIT", "Quota exhausted"),
        CatalogError(409, "WV-COMPATIBILITY", "Compatibility check pending"),
        CatalogError(503, "WV-UNAVAILABLE", "Platform unavailable"),
    ],
    ids=["spent quota", "busy lock", "operation capacity", "request capacity", "quota", "compatibility", "503"],
)
def test_a_busy_lock_a_spent_quota_or_capacity_starts_later(error):
    assert start_refusal(error) == "later"


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (AccessDenied(), "WV-FORBIDDEN"),
        (AuthenticationFailed(), "WV-FORBIDDEN"),
        (CatalogError(422, "WV-READINESS", "Retired activation or workflow cannot start"), "WV-READINESS"),
        (CatalogError(422, "WV-RUNTIME-INPUT", "Invalid run input"), "WV-RUNTIME-INPUT"),
        (CatalogError(404, "WV-NOT-FOUND", "Activation not found"), "WV-NOT-FOUND"),
        # Bounded budgets are refused the same way on every attempt, though they answer 429.
        (CatalogError(429, "WV-RUNTIME-LIMIT", "Retained run exceeds the bounded fetch budget"), "WV-RUNTIME-LIMIT"),
        (CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit"), "WV-PAGE-LIMIT"),
        (RuntimeCapacityError(), "WV-RUNTIME-LIMIT"),
    ],
    ids=[
        "lost grant",
        "unknown principal",
        "retired activation",
        "bad input",
        "missing activation",
        "run budget",
        "page budget",
        "size",
    ],
)
def test_a_lost_grant_a_retired_activation_or_a_bad_input_rejects_the_start_with_its_cause(error, cause):
    assert start_refusal(error) == cause


def test_any_other_failure_is_not_a_refusal():
    with pytest.raises(ValueError, match="refusal"):
        start_refusal(KeyError("missing"))
    with pytest.raises(ValueError, match="refusal"):
        start_refusal(database("40001"))


def test_a_start_that_never_got_going_expires_a_day_after_its_event():
    assert timedelta(hours=24) == EVENT_START_EXPIRY
    assert not start_expired(REQUESTED, REQUESTED + EVENT_START_EXPIRY - timedelta(seconds=1))
    assert start_expired(REQUESTED, REQUESTED + EVENT_START_EXPIRY)


def test_event_starts_are_counted_with_bounded_labels():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    service.record("event_start", operation="event.start", status="ok")
    service.record("event_start", operation="event.start", status="rejected", error_code="WV-TRIGGER-START-EXPIRED")
    for code in ("WV-TRIGGER-DISABLED", "WV-TRIGGER-EVENT-DEPTH", "WV-TRIGGER-CHAIN-BUDGET"):
        service.record("event_start", operation="event.start", status="blocked", error_code=code, amount=2)
    service.record("event_start", operation="event.resolve", status="ok")
    found = [
        (dict(point.attributes), point.value)
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "weave.event_starts"
        for point in metric.data.data_points
    ]
    assert sorted((labels["operation"], labels["status"], labels["error_code"], value) for labels, value in found) == [
        ("event.resolve", "ok", "none", 1),
        ("event.start", "blocked", "WV-TRIGGER-CHAIN-BUDGET", 2),
        ("event.start", "blocked", "WV-TRIGGER-DISABLED", 2),
        ("event.start", "blocked", "WV-TRIGGER-EVENT-DEPTH", 2),
        ("event.start", "ok", "none", 1),
        ("event.start", "rejected", "WV-TRIGGER-START-EXPIRED", 1),
    ]
    meters.shutdown()
