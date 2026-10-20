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

"""Studio documents on real PostgreSQL: who may read and write them, run data, secrets, sizes, conflicts."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_definitions import author as author

from firefly_weave.access.models import Grant
from firefly_weave.contracts.access import Scope

pytestmark = pytest.mark.integration
RUN = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11"
ACTION = {  # a published action whose output marks a secret
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "crm.token", "version": "1.0.0"},
    "spec": {
        "implementation": {"kind": "worker", "taskType": "crm.token", "taskVersion": "1.0.0"},
        "sideEffect": "read_only",
        "timeoutSeconds": 60,
        "inputSchema": {"type": "object"},
        "outputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
        },
    },
}
WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "orders", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "object", "properties": {"password": {"type": "string", "x-secret": True}}},
        "outputSchema": {},
        "steps": [
            {"id": "token", "kind": "action", "uses": "crm.token@1.0.0", "with": {"literal": {}}},
            {"id": "local", "kind": "action", "uses": "crm.unpublished@1.0.0", "with": {"ref": "/input"}},
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


def test_data(**fields):
    return {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-10-20T10:00:00Z", **fields}


async def draft(author, headers, project_url):
    """A saved draft of WORKFLOW at revision 1, and the published action it calls."""
    # A worker action needs its task contract: a worker release in the environment provides it.
    contract = {
        "taskType": "crm.token",
        "taskVersion": "1.0.0",
        "inputSchema": ACTION["spec"]["inputSchema"],
        "outputSchema": ACTION["spec"]["outputSchema"],
        "sideEffect": "read_only",
        "timeoutSeconds": 60,
    }
    release = await author[0].post(
        f"{project_url}/environments/{author[2].environment_id}/worker-releases",
        headers=headers,
        json={"image_digest": "sha256:" + "c" * 64, "capabilities": [contract]},
    )
    assert release.status_code == 201, release.text
    published = await author[0].post(
        project_url + "/actions",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"source": json.dumps(ACTION), "format": "json"},
    )
    assert published.status_code == 201, published.text
    identifier = uuid4()
    saved = await author[0].put(f"{project_url}/drafts/{identifier}", headers=headers, json={"document": WORKFLOW})
    assert saved.status_code == 201, saved.text
    return identifier


def url(project_url, identifier, kind="test-data"):
    return f"{project_url}/drafts/{identifier}/studio/{kind}"


async def save(client, project_url, headers, identifier, document, *, revision=None, kind="test-data"):
    extra = {"If-Match": f'"{revision}"'} if revision is not None else {}
    return await client.put(
        url(project_url, identifier, kind),
        headers={**headers, **extra},
        json={"draft_revision": 1, "document": document},
    )


@pytest.fixture
async def person(access_db, provisioned):
    """Subject 1 (other_headers), given roles in the first project and its environment by each test."""
    sessions, _, access, _ = access_db
    admin, scopes = provisioned
    async with sessions.begin() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='1'"))
    project = Scope(tenant_id=scopes[0].tenant_id, project_id=scopes[0].project_id)
    bindings = []

    async def grant(project_roles, environment_roles=()):
        for role in project_roles:
            bindings.append(await access.grant(admin, identifier, Grant(role=role, scope=project)))
        for role in environment_roles:
            bindings.append(await access.grant(admin, identifier, Grant(role=role, scope=scopes[0])))

    async def revoke_all():
        for binding in bindings:
            await access.revoke_grant(admin, project, binding)
        bindings.clear()

    return grant, revoke_all


async def test_a_document_round_trips_and_keeps_two_revisions(author, headers, project_url, access_db):
    client, identifier = author[0], await draft(author, headers, project_url)
    first = await save(client, project_url, headers, identifier, test_data(input={"value": {}, "source": "manual"}))
    assert first.status_code == 201, first.text
    assert first.headers["etag"] == '"1"'
    body = first.json()
    assert body["revision"] == 1 and body["kind"] == "test-data" and body["contains_run_data"] is False
    for revision in (1, 2):
        again = await save(client, project_url, headers, identifier, test_data(), revision=revision)
        assert again.status_code == 200, again.text
    read = await client.get(url(project_url, identifier), headers=headers)
    assert read.json()["revision"] == 3 and read.json()["document"] == test_data()
    sessions = access_db[0]
    async with sessions.begin() as session:
        await session.execute(
            text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": str(author[2].tenant_id)}
        )
        kept = await session.scalar(text("SELECT array_agg(revision ORDER BY revision) FROM studio_documents"))
    assert kept == [2, 3]


async def test_an_update_needs_the_current_revision(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    await save(client, project_url, headers, identifier, test_data())
    missing = await save(client, project_url, headers, identifier, test_data())
    stale = await save(client, project_url, headers, identifier, test_data(), revision=7)
    assert (missing.status_code, stale.status_code) == (412, 412)
    assert stale.json()["code"] == "WV-ETAG"


async def test_a_save_with_a_secret_value_is_refused_with_pointers_only(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    refused = await save(
        client,
        project_url,
        headers,
        identifier,
        test_data(
            input={"value": {"password": "s3cret"}, "source": "manual"},
            pins={"token": {**PIN, "output": {"id": "t", "token": "abc"}}},
        ),
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "WV-STUDIO-DOCUMENT-SECRET"
    assert refused.json()["result"] == {"paths": ["/input", "/pins/token"]}
    assert "s3cret" not in refused.text and "abc" not in refused.text
    assert (await client.get(url(project_url, identifier), headers=headers)).status_code == 404


async def test_an_entry_the_platform_cannot_classify_is_refused_by_pointer(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    refused = await save(client, project_url, headers, identifier, test_data(pins={"local": {**PIN, "output": {}}}))
    assert refused.status_code == 422
    assert refused.json()["code"] == "WV-STUDIO-DOCUMENT-UNCLASSIFIED"
    assert refused.json()["result"] == {"paths": ["/pins/local"]}


async def test_run_data_is_sticky_and_a_client_cannot_clear_it(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    environment = str(author[2].environment_id)
    run = {"value": {}, "source": "run", "runId": RUN, "runEnvironmentId": environment}
    first = await save(client, project_url, headers, identifier, test_data(input=run))
    assert first.status_code == 201, first.text
    assert first.json()["contains_run_data"] is True
    assert first.json()["source_environment_ids"] == [environment]
    clean = await save(client, project_url, headers, identifier, test_data(), revision=1)
    assert clean.json()["contains_run_data"] is True and clean.json()["source_environment_ids"] == [environment]
    asserted = await client.put(
        url(project_url, identifier),
        headers={**headers, "If-Match": '"2"'},
        json={"draft_revision": 1, "document": test_data(), "contains_run_data": False},
    )
    assert asserted.status_code == 422


async def test_reading_run_data_needs_run_read_in_every_source_environment(
    author, headers, other_headers, project_url, person
):
    client, identifier = author[0], await draft(author, headers, project_url)
    run = {"value": {}, "source": "run", "runId": RUN, "runEnvironmentId": str(author[2].environment_id)}
    await save(client, project_url, headers, identifier, test_data(input=run))
    grant, revoke_all = person
    await grant(["developer"])
    assert (await client.get(url(project_url, identifier), headers=other_headers)).status_code == 403
    assert (await save(client, project_url, other_headers, identifier, test_data(), revision=1)).status_code == 403
    await grant([], ["viewer"])
    assert (await client.get(url(project_url, identifier), headers=other_headers)).status_code == 200
    await revoke_all()
    assert (await client.get(url(project_url, identifier), headers=other_headers)).status_code == 403


@pytest.mark.parametrize(
    ("kind", "roles", "read", "write"),
    [
        ("canvas", ["viewer"], 200, 403),
        ("canvas", ["developer"], 200, 200),
        ("test-data", ["viewer"], 403, 403),
        ("test-data", ["developer"], 200, 200),
    ],
)
async def test_each_kind_needs_its_capabilities(
    author, headers, other_headers, project_url, person, kind, roles, read, write
):
    client, identifier = author[0], await draft(author, headers, project_url)
    document = {"version": 2} if kind == "canvas" else test_data()
    await save(client, project_url, headers, identifier, document, kind=kind)
    await person[0](roles)
    assert (await client.get(url(project_url, identifier, kind), headers=other_headers)).status_code == read
    changed = await save(client, project_url, other_headers, identifier, document, revision=1, kind=kind)
    assert changed.status_code == write


async def test_sizes_are_bounded_per_kind(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    too_large = await save(client, project_url, headers, identifier, {"version": 2, "x": "x" * 262_144}, kind="canvas")
    assert too_large.status_code == 413 and too_large.json()["code"] == "WV-STUDIO-DOCUMENT-SIZE"
    data = test_data(input={"value": "x" * 1_048_576, "source": "manual"})
    assert (await save(client, project_url, headers, identifier, data)).status_code == 413


async def test_another_tenant_reads_nothing(author, headers, other_headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    await save(client, project_url, headers, identifier, test_data())
    assert (await client.get(url(project_url, identifier), headers=other_headers)).status_code in {403, 404}


async def test_retiring_a_draft_deletes_its_documents(author, headers, project_url):
    client, identifier = author[0], await draft(author, headers, project_url)
    await save(client, project_url, headers, identifier, test_data())
    await save(client, project_url, headers, identifier, {"version": 2}, kind="canvas")
    retired = await client.delete(f"{project_url}/drafts/{identifier}", headers={**headers, "If-Match": '"1"'})
    assert retired.status_code == 200, retired.text
    for kind in ("test-data", "canvas"):
        assert (await client.get(url(project_url, identifier, kind), headers=headers)).status_code == 404
    assert (await save(client, project_url, headers, identifier, test_data())).status_code == 409


async def test_saves_and_deletes_are_audited_without_values(author, headers, project_url, access_db):
    client, identifier = author[0], await draft(author, headers, project_url)
    await save(
        client, project_url, headers, identifier, test_data(input={"value": {"name": "Ada"}, "source": "manual"})
    )
    deleted = await client.delete(url(project_url, identifier), headers={**headers, "If-Match": '"1"'})
    assert deleted.status_code == 204
    sessions = access_db[0]
    async with sessions.begin() as session:
        rows = (
            await session.execute(
                text("SELECT action,details::text FROM access_audit WHERE action LIKE 'studio_document.%' ORDER BY id")
            )
        ).all()
    assert [row[0] for row in rows] == ["studio_document.save", "studio_document.delete"]
    assert all("Ada" not in row[1] for row in rows)
    assert json.loads(rows[0][1]) == {"kind": "test-data", "revision": 1, "contains_run_data": False}
