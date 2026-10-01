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

"""Native owned providers with opt-in, bounded, value-free telemetry export.

No process globals, ambient endpoints/headers/samplers, implicit resource detectors
or automatic exception events are used. Each provider generation has one owner.
"""

import threading

from opentelemetry.metrics import MeterProvider as MeterProviderAPI
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.view import DropAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import TracerProvider as TracerProviderAPI
from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context.lifecycle import pre_destroy
from pyfly.core.config import Config
from pyfly.observability.ports import MetricsRecorder

from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.operations.telemetry import INSTRUMENTS, LABELS, SCOPE, WeaveMetricsRecorder, attributes
from firefly_weave.operations.telemetry_export import (
    BoundedSpanProcessor,
    OTLPTransport,
    OwnedMetricExporter,
    OwnedPeriodicMetricReader,
    OwnedSpanExporter,
)


class TelemetryDrops:
    """A saturating diagnostic counter remains bounded even when no exporter is available."""

    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()
        self.provider: MeterProviderAPI | None = None

    def add(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self.count = min((1 << 63) - 1, self.count + count)
            provider = self.provider
        if provider is not None:
            provider.get_meter(SCOPE).create_counter("weave.telemetry_dropped").add(
                count, attributes("other", "rejected")
            )


@configuration
class OwnedMeterConfiguration:
    def __init__(self, options: TelemetryOptions | None = None, drops: TelemetryDrops | None = None) -> None:
        self.options = options or TelemetryOptions()
        self.drops = drops or TelemetryDrops()
        self.provider: MeterProvider | None = None
        self.closed = False

    @bean
    def meter_provider(self, config: Config) -> MeterProviderAPI:
        readers = []
        exporter = None
        provider = None
        try:
            if self.options.enabled and self.options.metrics_endpoint:
                exporter = OwnedMetricExporter(
                    OTLPTransport(self.options.metrics_endpoint, self.options), self.drops.add
                )
                readers.append(
                    OwnedPeriodicMetricReader(
                        exporter,
                        self.options.metrics_interval_seconds,
                        self.options.timeout_seconds,
                        self.drops.add,
                    )
                )
            views = [View(instrument_name="*", aggregation=DropAggregation())]
            views.extend(
                View(instrument_name=name, meter_name=SCOPE, attribute_keys=set(LABELS)) for name in INSTRUMENTS
            )
            self.provider = provider = MeterProvider(
                metric_readers=readers,
                resource=Resource({"service.name": "firefly-weave"}),
                shutdown_on_exit=False,
                exemplar_filter=AlwaysOffExemplarFilter(),
                views=views,
            )
            self.closed = False
            for reader in readers:
                reader.start()
        except BaseException:
            if provider is not None:
                self.close()
            elif readers:
                for reader in readers:
                    reader.shutdown(timeout_millis=5000)
            elif exporter is not None:
                exporter.shutdown()
            raise
        self.drops.provider = self.provider
        self.closed = False
        return self.provider

    @bean
    def metrics_recorder(self, meter_provider: MeterProviderAPI) -> MetricsRecorder:
        return WeaveMetricsRecorder(meter_provider)

    @pre_destroy
    def close(self) -> None:
        if self.provider is not None and not self.closed:
            self.closed = True
            self.drops.provider = None
            self.provider.shutdown(timeout_millis=5500)
        self.closed = True


@configuration
class OwnedTracingConfiguration:
    def __init__(self, options: TelemetryOptions | None = None, drops: TelemetryDrops | None = None) -> None:
        self.options = options or TelemetryOptions()
        self.drops = drops or TelemetryDrops()
        self.provider: TracerProvider | None = None
        self.closed = False

    @bean
    def tracer_provider(self, config: Config) -> TracerProviderAPI:
        self.provider = TracerProvider(
            resource=Resource({"service.name": "firefly-weave"}),
            shutdown_on_exit=False,
            sampler=ALWAYS_ON,
            span_limits=SpanLimits(
                max_attributes=3,
                max_events=0,
                max_links=8,
                max_span_attributes=3,
                max_event_attributes=0,
                max_link_attributes=0,
                max_attribute_length=128,
                max_span_attribute_length=128,
            ),
        )
        self.closed = False
        try:
            if self.options.enabled and self.options.traces_endpoint:
                exporter = OwnedSpanExporter(OTLPTransport(self.options.traces_endpoint, self.options), self.drops.add)
                self.provider.add_span_processor(BoundedSpanProcessor(exporter, self.drops.add))
        except BaseException:
            self.close()
            raise
        return self.provider

    @pre_destroy
    def close(self) -> None:
        if self.provider is not None and not self.closed:
            self.closed = True
            self.provider.shutdown()
        self.closed = True
