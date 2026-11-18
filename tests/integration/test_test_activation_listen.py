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

"""Listening for a test event: a signed webhook that starts test runs of the draft until its activation expires."""

from urllib.parse import urlsplit
from uuid import UUID

import pytest
from test_test_activation_create import CONTEXT, NOTIFY, drafts, refused  # noqa: F401
from test_webhook_retarget import signed
from transaction_platform import workflow

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.step_tests import DraftTestListenRequest

pytestmark = pytest.mark.integration
BASE = "https://weave.example.com"
EVENT = b'{"eventId":"event-1","payload":{"moveId":"M-1","quantity":2}}'


async def listen(env, activation, actor=None, secret="webhook"):
    return await env.service.listen(
        actor or env.tester, env.scope, activation.id, DraftTestListenRequest(secret_ref=secret), BASE, context=CONTEXT
    )


def route_of(listened) -> UUID:
    return UUID(urlsplit(listened.test_url).path.rsplit("/", 1)[1])


@pytest.fixture
async def listening(drafts):  # noqa: F811
    from firefly_weave.triggers.service import WebhookService

    created = await drafts.create(drafts.tester, workflow("notify-moves", [NOTIFY]))
    listened = await listen(drafts, created)
    return drafts, created, listened, drafts.graph.resolve(WebhookService)


async def test_the_test_url_names_a_route_on_the_test_activation_and_no_secret(listening):
    env, created, listened, _ = listening
    parts = urlsplit(listened.test_url)
    assert (parts.scheme, parts.netloc, parts.query, parts.fragment) == ("https", "weave.example.com", "", "")
    assert parts.path == f"/webhooks/{route_of(listened)}"
    assert listened.expires_at == created.expires_at
    [route] = await env.query(
        "SELECT activation_id,principal_id,disabled FROM trigger_routes WHERE id=:id", id=route_of(listened)
    )
    assert (route["activation_id"], route["principal_id"], route["disabled"]) == (created.id, env.tester.id, False)
    [record] = await env.query("SELECT target,event FROM access_audit WHERE action='test_activation.listen'")
    assert record["event"]["details"] == {"trigger_id": str(route_of(listened))}
    assert (await listen(env, created)).test_url == listened.test_url


async def test_a_signed_event_starts_a_test_run_of_the_draft(listening):
    env, created, listened, webhooks = listening
    receipt = await webhooks.receive(route_of(listened), EVENT, signed(EVENT))
    [run] = await env.query("SELECT activation_id,principal_id FROM runs WHERE id=:run", run=receipt.run_id)
    assert (run["activation_id"], run["principal_id"]) == (created.id, env.tester.id)
    [facts] = await env.query("SELECT test,origin FROM run_facts WHERE run_id=:run", run=receipt.run_id)
    assert (facts["test"], facts["origin"]) == (True, "test")
    # The same event again answers the same receipt, as any webhook does.
    assert await webhooks.receive(route_of(listened), EVENT, signed(EVENT)) == receipt


async def test_unsigned_stale_or_oversized_events_are_refused_as_any_webhooks_are(listening):
    env, _, listened, webhooks = listening
    for headers in ({}, signed(b'{"eventId":"event-1","payload":1}'), {**signed(EVENT), "x-weave-timestamp": "1"}):
        error = await refused(webhooks.receive(route_of(listened), EVENT, headers))
        assert (error.status, error.code) == (401, "WV-WEBHOOK-AUTH")
    error = await refused(webhooks.receive(route_of(listened), b" " * 1048577, signed(EVENT)))
    assert error.status == 413
    assert await env.query("SELECT id FROM runs") == []


async def test_after_the_activation_expires_a_signed_event_is_refused_before_the_sweep(listening):
    env, created, listened, webhooks = listening
    await env.execute(
        "UPDATE test_activation_details SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 second' "
        "WHERE activation_id=:a",
        a=created.id,
    )
    error = await refused(webhooks.receive(route_of(listened), EVENT, signed(EVENT)))
    assert (error.status, error.code) == (410, "WV-TESTACTIVATION-EXPIRED")
    assert await env.query("SELECT id FROM runs") == []
    error = await refused(listen(env, created))
    assert error.code == "WV-TESTACTIVATION-EXPIRED"


async def test_listening_needs_trigger_manage_and_a_granted_secret(drafts):  # noqa: F811
    env = drafts
    tester = await env.person("step_tester", "developer", "viewer")
    created = await env.create(tester, workflow("notify-moves", [NOTIFY]))
    with pytest.raises(AccessDenied):
        await listen(env, created, actor=tester)
    created = await env.create(env.tester, workflow("notify-moves", [NOTIFY]))
    error = await refused(listen(env, created, secret="not-granted"))
    assert (error.status, error.code) == (422, "WV-CONNECTION")
    assert await env.query("SELECT id FROM trigger_routes") == []


async def test_the_trigger_operations_never_show_stop_or_move_a_listen_route(listening):
    env, created, listened, webhooks = listening
    route = route_of(listened)
    manager = await env.person("deployer", "operator", "viewer")
    assert all(item["id"] != str(route) for item in (await webhooks.list(manager, env.scope, context=CONTEXT))["items"])
    for call in (
        webhooks.read(manager, env.scope, route, context=CONTEXT),
        webhooks.retarget(manager, env.scope, route, env.platform.release, 1, context=CONTEXT),
    ):
        error = await refused(call)
        assert (error.status, error.code) == (404, "WV-NOT-FOUND")
    await webhooks.disable(manager, env.scope, route, context=CONTEXT)
    [row] = await env.query("SELECT disabled FROM trigger_routes WHERE id=:id", id=route)
    assert row["disabled"] is False
