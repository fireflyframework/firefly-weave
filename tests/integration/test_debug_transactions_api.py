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

"""Debug sessions with transactions on real PostgreSQL: a person resolves one, and nothing of the project moves."""

import json
import sys
from pathlib import Path

import pytest
import test_definitions as catalog_tests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit" / "runtime"))

from transaction_support import INPUT, add, compiled, hold, submit, transaction  # noqa: E402

pytestmark = pytest.mark.integration
author = catalog_tests.author

UNDO_STUCK = {"node:add": {"code": "UNAVAILABLE"}, "node:add#undo": {"code": "UNAVAILABLE", "attempts": 10}}
RETRY = {"kind": "resolve", "incident_key": "add#undo:10", "resolution": "retry_safe"}


async def in_doubt(author, headers, project_url):
    """A session whose transaction is in doubt, at revision 2: its URL and that view."""
    response = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json={
            "artifact": json.loads(compiled([transaction([hold(), add(), submit()])]).to_bytes()),
            "mocks": {"node:hold": {"holdId": "h-1"}, "node:submit": {"orderId": "o-1"}},
            "errors": UNDO_STUCK,
            "input": INPUT,
            "now": "2026-11-12T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    url = project_url + "/debug/sessions/" + response.json()["id"]
    stepped = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "continue"})
    assert stepped.status_code == 200, stepped.text
    return url, stepped.json()


async def test_a_session_runs_a_transaction_and_a_person_resolves_it(author, headers, project_url):
    url, stepped = await in_doubt(author, headers, project_url)
    assert stepped["revision"] == 2
    [doubt] = stepped["view"]["transactions"]
    assert (doubt["state"], doubt["incident_key"]) == ("in_doubt", "add#undo:10")
    resolved = await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json=RETRY)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["revision"] == 3
    ended = await author[0].post(url + "/commands", headers={**headers, "If-Match": "3"}, json={"kind": "continue"})
    assert ended.status_code == 200, ended.text
    assert ended.json()["revision"] == 4
    assert ended.json()["view"]["transactions"][0]["state"] == "compensated"
    read = await author[0].get(url, headers=headers)
    assert read.json() == ended.json()


async def test_a_resolve_in_a_session_changes_no_run_or_incident_of_the_project(
    author, headers, project_url, access_db
):
    from sqlalchemy import text

    # Runs, incidents, transactions, and the platform's transactional outbox (integration_events).
    rows = text(
        "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM incidents),"
        " (SELECT count(*) FROM run_transactions), (SELECT count(*) FROM integration_events)"
    )

    async def counts():
        async with access_db[1]() as session:
            return tuple((await session.execute(rows)).one())

    before = await counts()
    url, _ = await in_doubt(author, headers, project_url)
    resolved = await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json=RETRY)
    assert resolved.status_code == 200, resolved.text
    assert await counts() == before


async def test_another_person_cant_resolve_a_session(
    author, headers, project_url, access_db, provisioned, other_headers
):
    from sqlalchemy import text

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.access import Scope

    url, _ = await in_doubt(author, headers, project_url)
    async with access_db[1]() as session:
        other_id = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='1'"))
    scope = Scope(tenant_id=author[2].tenant_id, project_id=author[2].project_id)
    await access_db[2].grant(provisioned[0], other_id, Grant(role="developer", scope=scope))
    denied = await author[0].post(url + "/commands", headers={**other_headers, "If-Match": "2"}, json=RETRY)
    assert (denied.status_code, denied.json()["code"]) == (403, "WV-FORBIDDEN")
    missing = url.rsplit("/", 1)[0] + "/00000000-0000-4000-8000-000000000000"
    unknown = await author[0].post(missing + "/commands", headers={**headers, "If-Match": "2"}, json=RETRY)
    assert unknown.status_code == 404


async def test_an_expired_session_refuses_a_resolve(author, headers, project_url, access_db):
    from sqlalchemy import text

    url, _ = await in_doubt(author, headers, project_url)
    async with access_db[1].begin() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:'||:t||':'||:p,0))"),
            {"t": str(author[2].tenant_id), "p": str(author[2].project_id)},
        )
        await session.execute(text("UPDATE debug_sessions SET expires_at=clock_timestamp()-interval '1 second'"))
    expired = await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json=RETRY)
    assert (expired.status_code, expired.json()["code"]) == (410, "WV-DEBUG-EXPIRED")


async def test_a_stale_revision_refuses_a_resolve(author, headers, project_url):
    url, _ = await in_doubt(author, headers, project_url)
    stale = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json=RETRY)
    assert (stale.status_code, stale.json()["code"]) == (409, "WV-DEBUG-REVISION")


async def test_an_inapplicable_resolution_answers_with_the_debug_code(author, headers, project_url):
    url, _ = await in_doubt(author, headers, project_url)
    wrong = {**RETRY, "resolution": "accept_reconciled_failure"}
    refused = await author[0].post(url + "/commands", headers={**headers, "If-Match": "2"}, json=wrong)
    assert refused.status_code == 422, refused.text
    assert refused.json() == {
        "status": 422,
        "code": "WV-DEBUG-INCIDENT",
        "message": "Invalid or over-budget simulation command",
        "request_id": refused.headers["X-Weave-Request-ID"],
        "diagnostics": [],
    }
    assert (await author[0].get(url, headers=headers)).json()["revision"] == 2
