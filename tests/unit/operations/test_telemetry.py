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

"""Exporter and recording boundaries use real SDK collection and hostile inputs."""

import json

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError
from pyfly.core.config import Config

from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.observability import OwnedMeterConfiguration, OwnedTracingConfiguration
from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder


def test_options_reject_secret_urls_and_invalid_bounds():
    for endpoint in (
        "https://u:CANARY@collector.test/v1/traces",
        "https://collector.test?token=CANARY",
        "http://collector.test/v1/traces",
    ):
        with pytest.raises(ValidationError) as error:
            TelemetryOptions(enabled=True, traces_endpoint=endpoint)
        assert "CANARY" not in str(error.value)
    with pytest.raises(ValidationError):
        TelemetryOptions(enabled=True)
    with pytest.raises(ValidationError):
        TelemetryOptions(enabled=True, traces_endpoint="http://127.0.0.1:4318/v1/traces", timeout_seconds=6)


def test_disabled_never_constructs_exporter_from_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://ambient-CANARY.test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=CANARY")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "not-a-sampler-CANARY")
    monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "garbage-CANARY")

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled export constructed a transport")

    monkeypatch.setattr("firefly_weave.observability.OTLPTransport", forbidden)
    meters, traces = OwnedMeterConfiguration(), OwnedTracingConfiguration()
    original_meter = __import__("opentelemetry.metrics", fromlist=["get_meter_provider"]).get_meter_provider()
    original_tracer = trace.get_tracer_provider()
    meters.meter_provider(Config({}))
    traces.tracer_provider(Config({}))
    meters.close()
    meters.close()
    traces.close()
    traces.close()
    assert meters.closed and traces.closed
    assert trace.get_tracer_provider() is original_tracer
    assert __import__("opentelemetry.metrics", fromlist=["get_meter_provider"]).get_meter_provider() is original_meter


@pytest.fixture
def instruments():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    traces = TracerProvider(shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    service = TelemetryService(WeaveMetricsRecorder(meters), traces)
    yield service, reader, exporter, meters
    meters.shutdown()
    traces.shutdown()


def test_thousands_of_untrusted_labels_collapse_before_sdk(instruments):
    service, reader, _, _ = instruments
    for index in range(2000):
        service.record("request", operation=f"https://CANARY/{index}", status=str(index), error_code=str(index))
    data = reader.get_metrics_data()
    metrics = [m for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics]
    points = [p for m in metrics for p in m.data.data_points]
    assert len(points) == 1
    assert dict(points[0].attributes) == {"operation": "other", "status": "other", "error_code": "other"}
    assert points[0].value == 2000
    assert "CANARY" not in data.to_json()


def test_pending_events_are_sanitized_and_only_emitted_after_commit(instruments):
    service, reader, _, _ = instruments
    pending = []
    service.after_commit(pending.append, "transition", operation="recovery", status="ok")
    assert reader.get_metrics_data() is None
    pending.pop()()
    assert "weave.transitions" in reader.get_metrics_data().to_json()
    service.after_commit(pending.append, "transition", operation="recovery", status="ok")
    pending.clear()  # A rolled-back outer transaction discards its callbacks.
    data = reader.get_metrics_data()
    assert next(iter(data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points)).value == 1


def test_span_has_no_ambient_context_baggage_or_exception_text(instruments):
    service, _, exporter, _ = instruments
    ctx = baggage.set_baggage("secret", "CANARY")
    token = context.attach(ctx)
    try:
        with pytest.raises(ValueError), service.span("https://CANARY/path"):
            raise ValueError("CANARY exception")
    finally:
        context.detach(token)
    span = exporter.get_finished_spans()[0]
    assert span.name == "weave.other"
    assert span.parent is None
    assert not span.events
    assert "CANARY" not in span.to_json()
    assert span.status.status_code == trace.StatusCode.ERROR


def test_recorder_implements_public_port_without_dynamic_names(instruments):
    _, reader, _, meters = instruments
    recorder = WeaveMetricsRecorder(meters)
    counter = recorder.counter("weave.requests", "fixed", ["operation", "status", "error_code"])
    counter.labels(operation="recovery", status="ok", error_code="none").inc()
    with pytest.raises(ValueError):
        recorder.counter("CANARY", "untrusted")
    with pytest.raises(ValueError):
        recorder.counter("weave.requests", "fixed", ["tenant"])
    assert "CANARY" not in reader.get_metrics_data().to_json()


def test_explicit_traceparent_validates_and_drops_tracestate(instruments):
    service, _, exporter, _ = instruments
    parent = "00-11111111111111111111111111111111-2222222222222222-01"
    with service.span("recovery", traceparent=parent) as active:
        assert active.traceparent.startswith("00-11111111111111111111111111111111-")
    with service.span("recovery", traceparent=parent + "CANARY"):
        pass
    spans = exporter.get_finished_spans()
    assert spans[0].parent.span_id == int("2222222222222222", 16)
    assert spans[1].parent is None
    assert "CANARY" not in json.dumps([s.to_json() for s in spans])


def test_enabled_export_ignores_ambient_secrets_and_uses_owned_headers(monkeypatch, caplog):
    import logging

    import httpx
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from firefly_weave.observability import TelemetryDrops

    sent = []
    original = httpx.Client

    def receive(request):
        sent.append(request)
        return httpx.Response(200)

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-leak=AMBIENT-CANARY")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://AMBIENT-CANARY.test")
    monkeypatch.setenv("HTTPS_PROXY", "http://AMBIENT-CANARY.test")
    caplog.set_level(logging.DEBUG)
    options = TelemetryOptions(
        enabled=True,
        traces_endpoint="https://collector.test/v1/traces",
        metrics_endpoint="https://collector.test/v1/metrics",
        authorization="Bearer OWNER-CANARY",
    )
    drops = TelemetryDrops()
    meters, traces = OwnedMeterConfiguration(options, drops), OwnedTracingConfiguration(options, drops)
    meter, tracer = meters.meter_provider(Config({})), traces.tracer_provider(Config({}))
    service = TelemetryService(WeaveMetricsRecorder(meter), tracer)
    try:
        service.record("request", operation="recovery", status="ok")
        with service.span("recovery"):
            pass
        assert tracer.force_flush(5000)
        assert meter.force_flush(5000)
        assert len(sent) == 2
        assert {str(request.url) for request in sent} == {options.traces_endpoint, options.metrics_endpoint}
        assert all(request.headers["authorization"] == "Bearer OWNER-CANARY" for request in sent)
        assert all("x-leak" not in request.headers for request in sent)
        assert "CANARY" not in caplog.text
        request = next(request for request in sent if request.url.path == "/v1/traces")
        proto = ExportTraceServiceRequest.FromString(request.content)
        assert proto.resource_spans[0].scope_spans[0].spans[0].name == "weave.recovery"
    finally:
        traces.close()
        meters.close()


def test_full_transaction_callback_buffer_counts_drop(instruments):
    service, reader, _, _ = instruments
    service.after_commit(lambda callback: False, "transition", operation="recovery", status="ok")
    data = reader.get_metrics_data().to_json()
    assert "weave.telemetry_dropped" in data
    assert "weave.transitions" not in data


def test_incomplete_inventory_preserves_last_complete_gauges(instruments):
    service, reader, _, _ = instruments
    service.inventory("recovery", 5, observed_age=0, complete=True)
    service.inventory("recovery", 900, observed_age=0, complete=False)
    points = {
        m.name: m.data.data_points[0].value
        for r in reader.get_metrics_data().resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
    }
    assert points["weave.inventory"] == 5


def test_bounded_span_queue_drops_and_closes_owned_exporter():
    import threading

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    from firefly_weave.operations.telemetry_export import BoundedSpanProcessor

    entered, release = threading.Event(), threading.Event()
    dropped, batches = [], []

    class Exporter(SpanExporter):
        closed = 0

        def export(self, spans):
            batches.append(len(spans))
            entered.set()
            assert release.wait(3)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            self.closed += 1

    exporter = Exporter()
    processor = BoundedSpanProcessor(exporter, dropped.append)
    provider = TracerProvider(resource=Resource({"service.name": "firefly-weave"}), shutdown_on_exit=False)
    provider.add_span_processor(processor)
    meter = MeterProvider(shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meter), provider)
    try:
        with service.span("recovery"):
            pass
        assert entered.wait(3)
        for _ in range(300):
            with service.span("recovery"):
                pass
        assert sum(dropped) == 44
        assert processor.force_flush(1) is False
        release.set()
        assert processor.force_flush(3000)
        assert sum(batches) == 257 and max(batches) <= 64
    finally:
        release.set()
        provider.shutdown()
        meter.shutdown()
    processor.shutdown()
    assert exporter.closed == 1


@pytest.mark.parametrize("starts", [False, True])
def test_export_thread_start_failure_does_not_leave_owned_thread(monkeypatch, starts):
    import threading

    from opentelemetry.sdk.trace.export import SpanExporter

    from firefly_weave.operations.telemetry_export import BoundedSpanProcessor

    class Exporter(SpanExporter):
        closed = 0

        def export(self, spans):
            raise AssertionError("No queued spans")

        def shutdown(self):
            self.closed += 1

    threads = []
    original = threading.Thread.start

    def fail(thread):
        threads.append(thread)
        if starts:
            original(thread)
        raise RuntimeError("thread failed")

    monkeypatch.setattr(threading.Thread, "start", fail)
    exporter = Exporter()
    with pytest.raises(RuntimeError):
        BoundedSpanProcessor(exporter, lambda count: None)
    assert exporter.closed == 1
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("starts", [False, True])
@pytest.mark.parametrize("previously_closed", [False, True])
def test_metric_thread_start_failure_is_owned(monkeypatch, starts, previously_closed):
    import threading

    from firefly_weave.observability import OwnedMeterConfiguration
    from firefly_weave.operations.telemetry_export import OwnedMetricExporter

    threads, closed = [], []
    original = threading.Thread.start

    def fail(thread):
        threads.append(thread)
        if starts:
            original(thread)
        raise RuntimeError("thread failed")

    monkeypatch.setattr(threading.Thread, "start", fail)
    monkeypatch.setattr(OwnedMetricExporter, "shutdown", lambda self, **kwargs: closed.append(self))
    owner = OwnedMeterConfiguration(TelemetryOptions(enabled=True, metrics_endpoint="http://localhost:4318/v1/metrics"))
    if previously_closed:
        owner.close()
    try:
        with pytest.raises(RuntimeError, match="thread failed"):
            owner.meter_provider(Config({}))
        owner.close()
        assert len(closed) == 1
        assert all(not thread.is_alive() for thread in threads)
    finally:
        # Preserve a failing regression without leaking the SDK constructor's worker.
        for thread in threads:
            if thread.is_alive():
                thread._target.__self__.shutdown(timeout_millis=1000)


def test_metric_provider_constructor_failure_after_closed_generation_cleans_new_reader(monkeypatch):
    from firefly_weave import observability
    from firefly_weave.operations.telemetry_export import OwnedMetricExporter

    closed = []
    monkeypatch.setattr(OwnedMetricExporter, "shutdown", lambda self, **kwargs: closed.append(self))
    owner = OwnedMeterConfiguration(TelemetryOptions(enabled=True, metrics_endpoint="http://localhost:4318/v1/metrics"))
    owner.meter_provider(Config({}))
    owner.close()

    def fail(**kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(observability, "MeterProvider", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        owner.meter_provider(Config({}))
    owner.close()
    assert len(closed) == 2
    assert closed[0] is not closed[1]


def test_metric_reader_flush_wait_and_deferred_cleanup_are_bounded():
    import threading
    import time

    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    from firefly_weave.operations.telemetry_export import OwnedPeriodicMetricReader

    entered, release, closed = threading.Event(), threading.Event(), []

    class Exporter(MetricExporter):
        def export(self, metrics_data, **kwargs):
            entered.set()
            assert release.wait(3)
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=1000):
            return True

        def shutdown(self, **kwargs):
            closed.append(True)

    reader = OwnedPeriodicMetricReader(Exporter(), 300, 1, lambda count: None)
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    reader.start()
    provider.get_meter("test").create_counter("test.counter").add(1)
    try:
        assert not reader.force_flush(0)
        assert entered.wait(1)
        started = time.monotonic()
        assert not reader.force_flush(10)
        reader.shutdown(timeout_millis=10)
        assert time.monotonic() - started < 0.5
        assert not closed
        assert not reader.force_flush(10)
    finally:
        release.set()
        reader._thread.join(timeout=1)
        provider.shutdown()
    assert not reader._thread.is_alive()
    assert closed == [True]


async def test_native_provider_port_and_service_restart_without_globals():
    from opentelemetry.metrics import MeterProvider as MeterAPI
    from opentelemetry.trace import TracerProvider as TraceAPI
    from pyfly.context.application_context import ApplicationContext
    from pyfly.observability.ports import MetricsRecorder

    context = ApplicationContext(
        Config({"pyfly": {"observability": {"metrics": {"enabled": False}, "tracing": {"enabled": False}}}})
    )
    owners = (OwnedMeterConfiguration(), OwnedTracingConfiguration())
    for owner in owners:
        context.register_bean(type(owner))
        context.container.register_instance(type(owner), owner)
    context.register_bean(TelemetryService)
    generations = []
    for _ in range(2):
        await context.start()
        try:
            service = context.get_bean(TelemetryService)
            assert service.metrics is context.get_bean(MetricsRecorder)
            assert isinstance(service.metrics, WeaveMetricsRecorder)
            generations.append((context.get_bean(MeterAPI), context.get_bean(TraceAPI)))
            service.record("request", operation="recovery", status="ok")
        finally:
            await context.stop()
        for owner in owners:
            assert owner.closed
            owner.close()
    assert all(left is not right for left, right in zip(*generations, strict=True))


def test_durable_links_are_bounded_and_have_no_baggage(instruments):
    service, _, exporter, _ = instruments
    carrier = "00-11111111111111111111111111111111-2222222222222222-01"
    with service.span("provider", links=[carrier] * 10 + ["CANARY"]):
        pass
    span = exporter.get_finished_spans()[0]
    assert len(span.links) == 8
    assert all(not link.attributes and not link.context.trace_state for link in span.links)
    assert span.status.status_code == trace.StatusCode.OK
    assert span.attributes["status"] == "ok"


def test_unhashable_runtime_labels_cannot_break_recording(instruments):
    service, reader, _, _ = instruments
    service.record("request", operation=["CANARY"], status={"CANARY": "value"}, error_code=None)
    data = reader.get_metrics_data().to_json()
    assert "CANARY" not in data
    assert '"value": 1' in data
