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

"""The alerts migration on real PostgreSQL: upgrade, isolation, the one firing alert per rule, immutable history,
the alert manager role and the scheduler's tenant page."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0042_operations_alerts.py"
SCOPED_TABLES = (
    "alert_rules",
    "alert_rule_state",
    "alerts",
    "alert_events",
    "alert_channels",
    "alert_recipient_consents",
    "alert_notifications",
    "alert_messages",
    "alert_settings",
    "alert_preset_installs",
)
# Every environment of a tenant reads these: member addresses and hash keys belong to the tenant, and the evaluator
# claims due environments with only the tenant set.
TENANT_TABLES = ("alert_member_addresses", "alert_hash_keys", "alert_environments")


def migration():
    spec = importlib.util.spec_from_file_location("alerts_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
        yield engine, ids
    finally:
        await engine.dispose()


async def seed_alerts(connection, ids, *, environment=None):
    """One row in every alert table of an environment, as the services will write them."""
    values = {**ids, "environment": environment or ids["environment"]}
    rule, alert, channel, revision = uuid4(), uuid4(), uuid4(), uuid4()
    statements = (
        "INSERT INTO connection_revisions(id,tenant_id,project_id,environment_id,name,revision,connector_version_id,"
        "payload) VALUES(:revision,:tenant,:project,:environment,:name,1,:version,'{}')",
        "INSERT INTO alert_rules(tenant_id,project_id,environment_id,id,name,enabled,signal,selector,comparator,"
        "threshold,window_seconds,for_seconds,min_samples,severity,channel_ids,renotify_seconds,revision,created_at,"
        "updated_at,updated_by) VALUES(:tenant,:project,:environment,:rule,'Run failure rate above 10 %',true,"
        "'run.failure_rate','{}','gt',0.1,900,0,10,'warning',ARRAY[cast(:channel AS uuid)],0,1,now(),now(),"
        ":principal)",
        "INSERT INTO alert_rule_state(tenant_id,project_id,environment_id,rule_id,state,since,alert_id,revision) "
        "VALUES(:tenant,:project,:environment,:rule,'firing',now(),:alert,1)",
        "INSERT INTO alerts(tenant_id,project_id,environment_id,id,rule_id,rule_name,rule_revision,signal,severity,"
        "comparator,threshold,unit,state,started_at,value_at_start) VALUES(:tenant,:project,:environment,:alert,"
        ":rule,'Run failure rate above 10 %',1,'run.failure_rate','warning','gt',0.1,'ratio','firing',now(),0.2)",
        "INSERT INTO alert_events(tenant_id,project_id,environment_id,id,alert_id,rule_id,rule_revision,at,kind,"
        "value,reason) VALUES(:tenant,:project,:environment,:event,:alert,:rule,1,now(),'firing',0.2,'threshold')",
        "INSERT INTO alert_channels(tenant_id,project_id,environment_id,id,kind,name,connection_revision_id,"
        "recipients,owner_id,revision,created_at,updated_at) VALUES(:tenant,:project,:environment,:channel,'email',"
        "'On call',:revision,cast(:recipients AS jsonb),:principal,1,now(),now())",
        "INSERT INTO alert_recipient_consents(tenant_id,project_id,environment_id,id,address,domain,method,"
        "statement,created_by,created_at) VALUES(:tenant,:project,:environment,:consent,'oncall@vendor.example',"
        "'vendor.example','administrator_attested','Agreed by email',:principal,now())",
        "INSERT INTO alert_notifications(tenant_id,project_id,environment_id,id,alert_id,channel_id,kind,sequence,"
        "recipients_count,state,created_at) VALUES(:tenant,:project,:environment,:notification,:alert,:channel,"
        "'firing',0,2,'pending',now())",
        "INSERT INTO alert_messages(tenant_id,project_id,environment_id,id,notification_id,rule_id,channel_id,"
        "recipient_hash,request_id,attempt,state,sent_at) VALUES(:tenant,:project,:environment,gen_random_uuid(),"
        ":notification,:rule,:channel,repeat('a',64),gen_random_uuid(),0,'queued',now())",
        "INSERT INTO alert_settings(tenant_id,project_id,environment_id,recipient_domains,approval_channel_ids,"
        "revision,updated_at,updated_by) VALUES(:tenant,:project,:environment,ARRAY['vendor.example'],'{}',1,now(),"
        ":principal)",
        "INSERT INTO alert_preset_installs(tenant_id,project_id,environment_id,preset_id,version,rule_id,"
        "installed_at) VALUES(:tenant,:project,:environment,'platform-degraded',1,:rule,now())",
        "INSERT INTO alert_environments(tenant_id,project_id,environment_id,next_at) "
        "VALUES(:tenant,:project,:environment,now())",
    )
    recipients = json.dumps([{"member": str(ids["principal"])}, {"address": "oncall@vendor.example"}])
    for statement in statements:
        await connection.execute(
            text(statement),
            {
                **values,
                "rule": rule,
                "alert": alert,
                "channel": channel,
                "revision": revision,
                "name": "mail-" + revision.hex[:8],
                "event": uuid4(),
                "consent": uuid4(),
                "notification": uuid4(),
                "recipients": recipients,
            },
        )
    return {"rule": rule, "alert": alert, "channel": channel, "revision": revision}


async def seed_tenant_rows(connection, ids, tenant=None):
    values = {"tenant": tenant or ids["tenant"], "principal": ids["principal"]}
    await connection.execute(
        text(
            "INSERT INTO alert_member_addresses(tenant_id,principal_id,address,updated_at) "
            "VALUES(:tenant,:principal,'ana@example.org',now())"
        ),
        values,
    )
    await connection.execute(
        text(
            "INSERT INTO alert_hash_keys(tenant_id,key,created_at) VALUES(:tenant,decode(repeat('ab',32),'hex'),now())"
        ),
        values,
    )


async def test_upgrading_keeps_existing_data_and_creates_empty_alert_tables(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            bindings = text("SELECT id,role FROM role_bindings ORDER BY id")
            before = (await connection.execute(bindings)).all()
            await upgrade(connection, HEAD)
            assert (await connection.execute(bindings)).all() == before
            for table in (*SCOPED_TABLES, *TENANT_TABLES):
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            # No alert table counts toward usage, so usage charged before the upgrade stays exact.
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_runs_keep_their_byte_guard(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            runs = text("SELECT id,public.weave_runs_bytes(r) FROM runs r ORDER BY id")
            before = (await connection.execute(runs)).all()
            await upgrade(connection, HEAD)
            assert (await connection.execute(runs)).all() == before
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "WHERE t.tgname='operation_usage' AND c.relname LIKE 'alert%')"
                )
            )
    finally:
        await engine.dispose()


async def test_the_schema_version_is_the_new_head(upgraded):
    engine, _ = upgraded
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    from firefly_weave.persistence.migrations import SCHEMA_VERSION

    assert HEAD == SCHEMA_VERSION


async def test_every_alert_table_forces_row_level_security_and_another_environment_reads_nothing(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for table in (*SCOPED_TABLES, *TENANT_TABLES):
            forced = (
                await connection.execute(
                    text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:table"),
                    {"table": table},
                )
            ).one()
            assert forced == (True, True), table
        await seed_alerts(connection, ids)
        await seed_tenant_rows(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        for table in (*SCOPED_TABLES, *TENANT_TABLES):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1, table
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        for table in SCOPED_TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
        for table in TENANT_TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1, table
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, tenant=uuid4())
        for table in (*SCOPED_TABLES, *TENANT_TABLES):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table


async def test_the_app_writes_alert_rows_only_in_its_own_environment(upgraded):
    engine, ids = upgraded
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(
                text(
                    "INSERT INTO alert_environments(tenant_id,project_id,environment_id,next_at) "
                    "VALUES(:tenant,:project,:other_environment,now())"
                ),
                ids,
            )
    assert sqlstate(refused.value) == "42501"
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(
                text(
                    "INSERT INTO alert_settings(tenant_id,project_id,environment_id,revision,updated_at,updated_by) "
                    "VALUES(:tenant,:project,:other_environment,1,now(),:principal)"
                ),
                ids,
            )
    assert sqlstate(refused.value) == "42501"


async def test_only_one_alert_per_rule_can_be_firing(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await seed_alerts(connection, ids)
    insert = text(
        "INSERT INTO alerts(tenant_id,project_id,environment_id,id,rule_id,rule_name,rule_revision,signal,severity,"
        "comparator,threshold,unit,state,started_at,resolved_at,resolved_reason,value_at_start) VALUES(:tenant,"
        ":project,:environment,:id,:rule,'Rule',1,'run.failure_rate','warning','gt',0.1,'ratio',:state,now(),"
        "CASE WHEN :state='resolved' THEN now() END,CASE WHEN :state='resolved' THEN 'threshold' END,0.2)"
    )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(insert, {**ids, "id": uuid4(), "rule": seeded["rule"], "state": "resolved"})
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(insert, {**ids, "id": uuid4(), "rule": seeded["rule"], "state": "firing"})
    assert sqlstate(refused.value) == "23505"
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(insert, {**ids, "id": uuid4(), "rule": uuid4(), "state": "firing"})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE alert_events SET value=0",
        "UPDATE alert_messages SET recipient_hash=repeat('b',64)",
        "UPDATE alert_recipient_consents SET address='other@vendor.example'",
        "UPDATE alert_recipient_consents SET statement='Changed'",
        "UPDATE alert_hash_keys SET key=decode(repeat('cd',32),'hex')",
        "DELETE FROM alert_recipient_consents",
        "DELETE FROM alert_preset_installs",
    ],
)
async def test_alert_history_cant_be_changed_by_the_app(upgraded, statement):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_alerts(connection, ids)
        await seed_tenant_rows(connection, ids)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text(statement))
    assert sqlstate(refused.value) == "42501"


async def test_the_app_may_prune_events_revoke_opt_ins_and_resolve_alerts(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await seed_alerts(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        revoked = await connection.execute(
            text("UPDATE alert_recipient_consents SET revoked_at=now(),revoked_by=:principal"), ids
        )
        assert revoked.rowcount == 1
        resolved = await connection.execute(
            text("UPDATE alerts SET state='resolved',resolved_at=now(),resolved_reason='threshold' WHERE id=:alert"),
            seeded,
        )
        assert resolved.rowcount == 1
        assert (await connection.execute(text("DELETE FROM alert_events"))).rowcount == 1


@pytest.mark.parametrize(
    "recipients",
    [
        [],
        [{"member": str(uuid4())}] * 11,
        [{"member": str(uuid4()), "address": "a@vendor.example"}],
        [{"name": "Ana"}],
        [{"member": "not-a-principal"}],
        [{"address": 7}],
        ["oncall@vendor.example"],
        {"member": str(uuid4())},
    ],
)
async def test_a_channel_holds_one_to_ten_members_or_addresses(upgraded, recipients):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await seed_alerts(connection, ids)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(
                text("UPDATE alert_channels SET recipients=cast(:recipients AS jsonb) WHERE id=:channel"),
                {"recipients": json.dumps(recipients), "channel": seeded["channel"]},
            )
    assert sqlstate(refused.value) == "23514"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("threshold", "'NaN'"),
        ("threshold", "'Infinity'"),
        ("threshold", "-1"),
        ("for_seconds", "45"),
        ("for_seconds", "3630"),
        ("renotify_seconds", "600"),
        ("window_seconds", "120"),
        ("comparator", "'eq'"),
        ("severity", "'page'"),
        ("name", "''"),
        ("channel_ids", "array_fill(gen_random_uuid(),ARRAY[6])"),
    ],
)
async def test_a_rule_row_keeps_the_contract_limits(upgraded, column, value):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_alerts(connection, ids)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text(f"UPDATE alert_rules SET {column}={value}"))
    assert sqlstate(refused.value) == "23514"


async def test_only_a_rule_weave_installed_from_a_preset_has_no_updater(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_alerts(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        installed = await connection.execute(
            text("UPDATE alert_rules SET preset_id='platform-degraded',updated_by=NULL")
        )
        assert installed.rowcount == 1
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text("UPDATE alert_rules SET preset_id=NULL"))
    assert sqlstate(refused.value) == "23514"


async def test_the_role_check_accepts_the_alert_manager(upgraded):
    engine, ids = upgraded
    statement = text(
        "INSERT INTO role_bindings(id,principal_id,tenant_id,project_id,environment_id,role) "
        "VALUES(:id,:principal,:tenant,:project,:environment,:role)"
    )
    async with engine.begin() as connection:
        await connection.execute(statement, {**ids, "id": uuid4(), "role": "alert_manager"})
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(statement, {**ids, "id": uuid4(), "role": "alert_admin"})
    assert sqlstate(refused.value) == "23514"
    async with engine.begin() as connection:
        definition = await connection.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='role_bindings_role_check'")
        )
    from firefly_weave.access.roles import ROLE_CAPABILITIES

    assert {role for role in ROLE_CAPABILITIES if f"'{role}'" in definition} == set(ROLE_CAPABILITIES)


async def test_the_alert_tenant_page_is_for_the_scheduler_only(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        assert not await connection.scalar(
            text("SELECT has_function_privilege('weave_app','weave_alert_tenants(integer)','EXECUTE')")
        )
        assert await connection.scalar(
            text("SELECT has_function_privilege('weave_scheduler','weave_alert_tenants(integer)','EXECUTE')")
        )
        operations_before = await connection.scalar(text("SELECT tenant_id FROM operations_tenant_cursor"))
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE weave_scheduler"))
        page = list((await connection.execute(text("SELECT weave_alert_tenants(16)"))).scalars())
        assert ids["tenant"] in page
        for value in (0, 17):
            with pytest.raises(DBAPIError, match="Bounded catalog page"):
                async with connection.begin_nested():
                    await connection.execute(text("SELECT weave_alert_tenants(:size)"), {"size": value})
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT tenant_id FROM alert_tenant_cursor")) == page[-1]
        assert await connection.scalar(text("SELECT tenant_id FROM operations_tenant_cursor")) == operations_before
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE weave_app"))
            await connection.execute(text("SELECT weave_alert_tenants(16)"))
    assert sqlstate(refused.value) == "42501"


@pytest.mark.parametrize("column", ["tenant_id", "project_id", "environment_id"])
async def test_a_claim_changes_only_the_schedule_and_the_approval_position(upgraded, column):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_alerts(connection, ids)
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL ROLE weave_app"))
        await connection.execute(
            text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": str(ids["tenant"])}
        )
        moved = await connection.execute(
            text(
                "UPDATE alert_environments SET next_at=now(),approval_after_at=now(),approval_after_id=:id "
                "WHERE environment_id=:environment"
            ),
            {"id": uuid4(), "environment": ids["environment"]},
        )
        assert moved.rowcount == 1
    # Within a tenant, a request may move another environment's schedule, never the row itself.
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE weave_app"))
            await connection.execute(
                text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": str(ids["tenant"])}
            )
            await connection.execute(text(f"UPDATE alert_environments SET {column}={column}"))
    assert sqlstate(refused.value) == "42501"


async def test_the_alert_loop_claims_due_environments_of_a_tenant(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_alerts(connection, ids)
    async with engine.begin() as connection:
        # The evaluator claims due environments with only the tenant set, before it enters one environment.
        await connection.execute(text("SET LOCAL ROLE weave_app"))
        await connection.execute(
            text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": str(ids["tenant"])}
        )
        claimed = await connection.execute(
            text(
                "UPDATE alert_environments SET next_at=now()+interval '30 seconds' WHERE (tenant_id,project_id,"
                "environment_id) IN (SELECT tenant_id,project_id,environment_id FROM alert_environments "
                "WHERE next_at<=now() ORDER BY next_at LIMIT 8 FOR UPDATE SKIP LOCKED) RETURNING environment_id"
            )
        )
        assert list(claimed.scalars()) == [ids["environment"]]
