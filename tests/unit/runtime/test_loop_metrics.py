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

"""Loop counters: collected and cancelled iterations and yields, counted per transition."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from test_loop_runtime import Run, compiled, each, send

from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder
from firefly_weave.runtime.repository import loop_activity


def test_a_transition_counts_the_iterations_it_collected_and_the_yields_it_registered():
    flow = compiled([each("each", [], {"ref": "/item"}, maxItems=10_000)], {"ref": "/steps/each/output"})
    run = Run(flow, list(range(2100)))
    assert loop_activity(run.start()) == (2000, 0, 1)
    assert loop_activity(
        run.send("wait_elapsed", node_id="each~1", deadline=run.state.waits["each~1"].isoformat())
    ) == (
        100,
        0,
        0,
    )


def test_a_run_that_ends_counts_its_cancelled_iterations(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=3)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2, 3, 4])
    assert loop_activity(run.start()) == (0, 0, 0)
    assert loop_activity(run.complete("send[1]", 2)) == (1, 0, 0)
    assert loop_activity(run.send("cancelled")) == (0, 3, 0)


def test_loop_counters_add_their_counts_after_commit():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    pending: list = []
    service.after_commit(pending.append, "loop_iteration", operation="loop", status="ok", amount=2000)
    service.after_commit(pending.append, "loop_yield", operation="loop", status="ok", amount=1)
    for callback in pending:
        callback()
    points = {
        metric.name: [(dict(point.attributes), point.value) for point in metric.data.data_points]
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    labels = {"operation": "loop", "status": "ok", "error_code": "none"}
    assert points == {"weave.loop_iterations": [(labels, 2000)], "weave.loop_yields": [(labels, 1)]}
    for amount in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            service.record("loop_iteration", operation="loop", amount=amount)
    meters.shutdown()
