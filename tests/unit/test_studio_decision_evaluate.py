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

"""Try an input asks the Studio host, which checks a local decision table as the platform would, without one."""

import json
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"
ROUTE = "/studio/local/decision/evaluate"


def client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE)), base_url=ORIGIN)


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


def ref(name: str) -> dict[str, Any]:
    return {"ref": f"/input/{name}"}


def op(name: str, *args: Any) -> dict[str, Any]:
    return {"op": {"name": name, "args": list(args)}}


def table(policy: str = "first", *, default: bool = True, version: str = "1.3.0") -> dict[str, Any]:
    spec: dict[str, Any] = {
        "inputSchema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "tier": {"type": "string"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"approve": {"type": "boolean"}, "limit": {"type": "number"}},
            "required": ["approve"],
        },
        "hitPolicy": policy,
        "rules": [
            {
                "id": "big-gold",
                "when": op(
                    "and", op("gt", ref("amount"), {"literal": 1000}), op("eq", ref("tier"), {"literal": "gold"})
                ),
                "output": {"literal": {"approve": True, "limit": 5000}},
            },
            {
                "id": "big",
                "when": op("gt", ref("amount"), {"literal": 1000}),
                "output": {"object": {"approve": {"literal": False}, "limit": ref("amount")}},
            },
        ],
    }
    if default:
        spec["defaultOutput"] = {"literal": {"approve": False, "limit": 0}}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "payment-policy", "version": version},
        "spec": spec,
    }


def test_a_local_table_answers_with_its_result_and_matched_rules(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        headers = pair(browser)
        answer = browser.post(
            ROUTE, json={"table": table(), "input": {"amount": 2000, "tier": "gold"}}, headers=headers
        )
        assert answer.status_code == 200
        assert answer.json() == {
            "output": {"approve": True, "limit": 5000},
            "matched_rule_ids": ["big-gold"],
            "used_default": False,
        }
        otherwise = browser.post(ROUTE, json={"table": table(), "input": {"amount": 10}}, headers=headers)
        assert otherwise.json() == {
            "output": {"approve": False, "limit": 0},
            "matched_rule_ids": [],
            "used_default": True,
        }


def test_refusals_name_the_reason_and_the_place_but_never_a_value(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        headers = pair(browser)
        cases = [
            (
                table("unique"),
                {"amount": 2000, "tier": "gold"},
                "WV-DECISION-MULTIPLE_MATCHES",
                "/spec/rules/1/when",
                "Two rules matched but the policy allows exactly one.",
            ),
            (
                table(default=False),
                {"amount": 10},
                "WV-DECISION-NO_MATCH",
                "/spec",
                "No rule matched and there's no Otherwise result.",
            ),
            (
                table(),
                {"amount": "secret-looking-value"},
                "WV-DECISION-INPUT",
                "/spec/inputSchema",
                "The input doesn't fit the table's input fields.",
            ),
            (
                table(),
                {"amount": 2000, "tier": "silver", "extra": "x"},
                "WV-DECISION-INPUT",
                "/spec/inputSchema",
                "The input doesn't fit the table's input fields.",
            ),
        ]
        for document, value, code, path, message in cases:
            answer = browser.post(ROUTE, json={"table": document, "input": value}, headers=headers)
            assert answer.status_code == 422, (code, answer.text)
            body = answer.json()
            assert (body["code"], body["result"], body["message"]) == (code, {"path": path}, message)
            assert "secret-looking-value" not in answer.text


def test_a_result_that_breaks_the_result_fields_is_refused(tmp_path: Path) -> None:
    document = table()
    document["spec"]["outputSchema"]["properties"]["limit"]["maximum"] = 6000
    with client(tmp_path) as browser:
        answer = browser.post(
            ROUTE, json={"table": document, "input": {"amount": 7000, "tier": "silver"}}, headers=pair(browser)
        )
        assert answer.status_code == 422
        assert answer.json()["code"] == "WV-DECISION-OUTPUT"
        assert answer.json()["message"] == "The result doesn't fit the table's result fields."


def test_a_table_that_does_not_compile_says_where(tmp_path: Path) -> None:
    document = table()
    document["spec"]["rules"][0]["when"] = op("gt", ref("tier"), {"literal": 1})
    document["spec"]["inputSchema"]["properties"]["tier"] = {"$ref": "urn:weave:schema:tier@1.0.0"}
    with client(tmp_path) as browser:
        answer = browser.post(ROUTE, json={"table": document, "input": {"amount": 1}}, headers=pair(browser))
        assert answer.status_code == 422
        body = answer.json()
        assert body["code"] == "WV-DECISION-COMPILE"
        assert body["message"] == "Weave can't check this table yet: fix the problems under the grid first."
        assert body["result"]["diagnostics"] and all(
            set(item) == {"code", "path"} for item in body["result"]["diagnostics"]
        )


def test_only_a_decision_table_and_an_input_are_accepted(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        headers = pair(browser)
        for body in (
            {"table": table()},
            {"table": table(), "input": {}, "extra": 1},
            {"table": {"kind": "Workflow"}, "input": {}},
        ):
            answer = browser.post(
                ROUTE, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
            )
            assert answer.status_code == 422
            assert answer.json()["code"] == "WV-STUDIO-REQUEST"


def test_try_an_input_needs_the_session_and_the_csrf_token(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        body = {"table": table(), "input": {"amount": 1}}
        assert browser.post(ROUTE, json=body, headers={"Origin": ORIGIN}).status_code in {401, 403}
        pair(browser)
        assert browser.post(ROUTE, json=body, headers={"Origin": ORIGIN}).status_code == 403


def test_an_oversized_body_is_refused_before_it_is_read(tmp_path: Path) -> None:
    with client(tmp_path) as browser:
        huge = {"table": table(), "input": {"tier": "x" * (2 * 1024 * 1024)}}
        assert browser.post(ROUTE, json=huge, headers=pair(browser)).status_code == 413


def test_it_shares_validation_s_slots_and_not_the_one_an_execution_holds(tmp_path: Path, monkeypatch: Any) -> None:
    from firefly_weave.studio import host

    with client(tmp_path) as browser:
        headers = pair(browser)
        body = {"table": table(), "input": {"amount": 1500, "tier": "gold"}}
        monkeypatch.setattr(host._DEBUG_WORK, "claim", lambda: False)
        assert browser.post(ROUTE, json=body, headers=headers).status_code == 200
        monkeypatch.setattr(host._LOCAL_WORK, "claim", lambda: False)
        busy = browser.post(ROUTE, json=body, headers=headers)
        assert busy.status_code == 503 and busy.json()["code"] == "WV-STUDIO-BUSY"


def test_an_input_a_rule_cant_evaluate_is_refused_in_words(tmp_path: Path) -> None:
    loose = table()
    loose["spec"]["inputSchema"] = {"type": "object", "properties": {"amount": {}, "tier": {}}}
    with client(tmp_path) as browser:
        answer = browser.post(ROUTE, json={"table": loose, "input": {"amount": "lots"}}, headers=pair(browser))
    assert answer.status_code == 422, answer.text
    body = answer.json()
    assert body["code"].startswith("WV-EXPR-")
    assert body["message"] == "A rule couldn't be evaluated with this input."
    assert body["result"]["path"].startswith("/")
    assert "lots" not in answer.text
