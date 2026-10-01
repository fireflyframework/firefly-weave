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

"""Public, separate-process acceptance with actual PostgreSQL and Keycloak."""

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]


async def test_host_product_flow_recovers(vertical_slice):
    result = await vertical_slice.execute()
    assert result["status"] == "succeeded"
    assert result["restarted_during_wait"] is True
    assert result["external_effect_count"] == 1
    assert result["stale_completion_rejected"] is True
    assert result["delivery_attempts"] == 2
    assert result["generations"] == [1, 2]
    assert result["output"] == {"approved": True, "receipt": "accepted", "customer": "demo"}


async def test_remote_worker_has_no_database_credentials(vertical_slice):
    result = await vertical_slice.execute()
    assert result["worker_database_credentials"] is False
    assert result["worker_server_imports"] == []


async def test_host_uses_public_routes_and_real_scoped_identity(vertical_slice):
    result = await vertical_slice.execute()
    assert result["identity_source"] == "keycloak-client-credentials"
    assert result["denials"] == {"worker_publish": 403, "cross_tenant": 403, "untrusted_client": 401}
    assert result["internal_route"] == 404


async def test_schema_invalid_workflow_rejected_before_publication(vertical_slice):
    result = await vertical_slice.execute()
    assert result["invalid_publish"] == 422
    assert result["invalid_version_persisted"] is False


async def test_original_activation_pins_survive_redeploy_and_restart(vertical_slice):
    result = await vertical_slice.execute()
    assert result["old_pins"] == result["recovered_pins"]
    assert result["old_version_id"] != result["new_version_id"]
    assert result["trace_after_restart"][: len(result["trace_before_restart"])] == result["trace_before_restart"]
    assert result["webhook_replay_same_run"] is True


async def test_logs_and_public_trace_do_not_contain_secrets(vertical_slice):
    result = await vertical_slice.execute()
    assert result["redaction_checked"] is True


async def test_fixture_cleanup_is_owned_and_retains_persistence(vertical_slice):
    result = await vertical_slice.execute()
    await vertical_slice.close()
    assert vertical_slice.cleanup_project_ids == [result["project_id"]]
    assert vertical_slice.retained_database is True
    assert vertical_slice.closed is True
