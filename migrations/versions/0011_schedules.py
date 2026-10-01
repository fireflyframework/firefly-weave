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

"""Durable schedule revisions, occurrence ranges, timer continuation and traversal."""
from alembic import op
import sqlalchemy as sa

revision = "0011_schedules"
down_revision = "0010_incidents"


def upgrade():
    permitted = op.get_bind().scalar(sa.text("""SELECT rolsuper OR (
        pg_has_role(current_user,'weave_catalog_reader','SET') AND
        has_schema_privilege(current_user,'public','CREATE WITH GRANT OPTION') AND
        pg_has_role(current_user,(SELECT relowner FROM pg_class WHERE oid='wait_wakeups'::regclass),'USAGE'))
        FROM pg_roles WHERE rolname=current_user"""))
    if not permitted:
        raise RuntimeError("Schedule migration requires explicit catalog ownership-transfer authority, schema CREATE grant option, and ownership of wait_wakeups; narrow catalog EXECUTE alone is insufficient")
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE schedules(id uuid PRIMARY KEY,{scope},revision integer NOT NULL CHECK(revision>0),
        status text NOT NULL CHECK(status IN ('enabled','disabled','blocked','deleted')),
        next_due_at timestamptz NOT NULL,blocked_reason text,UNIQUE({key},id),
        FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE schedule_revisions({scope},id uuid NOT NULL,revision integer NOT NULL,
        principal_id uuid NOT NULL REFERENCES principals(id),activation_id uuid NOT NULL,
        payload jsonb NOT NULL,created_at timestamptz NOT NULL,PRIMARY KEY(id,revision),UNIQUE({key},id,revision),
        FOREIGN KEY({key},id) REFERENCES schedules({key},id),
        FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id))""")
    op.execute(f"""CREATE TABLE schedule_occurrences(sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        {scope},schedule_id uuid NOT NULL,revision integer NOT NULL,instant timestamptz NOT NULL,
        through timestamptz NOT NULL,observed_at timestamptz NOT NULL,kind text NOT NULL CHECK(kind IN ('started','skipped')),
        reason text,run_id uuid,UNIQUE(schedule_id,revision,instant),CHECK(through>=instant),
        CHECK((kind='started' AND run_id IS NOT NULL AND through=instant) OR (kind='skipped' AND run_id IS NULL)),
        FOREIGN KEY({key},schedule_id,revision) REFERENCES schedule_revisions({key},id,revision),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    op.execute("CREATE TABLE scheduler_environment_cursors(tenant_id uuid PRIMARY KEY REFERENCES tenants(id),environment_id uuid)")
    for table in ("schedules", "schedule_revisions", "schedule_occurrences", "scheduler_environment_cursors"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(revision,status,next_due_at,blocked_reason) ON schedules TO weave_app")
    op.execute("GRANT UPDATE(environment_id) ON scheduler_environment_cursors TO weave_app")
    op.execute("GRANT USAGE ON SEQUENCE schedule_occurrences_sequence_seq TO weave_app")
    op.execute("CREATE INDEX schedules_due ON schedules(tenant_id,project_id,environment_id,next_due_at,id) WHERE status='enabled'")
    op.execute("ALTER TABLE wait_wakeups DROP CONSTRAINT wait_wakeups_wakeup_kind_check")
    op.execute("ALTER TABLE wait_wakeups ADD CHECK(wakeup_kind IN ('signal','timeout','elapsed'))")
    op.execute("CREATE TABLE scheduler_tenant_cursor(singleton boolean PRIMARY KEY CHECK(singleton),tenant_id uuid)")
    op.execute("INSERT INTO scheduler_tenant_cursor VALUES(true,NULL)")
    op.execute("GRANT SELECT,UPDATE ON scheduler_tenant_cursor TO weave_catalog_reader")
    op.execute("""CREATE FUNCTION weave_scheduler_tenants(page_size integer) RETURNS SETOF uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE prior uuid; candidate uuid; visited uuid[] := ARRAY[]::uuid[];
        BEGIN
          IF page_size<1 OR page_size>16 THEN RAISE EXCEPTION 'Bounded catalog page required'; END IF;
          SELECT tenant_id INTO prior FROM public.scheduler_tenant_cursor WHERE singleton FOR UPDATE;
          FOR candidate IN SELECT id FROM public.tenants WHERE prior IS NULL OR id>prior ORDER BY id LIMIT page_size LOOP
            visited := array_append(visited,candidate); RETURN NEXT candidate;
          END LOOP;
          IF array_length(visited,1) IS NULL OR array_length(visited,1)<page_size THEN
            FOR candidate IN SELECT id FROM public.tenants WHERE id<=prior ORDER BY id
              LIMIT page_size-coalesce(array_length(visited,1),0) LOOP
              visited := array_append(visited,candidate); RETURN NEXT candidate;
            END LOOP;
          END IF;
          IF array_length(visited,1)>0 THEN
            UPDATE public.scheduler_tenant_cursor SET tenant_id=visited[array_length(visited,1)] WHERE singleton;
          END IF;
        END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute("ALTER FUNCTION weave_scheduler_tenants(integer) OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_scheduler_tenants(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_scheduler_tenants(integer) TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0011_schedules'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
