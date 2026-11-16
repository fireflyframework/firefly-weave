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

"""The signals alert rules can watch, what each one measures, and the rule fields each one allows.

The registry is closed: a signal is added here, with its reader, and never renamed or given another meaning. Adding a
signal, a selector key a signal accepts, or a preset is additive; a stored rule whose signal this server doesn't know
reads as unsupported and is never evaluated. Test runs never contribute to any signal.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from firefly_weave.contracts.alerts import AlertRuleDraft, AlertUnit
from firefly_weave.definitions.models import CatalogError


@dataclass(frozen=True)
class SignalSpec:
    unit: AlertUnit
    # A window signal sums or rates what ended within the rule's window; a gauge is read as it is now.
    kind: Literal["window", "gauge"]
    # The selector keys a rule on this signal may use; a rule reads the whole environment without one.
    selectors: frozenset[str]
    # Rate and quantile signals compare only once the rule's min_samples runs ended in the window.
    sampled: bool = False


_WORKFLOW = frozenset({"workflow"})
_TASK_TYPE = frozenset({"task_type"})
_TARGET = frozenset({"target_id"})
_NONE: frozenset[str] = frozenset()

ALERT_SIGNALS: Mapping[str, SignalSpec] = MappingProxyType(
    {
        "run.failure_rate": SignalSpec("ratio", "window", _WORKFLOW, sampled=True),
        "run.failures": SignalSpec("count", "window", _WORKFLOW),
        "run.duration_p95": SignalSpec("seconds", "window", _WORKFLOW, sampled=True),
        "queue.oldest_ready_seconds": SignalSpec("seconds", "gauge", _TASK_TYPE),
        "queue.ready_tasks": SignalSpec("count", "gauge", _TASK_TYPE),
        "outbox.pending": SignalSpec("count", "gauge", _NONE),
        "provider.pending": SignalSpec("count", "gauge", _NONE),
        "incidents.active": SignalSpec("count", "gauge", _NONE),
        "workers.online": SignalSpec("count", "gauge", _NONE),
        "workers.free_slots": SignalSpec("count", "gauge", _NONE),
        "targets.runner_offline": SignalSpec("count", "gauge", _TARGET),
        "deployments.reconciliation_required": SignalSpec("count", "gauge", _TARGET),
        "health.degraded": SignalSpec("count", "gauge", _NONE),
    }
)

_MESSAGES = {
    "signal": "This server doesn't evaluate that signal.",
    "selector": "This signal can't be narrowed that way.",
    "window_seconds": "Window signals need a window, and current-value signals take none.",
    "threshold": "A rate threshold is between 0 and 1.",
    "min_samples": "Only failure rates and durations count ended runs.",
}


def _refuse(field: str) -> CatalogError:
    return CatalogError(422, "WV-ALERT-SIGNAL", _MESSAGES[field], result={"field": field})


def validate_rule(rule: AlertRuleDraft) -> None:
    """Refuse the rule fields its signal doesn't allow; the contract has checked what holds for every signal."""
    spec = ALERT_SIGNALS.get(rule.signal)
    if spec is None:
        raise _refuse("signal")
    if not rule.selector.names() <= spec.selectors:
        raise _refuse("selector")
    if (spec.kind == "window") != (rule.window_seconds is not None):
        raise _refuse("window_seconds")
    if spec.unit == "ratio" and rule.threshold > 1:
        raise _refuse("threshold")
    if not spec.sampled and rule.min_samples != 1:
        raise _refuse("min_samples")
