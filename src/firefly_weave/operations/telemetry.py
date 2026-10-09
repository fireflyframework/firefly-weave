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

"""Finite, value-free instrumentation over application-owned public OTel providers."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry.context import Context
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    SpanContext,
    StatusCode,
    TraceFlags,
    TracerProvider,
    TraceState,
    get_current_span,
    set_span_in_context,
)
from pyfly.container.stereotypes import service
from pyfly.observability.ports import MetricsRecorder

from firefly_weave.contracts.surface import OPERATIONS

SCOPE = "firefly.weave"
LABELS = ("operation", "status", "error_code")
OPERATIONS_ALLOWED = frozenset(OPERATIONS) | frozenset(
    {
        "recovery",
        "deadline",
        "outbox",
        "provider",
        "secret_resolve",
        "retention",
        "compatibility",
        "other",
    }
)
STATUSES = frozenset({"ok", "rejected", "failed", "blocked", "pending", "unknown", "unavailable", "other"})
ERRORS = frozenset(
    {
        "none",
        "other",
        "WV-REQUEST-CAPACITY",
        "WV-REQUEST-LIMIT",
        "WV-REQUEST-TIMEOUT",
        "WV-RUNTIME-LIMIT",
        "WV-QUOTA-LIMIT",
        "WV-COMPATIBILITY",
        "WV-UNAVAILABLE",
        "WV-SECRET-CAPACITY",
        "WV-FORBIDDEN",
        "WV-UNAUTHORIZED",
        "WV-RETENTION",
        "WV-CONFLICT",
        "WV-POLICY-MISMATCH",
    }
)
COUNTERS = {
    "request": "weave.requests",
    "transition": "weave.transitions",
    "recovery": "weave.recoveries",
    "provider": "weave.provider_events",
    "outbox": "weave.outbox_deliveries",
    "secret": "weave.secret_capacity",
    "compatibility": "weave.compatibility",
    "retention": "weave.retention",
    "dropped": "weave.telemetry_dropped",
    "queue_unknown": "weave.queue_unknown",
}
HISTOGRAMS = frozenset({"weave.duration", "weave.queue_age"})
GAUGES = frozenset({"weave.inventory", "weave.inventory_freshness"})
INSTRUMENTS = frozenset(COUNTERS.values()) | HISTOGRAMS | GAUGES
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-(0[01])\Z")


def attributes(operation: str = "other", status: str = "other", error_code: str = "none") -> dict[str, str]:
    return {
        "operation": operation if isinstance(operation, str) and operation in OPERATIONS_ALLOWED else "other",
        "status": status if isinstance(status, str) and status in STATUSES else "other",
        "error_code": error_code if isinstance(error_code, str) and error_code in ERRORS else "other",
    }


def parent_context(value: str | None) -> Context:
    # Explicit empty context rejects ambient baggage, arbitrary parents and tracestate.
    if value is not None and len(value) == 55 and (match := _TRACEPARENT.fullmatch(value)):
        trace_id, span_id, flags = (int(part, 16) for part in match.groups())
        if trace_id and span_id:
            return set_span_in_context(
                NonRecordingSpan(SpanContext(trace_id, span_id, True, TraceFlags(flags), TraceState())), Context()
            )
    return Context()


def _number(value: float, *, negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Telemetry values must be finite numbers")
    if not negative and value < 0:
        raise ValueError("Telemetry values must be nonnegative")
    return value


class MetricHandle:
    def __init__(
        self, recorder: WeaveMetricsRecorder, name: str, labels: tuple[str, ...], values: dict[str, str]
    ) -> None:
        self._recorder, self._name, self._labels, self._values = recorder, name, labels, values

    def labels(self, *args: str, **kwargs: str) -> MetricHandle:
        if args and kwargs or (args and len(args) != len(self._labels)):
            raise ValueError("Metric labels must match the declared keys")
        values = dict(zip(self._labels, args, strict=True)) if args else kwargs
        if set(values) != set(self._labels):
            raise ValueError("Metric labels must match the declared keys")
        return MetricHandle(self._recorder, self._name, self._labels, attributes(**values))

    def inc(self, amount: float = 1) -> None:
        self._recorder._write(self._name, "inc", _number(amount), self._values)

    def dec(self, amount: float = 1) -> None:
        self._recorder._write(self._name, "inc", -_number(amount), self._values)

    def set(self, amount: float) -> None:
        self._recorder._write(self._name, "set", _number(amount), self._values)

    def observe(self, amount: float) -> None:
        self._recorder._write(self._name, "observe", _number(amount), self._values)

    @contextmanager
    def time(self) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.observe(time.monotonic() - start)


class WeaveMetricsRecorder:
    """PyFly MetricsRecorder adapter with fixed instruments and at most 256 series each."""

    def __init__(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter(SCOPE)
        self._instruments: dict[str, Any] = {name: meter.create_counter(name) for name in COUNTERS.values()}
        self._instruments.update({name: meter.create_histogram(name, unit="s") for name in HISTOGRAMS})
        self._instruments.update({name: meter.create_gauge(name) for name in GAUGES})
        self._series: dict[str, set[tuple[str, ...]]] = {name: set() for name in INSTRUMENTS}
        self._gauges: dict[tuple[str, tuple[str, ...]], float] = {}
        self._lock = threading.Lock()

    def _handle(self, name: str, labels: list[str] | None, allowed: frozenset[str]) -> MetricHandle:
        if name not in allowed or (
            labels is not None and (len(set(labels)) != len(labels) or set(labels) - set(LABELS))
        ):
            raise ValueError("Unknown telemetry instrument or label key")
        return MetricHandle(self, name, tuple(labels or ()), attributes())

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> MetricHandle:
        return self._handle(name, labels, frozenset(COUNTERS.values()))

    def histogram(
        self, name: str, description: str, labels: list[str] | None = None, buckets: tuple[float, ...] | None = None
    ) -> MetricHandle:
        return self._handle(name, labels, HISTOGRAMS)

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> MetricHandle:
        return self._handle(name, labels, GAUGES)

    def _write(self, name: str, method: str, value: float, attrs: dict[str, str]) -> None:
        with self._lock:
            key = tuple(attrs[k] for k in LABELS)
            series = self._series[name]
            if key not in series and len(series) >= 255:
                attrs = attributes("other", "other", "other")
                key = tuple(attrs[k] for k in LABELS)
            series.add(key)
            instrument = self._instruments[name]
            if name in GAUGES and method in {"set", "inc"}:
                current = value if method == "set" else self._gauges.get((name, key), 0) + value
                self._gauges[name, key] = current
                instrument.set(current, attrs)
            elif name in HISTOGRAMS and method == "observe":
                instrument.record(value, attrs, context=Context())
            elif name in COUNTERS.values() and method == "inc" and value >= 0:
                instrument.add(value, attrs, context=Context())
            else:
                raise ValueError("Unsupported telemetry instrument operation")


@dataclass(frozen=True)
class ActiveSpan:
    traceparent: str


@service
class TelemetryService:
    """No raw values cross this service; authoritative callers defer records until commit."""

    def __init__(self, metrics_recorder: MetricsRecorder, tracer_provider: TracerProvider) -> None:
        self.metrics = metrics_recorder
        self.tracer = tracer_provider.get_tracer(SCOPE)

    def record(
        self,
        kind: str,
        *,
        operation: str = "other",
        status: str = "other",
        error_code: str = "none",
        duration: float | None = None,
    ) -> None:
        name = COUNTERS.get(kind)
        if name is None:
            raise ValueError("Unknown telemetry event kind")
        attrs = attributes(operation, status, error_code)
        if duration is not None:
            _number(duration)
        self.metrics.counter(name, "", list(LABELS)).labels(**attrs).inc()
        if duration is not None:
            self.metrics.histogram("weave.duration", "", list(LABELS)).labels(**attrs).observe(duration)

    def after_commit(
        self,
        register: Callable[[Callable[[], None]], object],
        kind: str,
        *,
        operation: str = "other",
        status: str = "other",
        error_code: str = "none",
        duration: float | None = None,
    ) -> None:
        if kind not in COUNTERS:
            raise ValueError("Unknown telemetry event kind")
        attrs = attributes(operation, status, error_code)
        if duration is not None:
            _number(duration)
        if register(lambda: self.record(kind, **attrs, duration=duration)) is False:
            self.record("dropped", operation="other", status="rejected")

    @contextmanager
    def span(
        self, operation: str, *, traceparent: str | None = None, links: Sequence[str] = ()
    ) -> Iterator[ActiveSpan]:
        attrs = attributes(operation)
        safe_links = []
        for value in links[:8]:
            candidate = get_current_span(parent_context(value)).get_span_context()
            if candidate.is_valid:
                safe_links.append(Link(candidate))
        with self.tracer.start_as_current_span(
            "weave." + attrs["operation"],
            context=parent_context(traceparent),
            attributes=attrs,
            links=safe_links,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            ctx = span.get_span_context()
            carrier = (
                f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags) & 1:02x}" if ctx.is_valid else ""
            )
            try:
                yield ActiveSpan(carrier)
            except BaseException:
                span.set_attribute("status", "failed")
                span.set_status(StatusCode.ERROR)
                raise
            else:
                span.set_attribute("status", "ok")
                span.set_status(StatusCode.OK)

    def inventory(self, operation: str, count: int, *, observed_age: float, complete: bool) -> None:
        # An incomplete sweep must leave the last complete value untouched.
        if complete:
            attrs = attributes(operation, "ok")
            self.metrics.gauge("weave.inventory", "", list(LABELS)).labels(**attrs).set(count)
            self.metrics.gauge("weave.inventory_freshness", "", list(LABELS)).labels(**attrs).set(observed_age)

    def queue_age(self, operation: str, seconds: float | None) -> None:
        if seconds is None:
            self.record("queue_unknown", operation=operation, status="unknown")
        else:
            self.metrics.histogram("weave.queue_age", "", list(LABELS)).labels(**attributes(operation, "ok")).observe(
                seconds
            )
