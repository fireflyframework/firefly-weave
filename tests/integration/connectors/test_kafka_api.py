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

"""Native broker metadata API, typed SDK and current grants on the same routes."""

import pytest
from test_definitions import author as author
from test_kafka import broker_record as broker_record
from test_kafka import kafka_backend as kafka_backend
from test_kafka import kafka_setup as kafka_setup

pytestmark = pytest.mark.integration


async def test_native_broker_sdk_reads_and_revokes_current_binding(
    kafka_setup, author, access_db, provisioned, headers, other_headers, env_url
):
    from httpx import ASGITransport

    from firefly_weave.access.models import Grant
    from firefly_weave.sdk.client import WeaveClient

    source, route, _, scope, _, _ = kafka_setup
    client, actor, _ = author
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="tenant_admin", scope=scope))
    transport = ASGITransport(app=client._transport.app)
    token = headers["Authorization"].removeprefix("Bearer ")
    async with WeaveClient("http://localhost", lambda: token, scope, transport=transport) as sdk:
        assert (await sdk.read_broker_trigger(route.id)).id == route.id
        assert any(item.id == route.id for item in (await sdk.list_broker_triggers()).items)
        assert (await sdk.read_source_binding(route.binding_id)).source_id == route.id
        assert (await sdk.revoke_source_binding(route.binding_id)).revoked
        assert (await sdk.read_source_binding(route.binding_id)).revoked
    denied = await client.get(
        "/api/v1" + env_url + "/connection-source-bindings/" + str(route.binding_id), headers=other_headers
    )
    assert denied.status_code == 403
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises(CatalogError) as rejected:
        await source.service.authorize(source.authority)
    assert str(rejected.value) == "Connection requirements are unavailable or incompatible"
