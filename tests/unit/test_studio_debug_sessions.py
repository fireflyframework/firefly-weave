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


"""Step through a simulated run in a session the Studio host keeps in memory: bounded, fenced by revision."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_studio_simulation_execute import ACTION, CHECK, SHAPE, workflow

from firefly_weave.operations.debug.models import DebugCommand
from firefly_weave.studio import debug_sessions
from firefly_weave.studio.debug_sessions import HostSessions
from firefly_weave.studio.local_contracts import SimulationRequest
from firefly_weave.studio.simulation import SimulationRefused

PIN = {"node:check": {"eligible": True}}


def body(source: str, **fields) -> dict:
    return {
        "source": source,
        "format": "json",
        "definitions": [ACTION],
        "catalog": "none",
        "input": {"id": "c-1"},
        "now": "2026-10-26T10:00:00Z",
        **fields,
    }


def request(source: str, **fields) -> SimulationRequest:
    return SimulationRequest.model_validate_json(json.dumps(body(source, **fields)))


def refusal(call) -> SimulationRefused:
    with pytest.raises(SimulationRefused) as refused:
        call()
    return refused.value


def test_a_session_steps_one_boundary_at_a_time_and_continues_to_a_breakpoint():
    sessions = HostSessions()
    session = sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=0)
    assert session.revision == 1
    stepped = sessions.command(session.id, 1, DebugCommand(kind="next"), now=1)
    assert stepped.revision == 2
    assert stepped.view.current_nodes != session.view.current_nodes
    marked = sessions.command(session.id, 2, DebugCommand(kind="breakpoints", node_ids=["shape"]), now=2)
    paused = sessions.command(session.id, marked.revision, DebugCommand(kind="continue"), now=3)
    assert paused.view.status == "paused"
    assert paused.view.selected_node == "shape" or "shape" in paused.view.current_nodes
    assert sessions.read(session.id, now=4).revision == paused.revision


def test_session_commands_need_the_current_revision():
    sessions = HostSessions()
    session = sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=0)
    sessions.command(session.id, 1, DebugCommand(kind="next"), now=1)
    stale = refusal(lambda: sessions.command(session.id, 1, DebugCommand(kind="next"), now=2))
    assert (stale.status, stale.code) == (409, "WV-DEBUG-REVISION")
    assert sessions.read(session.id, now=3).revision == 2


def test_at_most_four_sessions_and_idle_ones_expire():
    sessions = HostSessions()
    made = [sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=0) for _ in range(4)]
    full = refusal(lambda: sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=1))
    assert (full.status, full.code) == (429, "WV-STUDIO-SESSIONS-FULL")
    # Reading keeps a session alive; one left idle longer than 30 minutes expires and frees its place.
    sessions.read(made[1].id, now=1000)
    expired = refusal(lambda: sessions.read(made[0].id, now=1801))
    assert (expired.status, expired.code) == (410, "WV-DEBUG-EXPIRED")
    assert sessions.read(made[1].id, now=1801).id == made[1].id
    sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=1802)
    gone = refusal(lambda: sessions.read(made[0].id, now=1803))
    assert gone.status == 404


def test_a_session_larger_than_its_bound_is_refused(monkeypatch):
    monkeypatch.setattr(debug_sessions, "SESSION_BYTES", 2048)
    sessions = HostSessions()
    limit = refusal(lambda: sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=0))
    assert (limit.status, limit.code) == (422, "WV-DEBUG-LIMIT")
    assert sessions.count() == 0


def test_a_session_takes_the_same_secret_check_as_execute():
    schema = {"type": "object", "properties": {"pin": {"type": "string", "x-secret": True}}}
    secret = refusal(
        lambda: HostSessions().create(
            request(workflow(SHAPE | {"value": {"literal": 1}}, input_schema=schema), input={"pin": "4321"}),
            None,
            now=0,
        )
    )
    assert (secret.status, secret.code, secret.paths) == (422, "WV-SCHEMA-SECRET_VALUE", ["/input"])
    assert "4321" not in repr(secret.__dict__)


def test_a_workflow_that_does_not_compile_gives_no_session():
    sessions = HostSessions()
    broken = refusal(
        lambda: sessions.create(
            request(workflow({"id": "broken", "kind": "transform", "value": {"ref": "/steps/nowhere/output"}})),
            None,
            now=0,
        )
    )
    assert (broken.status, broken.code) == (422, "WV-STUDIO-REQUEST")
    assert sessions.count() == 0


ORIGIN = "http://127.0.0.1:8766"
CODE = "terminal-only-pairing-code"


def client(tmp_path: Path, **options) -> TestClient:
    from firefly_weave.studio.host import make_studio_app
    from firefly_weave.studio.service import StudioOptions

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Weave Studio</title>")
    return TestClient(
        make_studio_app(StudioOptions(origin=ORIGIN, assets=assets, pairing_code=CODE, **options)), base_url=ORIGIN
    )


def pair(browser: TestClient) -> dict[str, str]:
    result = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    return {"Origin": ORIGIN, "X-Weave-CSRF": result.json()["csrfToken"]}


SESSIONS = "/studio/local/debug/sessions"


def test_the_host_creates_reads_commands_and_deletes_sessions(tmp_path):
    with client(tmp_path) as browser:
        headers = pair(browser)
        created = browser.post(SESSIONS, json=body(workflow(CHECK, SHAPE), mocks=PIN), headers=headers)
        assert created.status_code == 201, created.text
        identifier, revision = created.json()["id"], created.json()["revision"]
        assert browser.get(f"{SESSIONS}/{identifier}", headers=headers).json()["revision"] == revision
        stepped = browser.post(
            f"{SESSIONS}/{identifier}/commands",
            json={"kind": "next"},
            headers=headers | {"If-Match": f'"{revision}"'},
        )
        assert stepped.status_code == 200 and stepped.json()["revision"] == revision + 1
        stale = browser.post(
            f"{SESSIONS}/{identifier}/commands",
            json={"kind": "next"},
            headers=headers | {"If-Match": f'"{revision}"'},
        )
        assert stale.status_code == 409 and stale.json()["code"] == "WV-DEBUG-REVISION"
        assert browser.delete(f"{SESSIONS}/{identifier}", headers=headers).status_code == 204
        assert browser.get(f"{SESSIONS}/{identifier}", headers=headers).status_code == 404


def test_signing_out_clears_every_session(tmp_path):
    # The desktop shell pairs again after an unpairing, so the same host can be asked again.
    with client(tmp_path, reusable_pairing=True) as browser:
        headers = pair(browser)
        identifier = browser.post(SESSIONS, json=body(workflow(CHECK, SHAPE), mocks=PIN), headers=headers).json()["id"]
        assert browser.delete("/studio/session", headers=headers).status_code == 204
        headers = pair(browser)
        missing = browser.get(f"{SESSIONS}/{identifier}", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "WV-STUDIO-DEBUG-NOT-FOUND"


def test_session_routes_keep_the_boundary(tmp_path):
    with client(tmp_path) as browser:
        anonymous = {"Origin": ORIGIN}
        assert browser.post(SESSIONS, json=body(workflow(SHAPE)), headers=anonymous).status_code == 401
        headers = pair(browser)
        refused = browser.post(SESSIONS, json=body(workflow(CHECK, SHAPE), mocks=PIN), headers=anonymous)
        assert refused.status_code == 403
        identifier = browser.post(SESSIONS, json=body(workflow(CHECK, SHAPE), mocks=PIN), headers=headers).json()["id"]
        unconditional = browser.post(f"{SESSIONS}/{identifier}/commands", json={"kind": "next"}, headers=headers)
        assert unconditional.status_code == 428 and unconditional.json()["code"] == "WV-STUDIO-REQUEST"
        extra = browser.post(SESSIONS, json=body(workflow(SHAPE)) | {"target": {"mode": "workflow"}}, headers=headers)
        assert extra.status_code == 422


def test_idle_sessions_expire_whenever_the_host_is_asked():
    sessions = HostSessions()
    first = sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=0)
    second = sessions.create(request(workflow(CHECK, SHAPE), mocks=PIN), None, now=1000)
    sessions.read(second.id, now=2000)
    # The first was idle more than 30 minutes: reading another one frees it.
    assert sessions.count() == 1
    assert refusal(lambda: sessions.read(first.id, now=2001)).status == 404


SECRET_SIGNAL = {
    "id": "paid",
    "kind": "signal",
    "name": "order-paid",
    "timeoutSeconds": 600,
    "payloadSchema": {"type": "object", "properties": {"code": {"type": "string", "x-secret": True}}},
}
SECRET_TASK = {
    "id": "approval",
    "kind": "humanTask",
    "assignment": "reviewers",
    "title": {"literal": "Approve"},
    "context": {"literal": {}},
    "formSchema": {"type": "object", "properties": {"pin": {"type": "string", "x-secret": True}}},
    "decisions": ["approve", "reject"],
}


def test_a_secret_signal_payload_is_refused_with_its_pointer_and_the_session_stays():
    sessions = HostSessions()
    session = sessions.create(request(workflow(SECRET_SIGNAL)), None, now=0)
    running = sessions.command(session.id, 1, DebugCommand(kind="continue"), now=1)
    secret = refusal(
        lambda: sessions.command(
            session.id,
            running.revision,
            DebugCommand(kind="signal", name="order-paid", payload={"code": "7731"}),
            now=2,
        )
    )
    assert (secret.status, secret.code, secret.paths) == (422, "WV-SCHEMA-SECRET_VALUE", ["/payload"])
    assert "7731" not in repr(secret.__dict__)
    assert sessions.read(session.id, now=3).revision == running.revision


def test_a_secret_in_a_person_s_answer_is_refused_with_its_pointer():
    sessions = HostSessions()
    session = sessions.create(request(workflow(SECRET_TASK)), None, now=0)
    running = sessions.command(session.id, 1, DebugCommand(kind="continue"), now=1)
    secret = refusal(
        lambda: sessions.command(
            session.id,
            running.revision,
            DebugCommand(kind="human_decision", name="approval", payload={"decision": "approve", "data": {"pin": "9"}}),
            now=2,
        )
    )
    assert (secret.status, secret.code, secret.paths) == (422, "WV-SCHEMA-SECRET_VALUE", ["/payload/data"])


PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 300}


def test_continue_lets_waits_pass_and_uses_test_answers_up_to_a_breakpoint():
    paid = {"id": "paid", "kind": "signal", "name": "order-paid", "timeoutSeconds": 600, "payloadSchema": {}}
    sessions = HostSessions()
    session = sessions.create(
        request(
            workflow(PAUSE, paid, SHAPE | {"value": {"literal": 1}}),
            scripts={"signals": {"paid": {"payload": {}, "source": "manual"}}},
        ),
        None,
        now=0,
    )
    marked = sessions.command(session.id, 1, DebugCommand(kind="breakpoints", node_ids=["shape"]), now=1)
    paused = sessions.command(session.id, marked.revision, DebugCommand(kind="continue"), now=2)
    assert paused.view.status == "paused"
    assert "shape" in paused.view.current_nodes
