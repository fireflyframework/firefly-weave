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

"""Test data entries are checked against their own schemas, and secrets are never kept."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.test_data import check_test_data
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.studio_documents import StudioTestData
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


ACTION = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))
SECRET_ACTION: dict[str, Any] = {
    **ACTION,
    "metadata": {"name": "onboarding.issue-token", "version": "1.0.0"},
    "spec": {
        **ACTION["spec"],
        "outputSchema": {
            "type": "object",
            "properties": {"token": {"type": "string", "x-secret": True}, "id": {"type": "string"}},
        },
    },
}
WORKFLOW: dict[str, Any] = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "orders", "version": "1.0.0"},
    "spec": {
        "inputSchema": {
            "type": "object",
            "properties": {"customerId": {"type": "string"}, "password": {"type": "string", "x-secret": True}},
        },
        "outputSchema": {},
        "steps": [
            {"id": "check", "kind": "action", "uses": "onboarding.check-customer@1.0.0", "with": {"literal": {}}},
            {"id": "token", "kind": "action", "uses": "onboarding.issue-token@1.0.0", "with": {"literal": {}}},
            {"id": "unknown", "kind": "action", "uses": "crm.lookup@1.0.0", "with": {"literal": {}}},
            {
                "id": "route",
                "kind": "switch",
                "cases": [
                    {
                        "when": {"literal": True},
                        "steps": [
                            {
                                "id": "paid",
                                "kind": "signal",
                                "name": "paid",
                                "timeoutSeconds": 60,
                                "payloadSchema": {
                                    "type": "object",
                                    "properties": {"pin": {"type": "string", "writeOnly": True}},
                                },
                            },
                            {
                                "id": "review",
                                "kind": "humanTask",
                                "assignment": "reviewers",
                                "title": {"literal": "Review"},
                                "context": {"literal": {}},
                                "formSchema": {"type": "object", "properties": {"note": {"type": "string"}}},
                                "decisions": ["approve", "reject"],
                            },
                        ],
                        "output": {"literal": {}},
                    }
                ],
                "default": {"steps": [], "output": {"literal": {}}},
            },
            {"id": "shape", "kind": "transform", "value": {"literal": 1}},
        ],
        "output": {"literal": None},
    },
}
PIN = {
    "source": "manual",
    "pinnedAt": "2026-10-20T10:00:00Z",
    "configHash": "fnv1a64:0000000000000001",
    "contractHash": "fnv1a64:0000000000000002",
    "upstreamHash": "fnv1a64:0000000000000003",
}


def data(**fields: Any) -> StudioTestData:
    document = {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-10-20T10:00:00Z", **fields}
    return StudioTestData.model_validate_json(json.dumps(document), by_alias=True, by_name=False)


def check(document: StudioTestData, *definitions: dict[str, Any]) -> dict[str, str | None]:
    catalog = CatalogSnapshot.from_definitions([load_definition(d) for d in definitions])
    return dict(check_test_data(WORKFLOW, document, catalog))


def test_each_entry_is_checked_against_its_own_schema():
    result = check(
        data(
            input={"value": {"customerId": "c-1"}, "source": "manual"},
            pins={
                "check": {**PIN, "output": {"eligible": True}},
                "check[2]": {**PIN, "output": {"eligible": "yes"}},
                "token": {**PIN, "output": {"id": "t-1"}},
            },
            signals={"paid": {"payload": {}}},
            humans={"review": {"decision": "approve", "data": {"note": "ok"}}},
        ),
        ACTION,
        SECRET_ACTION,
    )
    assert result == {
        "/input": None,
        "/pins/check": None,
        "/pins/check[2]": "WV-STUDIO-TESTDATA-SCHEMA",
        "/pins/token": None,
        "/signals/paid": None,
        "/humans/review": None,
    }


def test_a_present_secret_value_is_refused_wherever_it_sits():
    result = check(
        data(
            input={"value": {"customerId": "c-1", "password": "s3cret"}, "source": "manual"},
            pins={"token": {**PIN, "output": {"id": "t-1", "token": "abc"}}},
            signals={"paid": {"payload": {"pin": "1234"}}},
        ),
        ACTION,
        SECRET_ACTION,
    )
    assert set(result.values()) == {"WV-SCHEMA-SECRET_VALUE"}


def test_entries_without_a_known_schema_are_unresolved_never_accepted():
    result = check(
        data(
            pins={
                "unknown": {**PIN, "output": {}},
                "gone": {**PIN, "output": {}},
                "shape": {**PIN, "output": 1},
            },
            humans={"review": {"decision": "escalate", "data": {}}},
            aiTurns={"support": {"turns": []}},
            workflows={"notify@1.0.0": {"mocks": {"send": {"output": {}, "source": "manual"}}}},
        ),
        ACTION,
    )
    assert result == {
        "/pins/unknown": "WV-STUDIO-TESTDATA-UNRESOLVED",
        "/pins/gone": "WV-STUDIO-TESTDATA-UNRESOLVED",
        # A transform's output comes from its input: nothing to pin.
        "/pins/shape": "WV-STUDIO-TESTDATA-SCHEMA",
        "/humans/review": "WV-STUDIO-TESTDATA-SCHEMA",
        "/aiTurns/support": "WV-STUDIO-TESTDATA-UNRESOLVED",
        "/workflows/notify@1.0.0/mocks/send": "WV-STUDIO-TESTDATA-UNRESOLVED",
    }


def test_timeouts_and_expiries_carry_no_value_to_check():
    result = check(data(signals={"paid": {"outcome": "timeout"}}, humans={"review": {"outcome": "expire"}}))
    assert result == {"/signals/paid": None, "/humans/review": None}


def body(document: StudioTestData, *definitions: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": json.dumps(WORKFLOW),
        "format": "json",
        "definitions": list(definitions),
        "catalog": "none",
        "testData": document.model_dump(mode="json", by_alias=True),
    }


def test_the_host_route_needs_a_paired_session_and_answers_every_entry(tmp_path):
    document = data(
        input={"value": {"password": "s3cret"}, "source": "manual"},
        pins={"check": {**PIN, "output": {"eligible": True}}},
    )
    with client(tmp_path) as browser:
        origin = {"Origin": ORIGIN}
        unpaired = browser.post("/studio/local/testdata/check", json=body(document, ACTION), headers=origin)
        assert unpaired.status_code == 401
        headers = pair(browser)
        without_csrf = browser.post("/studio/local/testdata/check", json=body(document, ACTION), headers=origin)
        assert without_csrf.status_code == 403
        answer = browser.post("/studio/local/testdata/check", json=body(document, ACTION), headers=headers)
        assert answer.status_code == 200
        assert answer.json() == {
            "entries": [
                {"path": "/input", "ok": False, "code": "WV-SCHEMA-SECRET_VALUE"},
                {"path": "/pins/check", "ok": True},
            ]
        }
        # Values never come back: the answer names pointers and codes only.
        assert "s3cret" not in answer.text
        malformed = browser.post(
            "/studio/local/testdata/check", json={**body(document), "testData": {"kind": "x"}}, headers=headers
        )
        assert malformed.status_code == 422


@pytest.mark.parametrize("source", ["{not json", json.dumps([1, 2])])
def test_a_source_that_does_not_read_leaves_every_entry_unresolved(tmp_path, source):
    document = data(pins={"check": {**PIN, "output": {"eligible": True}}})
    with client(tmp_path) as browser:
        headers = pair(browser)
        answer = browser.post(
            "/studio/local/testdata/check", json={**body(document, ACTION), "source": source}, headers=headers
        )
    assert answer.json() == {"entries": [{"path": "/pins/check", "ok": False, "code": "WV-STUDIO-TESTDATA-UNRESOLVED"}]}


def test_an_undo_phase_entry_is_unresolved_never_checked_against_the_step_it_undoes():
    # `check#confirm` is the confirm phase of step check, which runs another action: its output schema isn't the
    # step's, so a value that fits the step (or a secret only the phase's schema marks) is never accepted for it.
    result = check(
        data(
            pins={
                "check#2": {**PIN, "output": {"eligible": True}},
                "check#confirm": {**PIN, "output": {"eligible": True}},
                "check[2]#cancel": {**PIN, "output": {"eligible": True}},
                "token#undo": {**PIN, "output": {"id": "t-1"}},
            },
        ),
        ACTION,
        SECRET_ACTION,
    )
    assert result == {
        "/pins/check#2": None,
        "/pins/check#confirm": "WV-STUDIO-TESTDATA-UNRESOLVED",
        "/pins/check[2]#cancel": "WV-STUDIO-TESTDATA-UNRESOLVED",
        "/pins/token#undo": "WV-STUDIO-TESTDATA-UNRESOLVED",
    }
