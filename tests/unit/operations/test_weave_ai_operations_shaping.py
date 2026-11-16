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

"""Explanations get the room an absent draft would use, send positions instead of record IDs, and trim by kind."""

import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from firefly_weave.contracts.operations_summary import AttentionItem
from firefly_weave.operations.lumi_context import (
    EXPLAIN_ONLY,
    estimate_tokens,
    input_budget,
    output_tokens,
    shape,
)
from firefly_weave.operations.lumi_deployments import LIMITATION
from firefly_weave.operations.lumi_operations import (
    LIMITATIONS,
    NOT_IN_RECORDS,
    SHORTENERS,
    change_request_context,
    environment_context,
    explanation_for,
    incident_context,
    worker_context,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_weave_ai_operations import (  # noqa: E402
    ITEM_IDS,
    NOW,
    change_request,
    incident_facts,
    incident_view,
    job,
    plan,
    summary_facts,
    summary_of,
    worker,
)

COMPACT = input_budget("compact", 8192, output_tokens("compact", 512))
IDS = [UUID(f"00000000-0000-4000-8000-0000000002{index:02d}") for index in range(4)]


def attachment(kind, data, index=0):
    return {"kind": kind, "id": str(IDS[index]), "data": data}


def shaped(resources, kinds=None, *, budget=COMPACT, profile="compact"):
    kinds = kinds or [resource["kind"] for resource in resources]
    return shape(
        profile,
        budget,
        message="Explain",
        history=[],
        resources=resources,
        draft=None,
        selected_step=None,
        schemas={"workflow": {}},
        explanation=explanation_for(kinds),
    )


def largest_worker():
    return worker_context(
        worker().model_copy(update={"task_types": [f"weave-agentic.generate-{index:02d}@1.0.0" for index in range(20)]})
    ).data


def largest_incident():
    view = incident_view(node_id="support", instance_key="support[12]#3.14~2", incident_key="support:1")
    events = tuple(["task_failed", "recovery_scheduled"] * 10)
    return incident_context(incident_facts(view, events=events, more_events=True, other_events=3)).data


def largest_environment():
    base = summary_of()
    names = [
        "database",
        "recovery_loop",
        "schedules",
        "outbox_loop",
        "provider_loop",
        "kafka_loop",
        "operations_loop",
        "alert_evaluator",
        "native_executors",
        "telemetry",
    ]
    health = [base.health[0].model_copy(update={"component": names[index % 10]}) for index in range(16)]
    items = [
        AttentionItem(
            kind="approval",
            id=ITEM_IDS[index],
            title="Approve the plan to restart workers on production-cluster-eu-west-01",
            expires_at=NOW + timedelta(seconds=900),
        )
        for index in range(20)
    ]
    return environment_context(summary_facts(base.model_copy(update={"health": health, "attention": items}))).data


def largest_change_request():
    names = [f"fixture-worker-{index:02d}" for index in range(20)]
    desired = [{"name": name, "replicas": 12, "cpu_millis": 1500, "memory_mib": 2048} for name in names]
    history = [
        {"at": (NOW - timedelta(seconds=86400 - index)).isoformat(), "state": "reconciliation_required"}
        for index in range(20)
    ]
    record = change_request(components=names, desired=desired, history=history)
    return change_request_context(record, plan(), job(), observed_at=NOW).data


def sized(wrapper, tokens):
    """Filler data whose attachment, wrapped as ``wrapper`` and listed as shape() measures the attachments, estimates
    at exactly ``tokens``."""
    words = 0
    while True:
        data = {"filler": " ".join(["a"] * words)}
        estimate = estimate_tokens(json.dumps([{**wrapper, "data": data}]))
        if estimate >= tokens:
            assert estimate == tokens
            return data
        words += 1


def test_an_explanation_gives_attachments_the_drafts_share():
    assert COMPACT == 4096
    sent = {"kind": "deployment", "ref": 1}
    fits = shaped([attachment("deployment", sized(sent, 2457))]).context
    assert len(fits["resources"]) == 1 and "trimmed" not in fits
    over = shaped([attachment("deployment", sized(sent, 2458))]).context
    assert over["resources"] == [] and over["trimmed"] == ["attachments"]


def test_an_authoring_request_keeps_its_attachment_room():
    wrapper = {"kind": "run", "id": str(IDS[0])}
    # The compact guide takes 512 tokens of the budget, so an authoring request keeps 764 for its attachments.
    for tokens, kept in ((764, True), (765, False)):
        resource = {**wrapper, "data": sized(wrapper, tokens)}
        authoring = shape(
            "compact",
            COMPACT,
            message="Help",
            history=[],
            resources=[resource],
            draft=None,
            selected_step=None,
            schemas={"workflow": {}},
        )
        assert authoring.context["resources"] == ([resource] if kept else [])


def test_an_authoring_request_drops_what_an_explanation_keeps():
    data = {"filler": " ".join(["word"] * 400)}
    resource = {"kind": "run", "id": str(IDS[0]), "data": data}
    assert 819 < estimate_tokens(json.dumps(resource)) <= 2457
    authoring = shape(
        "compact",
        COMPACT,
        message="Help",
        history=[],
        resources=[resource],
        draft=None,
        selected_step=None,
        schemas={"workflow": {}},
    )
    assert authoring.context["resources"] == []


def test_an_explanation_sends_positions_not_record_ids():
    resources = [
        attachment("worker", worker_context(worker()).data, 0),
        attachment("incident", incident_context(incident_facts()).data, 1),
        attachment("environment", environment_context(summary_facts()).data, 2),
        attachment("deployment-target", {"name": "local-docker"}, 3),
    ]
    sent = shaped(resources).context["resources"]
    assert [item["ref"] for item in sent] == [1, 2, 3, 4]
    assert all(set(item) == {"kind", "ref", "data"} for item in sent)
    text = json.dumps(sent)
    for identifier in IDS:
        assert str(identifier) not in text and identifier.hex not in text


def test_an_authoring_request_keeps_its_ids():
    resource = {"kind": "run", "id": str(IDS[0]), "data": {"status": "failed"}}
    authoring = shape(
        "compact",
        COMPACT,
        message="Help",
        history=[],
        resources=[resource],
        draft=None,
        selected_step=None,
        schemas={"workflow": {}},
    )
    assert authoring.context["resources"] == [resource]


def test_a_worker_an_incident_and_the_environment_fit_one_compact_request_untrimmed():
    resources = [
        attachment("worker", worker_context(worker()).data, 0),
        attachment("incident", incident_context(incident_facts()).data, 1),
        attachment("environment", environment_context(summary_facts()).data, 2),
    ]
    context = shaped(resources).context
    assert [item["data"] for item in context["resources"]] == [item["data"] for item in resources]
    assert "trimmed" not in context


def test_each_kinds_largest_fixture_stays_under_its_size_target():
    for kind, data, target in (
        ("worker", largest_worker(), 950),
        ("incident", largest_incident(), 700),
        ("environment", largest_environment(), 3000),
        ("change-request", largest_change_request(), 2600),
    ):
        tokens = estimate_tokens(json.dumps({"kind": kind, "ref": 1, "data": data}))
        assert tokens <= target, (kind, tokens)


def test_limitations_name_each_kind_present_once_in_order():
    context = shaped(
        [
            attachment("environment", environment_context(summary_facts()).data, 0),
            attachment("deployment-target", {"name": "a"}, 1),
            attachment("worker", worker_context(worker()).data, 2),
            attachment("environment", environment_context(summary_facts()).data, 3),
        ]
    ).context
    assert context["limitations"] == " ".join(
        (LIMITATION, LIMITATIONS["worker"], LIMITATIONS["environment"], NOT_IN_RECORDS, EXPLAIN_ONLY)
    )
    assert context["purpose"] == "explain-operations"
    assert "guide" not in context and "authoringSchemas" not in context


@pytest.mark.parametrize("kind", ["worker", "incident", "environment", "change-request"])
def test_an_operations_explanation_says_what_to_cite_and_to_say_what_is_missing(kind):
    built = {
        "worker": lambda: worker_context(worker()).data,
        "incident": lambda: incident_context(incident_facts()).data,
        "environment": lambda: environment_context(summary_facts()).data,
        "change-request": lambda: change_request_context(change_request(), None, None, observed_at=NOW).data,
    }[kind]()
    limitations = shaped([attachment(kind, built)]).context["limitations"]
    # Each kind's sentence asks for the facts an answer should name; the records never carry logs or free text.
    assert re.search(r"\b(Name|Give)\b", LIMITATIONS[kind])
    assert limitations == " ".join((LIMITATIONS[kind], NOT_IN_RECORDS, EXPLAIN_ONLY))


def test_a_deployment_only_explanations_limitations_string_is_unchanged():
    context = shaped([attachment("deployment", {"name": "a"})]).context
    assert context["limitations"] == (
        "Saved snapshots only, not live cloud state or worker task capacity. Explain only; return no proposals."
    )


def test_a_deployment_only_explanation_now_gets_the_explanation_room():
    data = {"filler": " ".join(["word"] * 1000)}
    assert 819 < estimate_tokens(json.dumps({"kind": "deployment", "ref": 1, "data": data})) <= 2457
    assert shaped([attachment("deployment", data)]).context["resources"] == [
        {"kind": "deployment", "ref": 1, "data": data}
    ]


def test_an_oversized_incident_drops_its_oldest_events_first_and_says_so():
    events = ["started", "task_failed", "recovery_scheduled", "incident_opened"] * 5
    data = {**largest_incident(), "recent_events": events, "more_recent_events": False}
    shortened = SHORTENERS["incident"](data, estimate_tokens(json.dumps(data)) - 20, trimmed := [])
    kept = shortened["recent_events"]
    assert 0 < len(kept) < 20 and kept == events[len(events) - len(kept) :]
    assert shortened["more_recent_events"] is True
    assert estimate_tokens(json.dumps(shortened)) <= estimate_tokens(json.dumps(data)) - 20
    assert trimmed == ["incident_events"]


def test_a_withheld_incident_has_no_events_to_drop():
    data = incident_context(incident_facts(step_kind="withheld", events="withheld", run_read=False)).data
    assert SHORTENERS["incident"](data, 10, trimmed := []) == data and trimmed == []


def test_an_oversized_environment_drops_attention_items_before_health():
    data = largest_environment()
    shortened = SHORTENERS["environment"](data, estimate_tokens(json.dumps(data)) - 40, trimmed := [])
    assert 0 < len(shortened["attention"]) < 20
    assert shortened["more_attention"] == 20 - len(shortened["attention"])
    assert shortened["attention"] == data["attention"][: len(shortened["attention"])]
    assert shortened["health"] == data["health"]
    assert trimmed == ["attention"]


def test_the_environment_counts_ok_health_components_after_attention():
    data = environment_context(summary_facts()).data
    ok = {"component": "database", "state": "ok", "last_ok_seconds": 1, "consecutive_failures": 0}
    down = {"component": "outbox_loop", "state": "down", "last_ok_seconds": 300, "consecutive_failures": 5}
    data["health"] = [ok, down] * 8
    shortened = SHORTENERS["environment"](data, estimate_tokens(json.dumps(data)) - 300, trimmed := [])
    assert shortened["attention"] == []
    assert shortened["health"].count(down) == 8
    assert shortened["health"].count(ok) < 8
    assert shortened["more_health"] == 8 - shortened["health"].count(ok)
    assert trimmed == ["attention", "health"]


def test_a_change_request_drops_old_history_then_components():
    data = largest_change_request()
    shortened = SHORTENERS["change-request"](data, estimate_tokens(json.dumps(data)) - 60, trimmed := [])
    assert len(shortened["history"]) < 20 and shortened["more_history"] > 0
    assert shortened["history"] == data["history"][len(data["history"]) - len(shortened["history"]) :]
    assert len(shortened["components"]) == 20
    tiny = SHORTENERS["change-request"](data, 150, trimmed := [])
    assert tiny["history"] == [] and len(tiny["components"]) < 20 and tiny["more_components"] > 0
    assert trimmed == ["change_history", "components"]


def test_a_worker_drops_task_types_from_the_end():
    data = largest_worker()
    shortened = SHORTENERS["worker"](data, estimate_tokens(json.dumps(data)) - 40, trimmed := [])
    assert shortened["task_types"] == data["task_types"][: len(shortened["task_types"])]
    assert shortened["more_task_types"] == 20 - len(shortened["task_types"])
    assert trimmed == ["task_types"]


def test_a_shortener_returns_data_that_fits_unchanged():
    data = largest_change_request()
    assert SHORTENERS["change-request"](data, None, trimmed := []) == data and trimmed == []


def test_shaping_trims_an_oversized_kind_by_its_own_rule():
    data = largest_environment()
    context = shaped([attachment("environment", data)]).context
    [sent] = context["resources"]
    assert sent["data"]["more_attention"] > 0
    assert "attention" in context["trimmed"]


def test_the_same_inputs_shape_the_same_request():
    resources = [
        attachment("worker", worker_context(worker()).data, 0),
        attachment("environment", largest_environment(), 1),
    ]
    assert shaped(resources).context == shaped(resources).context
