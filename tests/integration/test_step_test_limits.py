# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""A person runs at most four step tests at once per environment, counted under a lock so racing starts count."""

import asyncio

import pytest
from test_step_test_run import make_step_tests

from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


@pytest.fixture
async def step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture):
    return await make_step_tests(services, access_db, provisioned, worker_setup, worker_runtime_fixture)


async def test_a_fifth_running_step_test_waits(step_tests):
    for click in range(4):
        await step_tests.start(step_tests.tester, key=f"click-{click}")
    with pytest.raises(CatalogError) as busy:
        await step_tests.start(step_tests.tester, key="click-4")
    assert (busy.value.status, busy.value.code) == (429, "WV-STEPTEST-BUSY")
    assert busy.value.retry_after is not None and busy.value.retry_after >= 1
    assert await step_tests.scalar("SELECT count(*) FROM test_activation_details") == 4


async def test_limits_count_per_person(step_tests):
    for click in range(4):
        await step_tests.start(step_tests.tester, key=f"click-{click}")
    other = await step_tests.person("step_tester", "developer")
    accepted = await step_tests.start(other, key="other-1")
    assert accepted.status == "running"


async def test_two_starts_at_once_count_each_other(step_tests):
    for click in range(3):
        await step_tests.start(step_tests.tester, key=f"click-{click}")
    results = await asyncio.gather(
        step_tests.start(step_tests.tester, key="race-a"),
        step_tests.start(step_tests.tester, key="race-b"),
        return_exceptions=True,
    )
    refused = [result for result in results if isinstance(result, CatalogError)]
    assert len(refused) == 1 and refused[0].code == "WV-STEPTEST-BUSY", results
    assert await step_tests.scalar("SELECT count(*) FROM test_activation_details") == 4
