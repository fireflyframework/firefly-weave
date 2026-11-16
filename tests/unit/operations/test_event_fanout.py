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

"""Which triggers a transaction event reaches, how deep the started runs sit in a chain, and what they receive."""

import json
from uuid import UUID

import pytest

from firefly_weave.contracts.integration_events import IntegrationEvent
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.run_views import RunSummaryCaller
from firefly_weave.operations.event_fanout import (
    SourceFacts,
    StartRow,
    TriggerMatch,
    event_payload,
    record_skips,
    start_rows,
)
from firefly_weave.operations.facts import RunStartFacts

RUN = UUID("0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c04")
IN_DOUBT, COMPENSATED = "transaction.in_doubt", "transaction.compensated"


def event(kind: str = IN_DOUBT, *, workflow: str = "move-between-systems", test: bool = False) -> IntegrationEvent:
    scope = {
        "tenant_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c0a",
        "project_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c0b",
        "environment_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c0c",
    }
    facts = {
        "workflow": {"name": workflow, "version": "1.0.0"},
        "run_id": str(RUN),
        "instance_key": "move",
        "business_key": "M-1",
        "state": kind.removeprefix("transaction."),
        "steps": {"done": [{"step": "hold", "instance_key": "hold", "how": "reserve"}]},
        "test": test,
    }
    return IntegrationEvent.model_validate_json(
        json.dumps(
            {
                "event_id": "0c6f2b55-3a1e-4f63-b1f4-0a7e5e9d1c03",
                "type": kind,
                "scope": scope,
                "resource_id": str(RUN),
                "correlation_id": str(RUN),
                "emitted_at": "2026-11-15T10:00:00Z",
                "payload": {"transaction": facts},
            }
        )
    )


def trigger(number: int, events=(IN_DOUBT,), *, workflow: str = "notify-team", sources=None) -> TriggerMatch:
    return TriggerMatch(id=UUID(int=number), workflow_name=workflow, events=tuple(events), source_workflows=sources)


def source(
    *,
    workflow: str = "move-between-systems",
    depth: int = 0,
    test: bool = False,
    root: UUID | None = None,
    spent: int = 0,
) -> SourceFacts:
    return SourceFacts(
        run_id=RUN, workflow_name=workflow, depth=depth, test=test, root_event_id=root, chain_starts=spent
    )


def offered(triggers, incoming=None, origin=None) -> list[tuple[int, str, int, str | None]]:
    rows = start_rows(triggers, incoming or event(), origin or source())
    return [(row.trigger_id.int, row.status, row.depth, row.code) for row in rows]


def test_a_trigger_accepts_only_its_event_types():
    triggers = [trigger(2, (COMPENSATED,)), trigger(1, (IN_DOUBT, COMPENSATED)), trigger(3, (IN_DOUBT,))]
    assert offered(triggers) == [(1, "requested", 1, None), (3, "requested", 1, None)]
    assert offered(triggers, event(COMPENSATED)) == [(1, "requested", 1, None), (2, "requested", 1, None)]


def test_a_trigger_limited_to_source_workflows_ignores_other_workflows():
    triggers = [trigger(1, sources=["restock"]), trigger(2, sources=["move-between-systems", "restock"])]
    assert offered(triggers) == [(2, "requested", 1, None)]
    assert offered(triggers, origin=source(workflow="restock")) == [
        (1, "requested", 1, None),
        (2, "requested", 1, None),
    ]


def test_a_trigger_ignores_its_own_workflow_unless_from_names_it():
    own = source(workflow="notify-team")
    assert offered([trigger(1)], origin=own) == []
    assert offered([trigger(1, sources=["notify-team"])], origin=own) == [(1, "requested", 1, None)]


def test_events_of_test_runs_start_nothing():
    assert offered([trigger(1)], event(test=True), source(test=True)) == []


def test_events_from_a_run_three_reactions_deep_are_recorded_as_skipped():
    assert offered([trigger(1), trigger(2)], origin=source(depth=2)) == [
        (1, "requested", 3, None),
        (2, "requested", 3, None),
    ]
    assert offered([trigger(1), trigger(2)], origin=source(depth=3)) == [
        (1, "skipped", 4, "WV-TRIGGER-EVENT-DEPTH"),
        (2, "skipped", 4, "WV-TRIGGER-EVENT-DEPTH"),
    ]


def test_a_chain_of_reactions_starts_at_most_64_workflows_then_records_skips():
    root = UUID(int=99)
    triggers = [trigger(number) for number in range(6, 0, -1)]
    # The chain that started with root has made 60 starts: four more fit, in trigger order.
    assert offered(triggers, origin=source(depth=1, root=root, spent=60)) == [
        *[(number, "requested", 2, None) for number in (1, 2, 3, 4)],
        *[(number, "skipped", 2, "WV-TRIGGER-CHAIN-BUDGET") for number in (5, 6)],
    ]
    assert offered(triggers, origin=source(depth=2, root=root, spent=64)) == [
        (number, "skipped", 3, "WV-TRIGGER-CHAIN-BUDGET") for number in range(1, 7)
    ]
    # Past the deepest reaction a chain ends whatever its budget, and says so.
    assert offered(triggers[-1:], origin=source(depth=3, root=root, spent=64)) == [
        (1, "skipped", 4, "WV-TRIGGER-EVENT-DEPTH")
    ]


class Recorder:
    """What a transaction would count once it commits."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, str, int]] = []

    def record(self, kind: str, operation: str, status: str = "ok", error_code: str = "none", *, amount: int = 1):
        self.records.append((kind, operation, status, error_code, amount))


def test_starts_a_chain_never_makes_are_counted_as_blocked_where_they_are_written():
    recorder = Recorder()
    rows = [
        StartRow(UUID(int=1), 4, "skipped", "WV-TRIGGER-EVENT-DEPTH"),
        StartRow(UUID(int=2), 2, "requested"),
        StartRow(UUID(int=3), 2, "skipped", "WV-TRIGGER-CHAIN-BUDGET"),
        StartRow(UUID(int=4), 2, "skipped", "WV-TRIGGER-CHAIN-BUDGET"),
    ]
    record_skips(recorder, rows)  # type: ignore[arg-type]
    assert sorted(recorder.records) == [
        ("event_start", "event.start", "blocked", "WV-TRIGGER-CHAIN-BUDGET", 2),
        ("event_start", "event.start", "blocked", "WV-TRIGGER-EVENT-DEPTH", 1),
    ]
    quiet = Recorder()
    record_skips(quiet, [StartRow(UUID(int=2), 1, "requested")])  # type: ignore[arg-type]
    assert quiet.records == []


def test_the_start_payload_is_the_platform_event():
    incoming = event()
    payload = event_payload(incoming)
    assert isinstance(payload, PlatformEvent)
    dumped = payload.model_dump(mode="json")
    assert set(dumped) == {"version", "event_id", "type", "emitted_at", "transaction"}
    assert (payload.event_id, payload.type, payload.emitted_at) == (
        incoming.event_id,
        incoming.type,
        incoming.emitted_at,
    )
    assert dumped["transaction"] == incoming.payload.model_dump(mode="json")["transaction"]
    with pytest.raises(ValueError, match="transaction"):
        event_payload(
            incoming.model_copy(update={"payload": incoming.payload.model_copy(update={"transaction": None})})
        )


def test_a_run_start_carries_its_chain_depth():
    assert RunStartFacts(origin="manual").event_depth == 0
    caller = RunSummaryCaller(run_id=RUN, node_id="notify", instance_key="")
    assert RunStartFacts(origin="call", caller=caller, event_depth=2, root_event_id=RUN).event_depth == 2
    for depth in (-1, True, "1"):
        with pytest.raises(ValueError, match="depth"):
            RunStartFacts(origin="manual", event_depth=depth)  # type: ignore[arg-type]


def test_an_event_start_sits_deeper_than_its_source():
    root = UUID(int=7)
    started = RunStartFacts(origin="event", event_depth=1, root_event_id=root)
    assert (started.origin, started.event_depth, started.root_event_id) == ("event", 1, root)
    with pytest.raises(ValueError, match="platform event"):
        RunStartFacts(origin="event")


def test_a_run_in_a_chain_names_the_event_that_started_the_chain():
    root = UUID(int=7)
    assert RunStartFacts(origin="manual").root_event_id is None
    caller = RunSummaryCaller(run_id=RUN, node_id="notify", instance_key="")
    assert RunStartFacts(origin="call", caller=caller, event_depth=1, root_event_id=root).root_event_id == root
    for depth, named in ((1, None), (0, root), (1, str(root))):
        with pytest.raises(ValueError, match="chain"):
            RunStartFacts(origin="manual", event_depth=depth, root_event_id=named)  # type: ignore[arg-type]
