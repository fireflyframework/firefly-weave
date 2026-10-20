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

"""Call counters and call latencies use the bounded metric labels."""

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder
from firefly_weave.runtime.calls import OUTCOMES, call_result


def points(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict, float]]]:
    found: dict[str, list[tuple[dict, float]]] = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    found.setdefault(metric.name, []).append(
                        (dict(point.attributes), value if value is not None else point.count)
                    )
    return found


def test_each_call_outcome_counts_once_by_mode_and_status():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    for status in ("succeeded", "failed", "cancelled", "timed_out", "rejected"):
        service.record("call", operation="call.wait", status=OUTCOMES[status])
    service.record("call", operation="call.detach", status="ok", amount=2)
    service.queue_age("call.dispatch", 0.25)
    service.queue_age("call.delivery", 0.5)
    found = points(reader)
    assert sorted((labels["operation"], labels["status"], value) for labels, value in found["weave.calls"]) == [
        ("call.detach", "ok", 2),
        ("call.wait", "cancelled", 1),
        ("call.wait", "failed", 1),
        ("call.wait", "ok", 1),
        ("call.wait", "rejected", 1),
        ("call.wait", "timed_out", 1),
    ]
    assert sorted(labels["operation"] for labels, _ in found["weave.queue_age"]) == ["call.delivery", "call.dispatch"]
    meters.shutdown()


@pytest.mark.parametrize(
    ("status", "output", "code", "kept"),
    [
        ("succeeded", {"tier": "gold"}, None, ({"tier": "gold"}, None)),
        (
            "failed",
            {"code": "NO_CUSTOMER", "message": "Unknown customer."},
            "NO_CUSTOMER",
            ({"code": "NO_CUSTOMER", "message": "Unknown customer."}, "NO_CUSTOMER"),
        ),
        (
            "failed",
            None,
            "WV-RUNTIME-LIMIT",
            ({"code": "WV-RUNTIME-LIMIT", "message": "The called run failed."}, "WV-RUNTIME-LIMIT"),
        ),
        (
            "cancelled",
            None,
            None,
            ({"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."}, "WV-RUN-CANCELLED"),
        ),
        (
            "timed_out",
            None,
            None,
            ({"code": "WV-RUN-TIMED_OUT", "message": "The called run timed out."}, "WV-RUN-TIMED_OUT"),
        ),
    ],
)
def test_a_call_row_keeps_the_child_output_or_a_code_and_message(status, output, code, kept):
    assert call_result(status, output, code) == kept
