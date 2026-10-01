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

"""Forward backfill under a real nonsuperuser, without weakening FORCE RLS."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def upgrade(connection, revision):
    def apply(sync):
        config = Config()
        config.set_main_option("script_location", str(Path("migrations").resolve()))
        config.attributes["connection"] = sync
        command.upgrade(config, revision)

    await connection.run_sync(apply)


async def test_forward_backfill_nonsuperuser_force_rls_and_rollback(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value())
    role = "weave_c2_migration_" + uuid4().hex
    app = "weave_c2_app_" + uuid4().hex
    ids = []
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0009_triggers")
            await connection.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
            await connection.execute(text(f"CREATE ROLE {app} NOLOGIN NOSUPERUSER NOBYPASSRLS IN ROLE weave_app"))
            await connection.execute(text(f"GRANT USAGE,CREATE ON SCHEMA public TO {role}"))
            await connection.execute(text(f"GRANT SELECT ON runs,principals TO {role}"))
            await connection.execute(text(f"GRANT REFERENCES ON runs,principals TO {role}"))
            await connection.execute(text(f"GRANT SELECT,UPDATE ON weave_schema_version,alembic_version TO {role}"))
            principal = uuid4()
            await connection.execute(text("INSERT INTO principals(id,kind) VALUES(:id,'human')"), {"id": principal})
            for _ in range(2):
                tenant, project, environment, version, activation = [uuid4() for _ in range(5)]
                parameters = dict(t=tenant, p=project, e=environment, v=version, a=activation, principal=principal)
                await connection.execute(text("INSERT INTO tenants VALUES(:t,'backfill')"), parameters)
                await connection.execute(text("INSERT INTO projects VALUES(:p,:t,'backfill')"), parameters)
                await connection.execute(text("INSERT INTO environments VALUES(:e,:t,:p,'backfill')"), parameters)
                await connection.execute(
                    text(
                        "INSERT INTO definition_versions VALUES(:v,:t,:p,'Workflow','backfill',"
                        "'1.0.0','digest','definition','{}','{}')"
                    ),
                    parameters,
                )
                await connection.execute(
                    text("INSERT INTO activation_revisions VALUES(:a,:t,:p,:e,'backfill',1,:v,'{}')"), parameters
                )
                for kind in ("c1", "legacy", "terminal"):
                    run = uuid4()
                    state = {"status": "suspended", "active": ["work"], "incident": "WV-TASK-AMBIGUOUS"}
                    if kind == "c1":
                        state["incidents"] = {
                            "work:3": {"node_id": "work", "generation": 3, "code": "WV-TASK-AMBIGUOUS"}
                        }
                    if kind == "terminal":
                        state["status"] = "failed"
                    await connection.execute(
                        text(
                            "INSERT INTO runs VALUES(:id,:t,:p,:e,:a,:principal,'{}','{}','{}',cast(:state AS jsonb))"
                        ),
                        {**parameters, "id": run, "state": json.dumps(state)},
                    )
                    ids.append((run, tenant, kind, state))
        with pytest.raises(RuntimeError, match="explicit EXECUTE"):
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
                await upgrade(connection, "0010_incidents")
        async with engine.begin() as connection:
            await connection.execute(text(f"GRANT EXECUTE ON FUNCTION weave_tenant_ids() TO {role}"))
        # A failed migration transaction must leave schema, snapshots and authority intact.
        with pytest.raises(RuntimeError, match="intentional rollback"):
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
                assert (
                    await connection.execute(
                        text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                    )
                ).one() == (False, False)
                assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0
                await upgrade(connection, "0010_incidents")
                raise RuntimeError("intentional rollback")
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT to_regclass('incidents')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009_triggers"
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            await upgrade(connection, "0010_incidents")
            assert not await connection.scalar(text("SELECT pg_has_role(current_user,'weave_catalog_reader','MEMBER')"))
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010_incidents"
            rows = (await connection.execute(text("SELECT * FROM incidents"))).mappings().all()
            assert len(rows) == 4
            for run, _tenant, kind, state in ids:
                assert await connection.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run}) == state
                projected = [row for row in rows if row["run_id"] == run]
                assert len(projected) == int(kind != "terminal")
                if projected:
                    item = projected[0]
                    assert item["revision"] == 1 and item["status"] == "active"
                    assert item["actor_id"] is None and item["resolved_at"] is None and item["resolution"] is None
                    assert item["node_id"] == ("work" if kind == "c1" else None)
                    assert item["generation"] == (3 if kind == "c1" else None)
                    assert item["origin_code"] == state["incident"]
            await connection.execute(text(f"SET LOCAL ROLE {app}"))
            assert not await connection.scalar(
                text("SELECT has_function_privilege(current_user,'weave_tenant_ids()','EXECUTE')")
            )
            assert await connection.scalar(text("SELECT count(*) FROM incidents")) == 0
            await connection.execute(text("SELECT set_config('weave.tenant_id',:t,true)"), {"t": str(ids[0][1])})
            assert await connection.scalar(text("SELECT count(*) FROM incidents")) == 2
    finally:
        await engine.dispose()
