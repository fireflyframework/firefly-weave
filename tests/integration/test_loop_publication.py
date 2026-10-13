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

"""A platform publishes a workflow only when it runs every language feature the workflow uses."""

import json

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts import language_features

pytestmark = pytest.mark.integration

LOOP = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "label-items", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "array", "items": {"type": "string"}},
        "outputSchema": {"type": "array"},
        "steps": [
            {
                "id": "label",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "body": {
                    "steps": [],
                    "output": {"op": {"name": "concat", "args": [{"literal": "#"}, {"ref": "/item"}]}},
                },
            }
        ],
        "output": {"ref": "/steps/label/output"},
    },
}


async def versions(access_db):
    async with access_db[1]() as session:
        return await session.scalar(text("SELECT count(*) FROM definition_versions WHERE name='label-items'"))


async def test_a_platform_that_does_not_run_loops_refuses_to_publish_one(worker_setup, services, access_db):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.service import DefinitionService

    actor, scope = worker_setup[3:5]
    definitions = services(access_db[0]).resolve(DefinitionService)
    with pytest.raises(CatalogError) as refused:
        await definitions.publish(actor, scope, "Workflow", json.dumps(LOOP), "json", "loop", context=AuditContext())
    assert (refused.value.status, refused.value.code) == (422, "WV-IR-UNSUPPORTED")
    assert refused.value.result == {"reason": "ir_unsupported", "missing_features": ["flow.forEach"]}
    assert await versions(access_db) == 0


async def test_a_platform_that_runs_loops_publishes_one_and_reads_it_back(
    worker_setup, services, access_db, monkeypatch
):
    from firefly_weave.definitions.service import DefinitionService

    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ("flow.forEach", "text.concat", "text.join"))
    actor, scope = worker_setup[3:5]
    definitions = services(access_db[0]).resolve(DefinitionService)
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(LOOP), "json", "loop", context=AuditContext()
    )
    read = await definitions.read(actor, scope, "Workflow", published.id, export=True, context=AuditContext())
    assert read["digest"] == published.digest
    assert read["artifact"]["executable"]["features"] == ["flow.forEach", "text.concat"]
    assert await versions(access_db) == 1
