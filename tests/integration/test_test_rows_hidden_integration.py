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
"""A step test's version and activation stay out of every list, lookup, compile catalog and normal start."""

import json
from uuid import UUID

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.definitions.calls import pinned_callee
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService

pytestmark = pytest.mark.integration
VERSION = UUID("0123456789ab4cde8f0123456789abcd")
ACTIVATION = UUID("0123456789ab4cde8f0123456789abce")
CONTEXT = AuditContext()


@pytest.fixture
async def platform(worker_setup, services, access_db):
    """The fixture workflow, published and activated, plus a step test's copy of both written as the service will."""
    sessions, owner, access, _ = access_db
    actor, scope, activation = worker_setup[3], worker_setup[4], worker_setup[5]
    definitions = services(sessions).resolve(DefinitionService)
    async with owner() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO definition_versions(id,tenant_id,project_id,kind,name,version,digest,definition_digest,"
                "document,artifact,test) SELECT :id,tenant_id,project_id,kind,name,version||'+test.0123456789ab',"
                "digest,definition_digest,document,artifact,true FROM definition_versions WHERE id=:published"
            ),
            {"id": VERSION, "published": activation.request.version_id},
        )
        payload = json.loads(activation.model_dump_json())
        payload.update(id=str(ACTIVATION), name="test-0123456789ab", revision=1)
        payload["request"]["version_id"] = str(VERSION)
        await session.execute(
            text(
                "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,"
                "payload,test) VALUES(:id,:tenant,:project,:environment,'test-0123456789ab',1,:version,"
                "cast(:payload AS jsonb),true)"
            ),
            {
                "id": ACTIVATION,
                "tenant": scope.tenant_id,
                "project": scope.project_id,
                "environment": scope.environment_id,
                "version": VERSION,
                "payload": json.dumps(payload),
            },
        )
        await session.execute(
            text(
                "INSERT INTO test_activation_details(tenant_id,project_id,environment_id,activation_id,principal_id,"
                "source,draft_id,draft_revision,step_id,side_effect,created_at,expires_at) VALUES(:tenant,:project,"
                ":environment,:id,:principal,'step',:id,1,'step','read_only',now(),now()+interval '5 minutes')"
            ),
            {
                "id": ACTIVATION,
                "tenant": scope.tenant_id,
                "project": scope.project_id,
                "environment": scope.environment_id,
                "principal": actor.id,
            },
        )
    return definitions, actor, scope, activation, sessions, owner, access


async def test_workflow_lists_and_reads_dont_show_test_versions(platform):
    definitions, actor, scope, activation, *_ = platform
    page = await definitions.list(actor, scope, "Workflow", limit=100, context=CONTEXT)
    ids = {item["id"] for item in page["items"]}
    assert str(activation.request.version_id) in ids and str(VERSION) not in ids
    for export in (False, True):
        with pytest.raises(CatalogError) as hidden:
            await definitions.read(actor, scope, "Workflow", VERSION, export=export, context=CONTEXT)
        assert (hidden.value.status, hidden.value.code) == (404, "WV-NOT-FOUND")


async def test_activation_lists_and_reads_dont_show_test_activations(platform):
    definitions, actor, scope, activation, *_ = platform
    page = await definitions.list(actor, scope, "activations", limit=100, context=CONTEXT)
    ids = {item["id"] for item in page["items"]}
    assert str(activation.id) in ids and str(ACTIVATION) not in ids
    with pytest.raises(CatalogError) as hidden:
        await definitions.read(actor, scope, "activations", ACTIVATION, context=CONTEXT)
    assert hidden.value.status == 404


async def test_a_normal_run_cant_start_from_a_test_activation(platform):
    definitions, actor, scope, *_, sessions, _, _ = platform
    async with definitions.transaction(scope, None, mutation=False) as tx:
        with pytest.raises(CatalogError) as hidden:
            await definitions.runtime_snapshot(actor, scope, ACTIVATION, context=CONTEXT, tx=tx)
    assert (hidden.value.status, hidden.value.code) == (404, "WV-NOT-FOUND")


async def test_a_step_test_finds_only_its_live_activation(platform):
    definitions, actor, scope, activation, _, owner, access = platform
    admin = await access.load_principal(actor.id)
    tester = admin.model_copy(update={"grants": (*admin.grants, Grant(role="step_tester", scope=scope))})
    async with definitions.transaction(scope, None, mutation=False) as tx:
        found, artifact = await definitions.runtime_snapshot(
            tester, scope, ACTIVATION, context=CONTEXT, tx=tx, test=True
        )
        assert found.id == ACTIVATION and artifact
        with pytest.raises(CatalogError) as normal:
            await definitions.runtime_snapshot(tester, scope, activation.id, context=CONTEXT, tx=tx, test=True)
        assert normal.value.status == 404
    async with owner() as session, session.begin():
        await session.execute(
            text("UPDATE test_activation_details SET retired_at=now() WHERE activation_id=:id"), {"id": ACTIVATION}
        )
    async with definitions.transaction(scope, None, mutation=False) as tx:
        with pytest.raises(CatalogError) as expired:
            await definitions.runtime_snapshot(tester, scope, ACTIVATION, context=CONTEXT, tx=tx, test=True)
    assert (expired.value.status, expired.value.code) == (410, "WV-TESTACTIVATION-EXPIRED")


async def test_the_compile_catalog_has_no_test_version_and_a_call_cant_pin_a_test_activation(platform):
    definitions, actor, scope, activation, *_ = platform
    async with definitions.transaction(scope, None, mutation=False) as tx:
        repository = DefinitionRepository(tx)
        snapshot = await repository.snapshot(definitions.capabilities)
        references = {reference for kind, reference in snapshot.resources if kind == "Workflow"}
        assert not any("+test." in reference for reference in references)
        assert await pinned_callee(repository, scope, ACTIVATION) is None
        assert (await pinned_callee(repository, scope, activation.id)) is not None
        with pytest.raises(CatalogError):
            await repository.version(VERSION)
        assert (await repository.version(VERSION, include_test=True))["test"] is True


async def test_retiring_a_test_activation_by_its_id_is_not_found(platform):
    definitions, actor, scope, *_ = platform
    with pytest.raises(CatalogError) as hidden:
        await definitions.retire_activation(actor, scope, ACTIVATION, "retire-test", 1, context=CONTEXT)
    assert hidden.value.status == 404
