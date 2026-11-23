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

"""Purging a run through the API while a trigger start still waits to read one of its platform events."""

from uuid import UUID

import pytest
import test_runs as runs
from test_event_trigger_migration import insert_start, insert_trigger

from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration
author = runs.author
publication_request = runs.publication_request
start_request = runs.start_request


async def test_a_trigger_start_still_waiting_to_run_blocks_the_purge_and_says_so(
    client, headers, env_url, start_request, access_db, provisioned, author
):
    started = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "source"}, json=start_request)
    assert started.status_code == 201, started.text
    run = started.json()["id"]
    scope = author[2]
    ids = {
        "tenant": scope.tenant_id,
        "project": scope.project_id,
        "environment": scope.environment_id,
        "activation": UUID(start_request["activation_id"]),
        "principal": author[1].id,
        "first_run": UUID(run),
    }
    async with access_db[1]() as owner, owner.begin():
        # A start of the run's event that a trigger requested and the platform has not run yet.
        await insert_start(owner, ids, await insert_trigger(owner, ids))
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="execution_manager", scope=scope))
    url = env_url + "/runs/" + run
    archived = await client.post(
        url + "/archive",
        headers={**headers, "Idempotency-Key": "archive"},
        json={"expected_revision": 0, "reason": "Done"},
    )
    assert archived.status_code == 200, archived.text

    response = await client.post(
        url + "/purge",
        headers={**headers, "Idempotency-Key": "purge"},
        json={"expected_revision": 1, "reason": "Done", "confirm_run_id": run},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WV-RUN-PURGE-BLOCKED"
    assert "a trigger start still waiting to run" in response.json()["message"]
    assert (await client.get(url, headers=headers)).status_code == 200
