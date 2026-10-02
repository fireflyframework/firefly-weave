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

"""Real RLS-backed discovery returns only the linked caller's accessible scopes."""

import pytest

pytestmark = pytest.mark.integration


async def test_discovery_does_not_list_other_tenant(client, headers, other_headers, provisioned):
    first = await client.get("/api/v1/identity", headers=headers)
    second = await client.get("/api/v1/identity", headers=other_headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["principal_id"] != second.json()["principal_id"]
    assert [w["id"] for w in first.json()["workspaces"]] == [str(provisioned[1][0].tenant_id)]
    assert [w["id"] for w in second.json()["workspaces"]] == [str(provisioned[1][1].tenant_id)]
    project = first.json()["workspaces"][0]["projects"][0]
    assert project["id"] == str(provisioned[1][0].project_id)
    assert [e["id"] for e in project["environments"]] == [str(provisioned[1][0].environment_id)]
    assert "run.read" in first.json()["grants"][0]["capabilities"]
    assert (await client.get("/api/v1/identity")).status_code == 401
