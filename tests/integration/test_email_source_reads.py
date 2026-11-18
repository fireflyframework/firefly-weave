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

"""Email sources can be listed and read by people who read email: what each reads and starts, never its mail."""

from uuid import UUID

import pytest
from test_connections import connections as connections
from test_email_retarget import change
from test_email_retarget import mail as mail
from test_webhook_retarget import person

pytestmark = pytest.mark.integration


async def test_a_read_shows_what_the_source_reads_and_starts_and_nothing_else(mail):  # noqa: F811
    env = mail
    await change(
        env,
        "UPDATE email_sources SET uidvalidity=7,last_uid=41,policy=cast(:policy AS jsonb) WHERE id=:id",
        policy='{"held": true}',
    )
    source = await env.sources.read(env.creator, env.scope, env.source_id)
    assert source.model_dump(mode="json") == {
        "id": str(env.source_id),
        "connection_revision_id": str(env.connection_id),
        "activation_id": str(env.first.id),
        "mailbox": "INBOX",
        "revision": 1,
        "disabled": False,
    }


async def test_a_source_that_needs_a_rebaseline_reads_disabled(mail):  # noqa: F811
    env = mail
    await change(env, "UPDATE email_sources SET state='resync_required' WHERE id=:id")
    assert (await env.sources.read(env.creator, env.scope, env.source_id)).disabled is True


async def test_the_list_pages_this_environments_sources(mail):  # noqa: F811
    env = mail
    second = await env.sources.create(env.creator, env.scope, env.connection_id)
    first_page = await env.sources.list(env.creator, env.scope, limit=1)
    assert len(first_page["items"]) == 1 and first_page["next_cursor"] is not None
    rest = await env.sources.list(env.creator, env.scope, limit=1, cursor=UUID(first_page["next_cursor"]))
    assert rest["next_cursor"] is None
    listed = {item["id"]: item for item in first_page["items"] + rest["items"]}
    assert set(listed) == {str(env.source_id), str(second)}
    assert listed[str(second)]["activation_id"] is None
    assert set(listed[str(second)]) == {
        "id",
        "connection_revision_id",
        "activation_id",
        "mailbox",
        "revision",
        "disabled",
    }


async def test_another_environments_source_is_not_found(mail, provisioned):  # noqa: F811
    env = mail
    from firefly_weave.definitions.models import CatalogError

    other = provisioned[1][1]
    reader = await person(env.access, env.admin, other, "email_reader")
    with pytest.raises(CatalogError) as missing:
        await env.sources.read(reader, other, env.source_id)
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")
    assert (await env.sources.list(reader, other))["items"] == []


async def test_reading_needs_email_read(mail):  # noqa: F811
    env = mail
    from firefly_weave.access.authorization import AccessDenied

    outsider = await person(env.access, env.admin, env.scope, "deployer", "operator", "viewer")
    with pytest.raises(AccessDenied):
        await env.sources.read(outsider, env.scope, env.source_id)
    with pytest.raises(AccessDenied):
        await env.sources.list(outsider, env.scope)
    reader = await person(env.access, env.admin, env.scope, "email_reader")
    assert (await env.sources.read(reader, env.scope, env.source_id)).id == env.source_id
