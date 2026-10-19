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


"""Activating a workflow that calls others pins one live activation of each called workflow version."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.catalog import Activation, ActivationRequest

pytestmark = pytest.mark.integration

EXAMPLES = Path("examples/language")


def source(name):
    return json.dumps(parse_source((EXAMPLES / name).read_text(), format="yaml").value)


async def stored(access_db, identifier):
    async with access_db[1]() as session:
        payload = await session.scalar(
            text("SELECT payload FROM activation_revisions WHERE id=:id"), {"id": identifier}
        )
    return Activation.model_validate_json(json.dumps(payload))


async def publish_and_activate(definitions, actor, scope, name, key, **pins):
    published = await definitions.publish(actor, scope, "Workflow", source(name), "json", key, context=AuditContext())
    request = ActivationRequest(scope=scope, version_id=published.id, artifact_digest=published.digest, **pins)
    return published, request


@pytest.fixture
def platform(worker_setup, services, access_db):
    from firefly_weave.definitions.service import DefinitionService

    actor, scope = worker_setup[3:5]
    return services(access_db[0]).resolve(DefinitionService), actor, scope


async def test_activation_pins_one_live_activation_of_each_called_workflow(platform, access_db):
    from firefly_weave.definitions.calls import call_tree_side_effect, pinned_call_tree
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.definitions.repository import DefinitionRepository

    definitions, actor, scope = platform
    _, callee_request = await publish_and_activate(definitions, actor, scope, "notify-customer.workflow.yaml", "callee")
    callee = await definitions.activate(actor, scope, callee_request, "callee", context=AuditContext())
    assert (callee.workflow_call_pins, callee.call_depth) == ([], 0)
    published, request = await publish_and_activate(definitions, actor, scope, "order-intake.workflow.yaml", "caller")

    with pytest.raises(CatalogError) as unpinned:
        await definitions.activate(actor, scope, request, "unpinned", context=AuditContext())
    assert (unpinned.value.status, unpinned.value.code) == (422, "WV-READINESS-CALL")

    pinned = request.model_copy(update={"workflow_activation_ids": {"notify-customer@1.0.0": callee.id}})
    activation = await definitions.activate(actor, scope, pinned, "pinned", context=AuditContext())
    [pin] = activation.workflow_call_pins
    assert (pin.reference, pin.activation_id, pin.activation_revision) == ("notify-customer@1.0.0", callee.id, 1)
    assert (pin.version_id, pin.artifact_digest) == (callee.request.version_id, callee.request.artifact_digest)
    assert activation.call_depth == 1
    assert await stored(access_db, activation.id) == activation

    async with definitions.uow.open(scope, mutation=False) as tx:
        resolve = pinned_call_tree(DefinitionRepository(tx), scope)
        row = await DefinitionRepository(tx).version(published.id)
        pins = {pin.reference: pin.activation_id}
        assert await call_tree_side_effect(row["artifact"]["executable"], pins, resolve) == "read_only"

    await definitions.retire_activation(actor, scope, callee.id, "retire-callee", 1, context=AuditContext())
    with pytest.raises(CatalogError) as retired:
        await definitions.activate(actor, scope, pinned, "after-retire", 1, context=AuditContext())
    assert (retired.value.code, retired.value.message) == (
        "WV-READINESS-CALL",
        "A pinned workflow activation or its version is retired",
    )

    retired_caller = await definitions.retire_activation(
        actor, scope, activation.id, "retire-caller", 1, context=AuditContext()
    )
    assert retired_caller.retired
    assert (retired_caller.workflow_call_pins, retired_caller.call_depth) == (activation.workflow_call_pins, 1)
    assert await stored(access_db, retired_caller.id) == retired_caller
