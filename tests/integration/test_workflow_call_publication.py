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


"""A platform publishes a workflow that calls another only once it runs workflow calls."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.compiler.parser import parse_source

pytestmark = pytest.mark.integration

EXAMPLES = Path("examples/language")


def source(name):
    return json.dumps(parse_source((EXAMPLES / name).read_text(), format="yaml").value)


async def test_a_platform_that_does_not_run_calls_refuses_to_publish_a_workflow_that_calls(
    worker_setup, services, access_db
):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService

    actor, scope = worker_setup[3:5]
    definitions = services(access_db[0]).resolve(DefinitionService)
    callee = await definitions.publish(
        actor, scope, "Workflow", source("notify-customer.workflow.yaml"), "json", "callee", context=AuditContext()
    )
    assert callee.name == "notify-customer"
    with pytest.raises(CatalogError) as refused:
        await definitions.publish(
            actor, scope, "Workflow", source("order-intake.workflow.yaml"), "json", "caller", context=AuditContext()
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-IR-UNSUPPORTED")
    assert refused.value.result == {"reason": "ir_unsupported", "missing_features": ["flow.callWorkflow"]}
    async with access_db[1]() as session:
        stored = await session.scalar(text("SELECT count(*) FROM definition_versions WHERE name='order-intake'"))
    assert stored == 0
