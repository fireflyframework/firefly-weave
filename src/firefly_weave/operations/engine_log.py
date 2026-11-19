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

"""The engine's own log entries, derived from what a run records and never stored.

A run's log shows one entry per run event, worded by a fixed table; across runs, the log shows runs that failed,
timed out or were canceled, and steps that failed or timed out, from the run and step facts. An entry reads only the
step and an error code: never an event's output, input, reason or actor.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.contracts.log_values import LogLevel
from firefly_weave.contracts.run_views import LOG_LEVELS, ExecutionLogEntry, RunLogEntry
from firefly_weave.operations.facts import author_instance

_CODE_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
_CODE: Final = re.compile(_CODE_PATTERN)
MESSAGE_LIMIT: Final = 512


@dataclass(frozen=True)
class EngineTemplate:
    """An event's level, its code (None: the event's own code), and its words; {step} and {code} are filled in."""

    level: LogLevel
    code: str | None
    message: str
    # The words when the event carries no code.
    uncoded: str | None = None


ENGINE_ENTRIES: Final = MappingProxyType(
    {
        "started": EngineTemplate("info", "RUN.STARTED", "Run started"),
        "task_completed": EngineTemplate("info", "STEP.SUCCEEDED", "{step} finished"),
        "task_failed": EngineTemplate("warning", None, "{step} attempt failed: {code}", "{step} attempt failed"),
        "recovery_scheduled": EngineTemplate("warning", "STEP.RETRY", "{step} will be retried"),
        # A handled failure is a warning; the run goes on.
        "step_failed": EngineTemplate("error", None, "{step} failed: {code}", "{step} failed"),
        "signal_received": EngineTemplate("info", "SIGNAL.RECEIVED", "Signal received"),
        "human_completed": EngineTemplate("info", "HUMAN_TASK.COMPLETED", "{step} was completed"),
        "paused": EngineTemplate("info", "RUN.PAUSED", "Run paused"),
        "resumed": EngineTemplate("info", "RUN.RESUMED", "Run resumed"),
        "wait_elapsed": EngineTemplate("info", "WAIT.ELAPSED", "{step} reached its deadline"),
        "timed_out": EngineTemplate("error", "RUN.TIMED_OUT", "Run timed out"),
        "incident_opened": EngineTemplate(
            "error", None, "Incident opened at {step}: {code}", "Incident opened at {step}"
        ),
        "incident_resolved": EngineTemplate("info", "INCIDENT.RESOLVED", "Incident resolved"),
        "cancelled": EngineTemplate("info", "RUN.CANCELED", "Run canceled"),
        "call_completed": EngineTemplate("info", "CALL.COMPLETED", "{step} call finished"),
        # Says only that the conversation was loaded: never its messages or its key hash.
        "memory_loaded": EngineTemplate(
            "info",
            None,
            "{step} couldn't read its conversation and started without it: {code}",
            "{step} loaded its conversation",
        ),
    }
)
HANDLED_SUFFIX: Final = "; the failure was handled"
RANKS: Final = MappingProxyType({level: rank for rank, level in enumerate(LOG_LEVELS)})


def rank_sql(event_type: str, handled: str) -> str:
    """The SQL for an event's level rank, from the table above; ``handled`` is a boolean SQL expression."""
    cases = " ".join(
        f"WHEN '{name}' THEN "
        + (
            f"CASE WHEN {handled} THEN {RANKS['warning']} ELSE {RANKS['error']} END"
            if name == "step_failed"
            else str(RANKS[template.level])
        )
        for name, template in ENGINE_ENTRIES.items()
    )
    return f"CASE {event_type} {cases} END"


def log_code(value: object) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def code_sql(column: str, fallback: str) -> str:
    """The code an entry shows, in SQL, so a filter on it finds what is shown: ``column`` when it is a log code (as
    ``log_code`` decides), otherwise ``fallback``."""
    return f"CASE WHEN {column} ~ '^{_CODE_PATTERN}$' THEN {column} ELSE '{fallback}' END"


def _step(key: object) -> tuple[str | None, str]:
    """The author step and instance key an event names, or none for keys only the engine uses."""
    if not isinstance(key, str):
        return None, ""
    try:
        identity = author_instance(key)
    except ValueError:
        return None, ""
    if identity is None:
        return None, ""
    return identity.node_id, identity.instance_key


def _words(template: str, step: str | None, code: str | None) -> str:
    return template.format(step=step or "A step", code=code)[:MESSAGE_LIMIT]


def engine_entry(
    identifier: UUID, event_type: str, at: datetime, key: object, code: object, *, handled: bool
) -> RunLogEntry:
    """One run event as a log entry: the step it names and its code, worded by ``ENGINE_ENTRIES``."""
    template = ENGINE_ENTRIES[event_type]
    node_id, instance_key = _step(key)
    shown = log_code(code) if template.code is None else template.code
    words = template.message if shown is not None or template.uncoded is None else template.uncoded
    message = _words(words, instance_key or node_id, shown)
    level = template.level
    if event_type == "step_failed" and handled:
        level, message = "warning", (message + HANDLED_SUFFIX)[:MESSAGE_LIMIT]
    return RunLogEntry(
        id=identifier,
        at=at,
        source="engine",
        level=level,
        code=shown,
        message=message,
        node_id=node_id,
        instance_key=instance_key,
        attempt=None,
        worker_id=None,
        fields={},
        redactions=0,
        truncated=False,
    )


def fact_entry_id(run_id: UUID, key: str, status: str) -> UUID:
    """A stable ID for an entry derived from facts: the same run, step and status give the same entry."""
    return uuid5(NAMESPACE_URL, f"firefly-weave/log/{run_id}/{key}/{status}")


def run_fact_entry(
    run_id: UUID, status: str, at: datetime, code: object, *, workflow: str, version: str, test: bool
) -> ExecutionLogEntry:
    """A run that failed, timed out or was canceled, as an entry of the log across runs."""
    level: LogLevel
    if status == "failed":
        shown = log_code(code) or "RUN.FAILED"
        level, message = "error", f"Run failed: {shown}"
    elif status == "timed_out":
        level, shown, message = "error", "RUN.TIMED_OUT", "Run timed out"
    else:
        level, shown, message = "info", "RUN.CANCELED", "Run canceled"
    return ExecutionLogEntry(
        id=fact_entry_id(run_id, "", status),
        at=at,
        source="engine",
        level=level,
        code=shown,
        message=message,
        node_id=None,
        instance_key="",
        attempt=None,
        worker_id=None,
        fields={},
        redactions=0,
        truncated=False,
        run_id=run_id,
        workflow_name=workflow,
        workflow_version=version,
        test=test,
    )


def step_fact_entry(
    run_id: UUID,
    node_id: str,
    instance_key: str,
    status: str,
    at: datetime,
    code: object,
    *,
    handled: bool,
    workflow: str,
    version: str,
    test: bool,
) -> ExecutionLogEntry:
    """A step that failed or timed out, as an entry of the log across runs."""
    step = instance_key or node_id
    level: LogLevel
    if status == "timed_out":
        level, shown, message = "error", "STEP.TIMED_OUT", _words("{step} timed out", step, None)
    else:
        shown = log_code(code) or "STEP.FAILED"
        level, message = ("warning" if handled else "error"), _words("{step} failed: {code}", step, shown)
        if handled:
            message = (message + HANDLED_SUFFIX)[:MESSAGE_LIMIT]
    return ExecutionLogEntry(
        id=fact_entry_id(run_id, step, status),
        at=at,
        source="engine",
        level=level,
        code=shown,
        message=message,
        node_id=node_id,
        instance_key=instance_key,
        attempt=None,
        worker_id=None,
        fields={},
        redactions=0,
        truncated=False,
        run_id=run_id,
        workflow_name=workflow,
        workflow_version=version,
        test=test,
    )
