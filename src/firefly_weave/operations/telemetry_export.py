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

"""Owned OTLP encoders and bounded export queue; no environment-driven exporters."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricReader, MetricsData
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from firefly_weave.connectors.http_logging import protected_http_diagnostics
from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.operations.telemetry import INSTRUMENTS, LABELS, OPERATIONS_ALLOWED, SCOPE, attributes


class OTLPTransport:
    """One bounded request per batch; failures drop the batch and never log remote data."""

    def __init__(self, endpoint: str, options: TelemetryOptions) -> None:
        self.endpoint, self.options = endpoint, options

    def send(self, payload: bytes) -> bool:
        if len(payload) > 1_048_576:
            return False
        headers = {"Content-Type": "application/x-protobuf"}
        if self.options.authorization is not None:
            headers["Authorization"] = self.options.authorization.get_secret_value()
        try:
            # Scope includes client acquisition and close, including HTTP debug logging.
            with (
                protected_http_diagnostics(),
                httpx.Client(
                    timeout=self.options.timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream("POST", self.endpoint, headers=headers, content=payload) as response,
            ):
                # No response content is useful to this value-free best-effort exporter.
                return 200 <= response.status_code < 300
        except Exception:
            return False


class OwnedSpanExporter(SpanExporter):
    def __init__(self, transport: OTLPTransport, dropped: Callable[[int], None]) -> None:
        self.transport, self.dropped, self.closed = transport, dropped, False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.closed:
            return SpanExportResult.FAILURE
        try:
            ok = self.transport.send(encode_spans(spans).SerializeToString())
        except Exception:
            ok = False
        if not ok:
            self.dropped(len(spans))
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self.closed = True


class OwnedMetricExporter(MetricExporter):
    def __init__(self, transport: OTLPTransport, dropped: Callable[[int], None]) -> None:
        super().__init__()
        self.transport, self.dropped, self.closed = transport, dropped, False

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> MetricExportResult:
        if self.closed:
            return MetricExportResult.FAILURE
        try:
            # Views exclude unrelated instruments; validate again at the export boundary.
            for resource in metrics_data.resource_metrics:
                if dict(resource.resource.attributes) != {"service.name": "firefly-weave"}:
                    raise ValueError("Unknown metric resource")
                for scope in resource.scope_metrics:
                    if scope.scope.name != SCOPE:
                        raise ValueError("Unknown metric scope")
                    for metric in scope.metrics:
                        if metric.name not in INSTRUMENTS:
                            raise ValueError("Unknown instrument")
                        for point in metric.data.data_points:
                            attrs = dict(point.attributes or {})
                            if set(attrs) != set(LABELS) or attrs != attributes(**attrs):
                                raise ValueError("Unclassified metric attributes")
            ok = self.transport.send(encode_metrics(metrics_data).SerializeToString())
        except Exception:
            ok = False
        if not ok:
            self.dropped(1)
        return MetricExportResult.SUCCESS if ok else MetricExportResult.FAILURE

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return not self.closed

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        self.closed = True


class OwnedPeriodicMetricReader(MetricReader):
    """Allocate before starting; one owned ticker with bounded flush/shutdown waits."""

    def __init__(self, exporter: MetricExporter, interval: float, timeout: float, dropped: Callable[[int], None]):
        super().__init__()
        self.exporter, self.interval, self.timeout, self.dropped = exporter, interval, timeout, dropped
        self._condition = threading.Condition()
        self._export_lock = threading.Lock()
        self._stopping = False
        self._requested = 0
        self._completed = 0
        self._thread = threading.Thread(target=self._run, name="weave-metrics", daemon=True)

    def start(self) -> None:
        # The configuration and MeterProvider own this reader before any worker exists.
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._stopping or self._requested > self._completed, timeout=self.interval
                    )
                    if self._stopping:
                        return
                    requested = self._requested
                try:
                    self.collect(timeout_millis=self.timeout * 1000)
                except Exception:
                    self.dropped(1)
                finally:
                    with self._condition:
                        self._completed = requested
                        self._condition.notify_all()
        finally:
            self.exporter.shutdown(timeout_millis=5000)

    def _receive_metrics(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> None:
        if not self._export_lock.acquire(timeout=min(max(timeout_millis, 0), 5000) / 1000):
            self.dropped(1)
            return
        try:
            if not self._stopping:
                self.exporter.export(metrics_data, timeout_millis=min(timeout_millis, self.timeout * 1000))
        finally:
            self._export_lock.release()

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        with self._condition:
            if self._stopping or not self._thread.is_alive():
                return False
            self._requested += 1
            requested = self._requested
            self._condition.notify_all()
            self._condition.wait_for(
                lambda: self._stopping or self._completed >= requested,
                timeout=min(max(timeout_millis, 0), 5000) / 1000,
            )
            return not self._stopping and self._completed >= requested

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._condition.notify_all()
        if self._thread.ident is None:
            self.exporter.shutdown(timeout_millis=5000)
        elif self._thread is not threading.current_thread():
            # An in-flight request owns deferred cleanup if it outlives this finite wait.
            self._thread.join(timeout=min(max(timeout_millis, 0), 5500) / 1000)


class BoundedSpanProcessor(SpanProcessor):
    """One daemon exporter, 256 queued spans, 64 per request, no retry spool."""

    def __init__(self, exporter: SpanExporter, dropped: Callable[[int], None]) -> None:
        self.exporter, self.dropped = exporter, dropped
        self._queue: deque[ReadableSpan] = deque()
        self._condition = threading.Condition()
        self._stopping = False
        self._inflight = False
        self._thread = threading.Thread(target=self._run, name="weave-telemetry", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            with self._condition:
                self._stopping = True
                self._condition.notify_all()
            if self._thread.ident is None:
                exporter.shutdown()
            else:
                # Thread.start may start the worker and then raise; that worker owns cleanup.
                self._thread.join(timeout=5.5)
            raise

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        attrs = dict(span.attributes or {})
        if (
            span.instrumentation_scope is None
            or span.instrumentation_scope.name != SCOPE
            or span.name not in {"weave." + op for op in OPERATIONS_ALLOWED}
            or set(attrs) != set(LABELS)
            or attrs != attributes(**attrs)
            or span.events
            or len(span.links) > 8
            or any(link.attributes or link.context.trace_state for link in span.links)
            or span.status.description
            or dict(span.resource.attributes) != {"service.name": "firefly-weave"}
        ):
            self.dropped(1)
            return
        with self._condition:
            if self._stopping or len(self._queue) >= 256:
                self.dropped(1)
                return
            self._queue.append(span)
            self._condition.notify_all()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: bool(self._queue) or self._stopping)
                    if self._stopping:
                        self.dropped(len(self._queue))
                        self._queue.clear()
                        return
                    batch = [self._queue.popleft() for _ in range(min(64, len(self._queue)))]
                    self._inflight = True
                try:
                    self.exporter.export(batch)
                except Exception:
                    self.dropped(len(batch))
                finally:
                    with self._condition:
                        self._inflight = False
                        self._condition.notify_all()
        finally:
            self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        deadline = time.monotonic() + min(max(timeout_millis, 0), 5000) / 1000
        with self._condition:
            while self._queue or self._inflight:
                left = deadline - time.monotonic()
                if left <= 0 or self._stopping:
                    return False
                self._condition.wait(left)
            return True

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5.5)
