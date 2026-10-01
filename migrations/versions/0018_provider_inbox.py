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
"""Immutable scoped provider sources, receipts, dispatch intents and private traversal."""

from alembic import op

revision = "0018_provider_inbox"
down_revision = "0017_outbox"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute("ALTER TABLE connection_source_bindings DROP CONSTRAINT connection_source_bindings_source_kind_check")
    op.execute(
        "ALTER TABLE connection_source_bindings ADD CONSTRAINT connection_source_bindings_source_kind_check CHECK(source_kind IN ('kafka-trigger','outbox-subscription','provider-source'))"
    )
    op.execute(f"""CREATE TABLE provider_sources(id uuid PRIMARY KEY,{scope},binding_id uuid NOT NULL,
      principal_id uuid NOT NULL REFERENCES principals(id),activation_id uuid,run_id uuid,payload jsonb NOT NULL,
      disabled boolean NOT NULL DEFAULT false,UNIQUE({key},id),
      FOREIGN KEY({key},binding_id) REFERENCES connection_source_bindings({key},id),
      FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id),
      FOREIGN KEY({key},run_id) REFERENCES runs({key},id),CHECK((activation_id IS NULL)<>(run_id IS NULL)))""")
    op.execute(f"""CREATE TABLE provider_receipts(id uuid PRIMARY KEY,{scope},source_id uuid NOT NULL,
      event_id text NOT NULL,kind text NOT NULL,fingerprint text NOT NULL,event jsonb NOT NULL,mapped jsonb NOT NULL,
      receipt jsonb NOT NULL,state text NOT NULL CHECK(state IN ('pending','dispatched','ignored','blocked','failed')),
      received_at timestamptz NOT NULL,UNIQUE({key},id),UNIQUE({key},source_id,id),UNIQUE({key},source_id,kind,event_id),
      FOREIGN KEY({key},source_id) REFERENCES provider_sources({key},id))""")
    op.execute(f"""CREATE TABLE provider_intents(receipt_id uuid PRIMARY KEY,{scope},source_id uuid NOT NULL,
      pending boolean NOT NULL,eligible_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY({key},source_id,receipt_id) REFERENCES provider_receipts({key},source_id,id),
      FOREIGN KEY({key},source_id) REFERENCES provider_sources({key},id))""")
    op.execute(
        "CREATE INDEX provider_intents_due ON provider_intents(tenant_id,project_id,environment_id,eligible_at,receipt_id) WHERE pending"
    )
    for table in ("provider_sources", "provider_receipts", "provider_intents"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY provider_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(disabled) ON provider_sources TO weave_app")
    op.execute("GRANT UPDATE(state,receipt) ON provider_receipts TO weave_app")
    op.execute("GRANT UPDATE(pending,eligible_at) ON provider_intents TO weave_app")
    op.execute(
        "GRANT SELECT(id,tenant_id,project_id,environment_id,disabled) ON provider_sources TO weave_trigger_reader"
    )
    op.execute("CREATE POLICY provider_lookup ON provider_sources FOR SELECT TO weave_trigger_reader USING(true)")
    op.execute("""CREATE FUNCTION weave_provider_route(identifier uuid) RETURNS TABLE(tenant_id uuid,project_id uuid,environment_id uuid)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT tenant_id,project_id,environment_id FROM public.provider_sources WHERE id=identifier AND NOT disabled $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_trigger_reader")
    op.execute("ALTER FUNCTION weave_provider_route(uuid) OWNER TO weave_trigger_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_trigger_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_provider_route(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_provider_route(uuid) TO weave_app")
    op.execute(
        "CREATE TABLE provider_environment_cursors(tenant_id uuid PRIMARY KEY REFERENCES tenants(id),environment_id uuid)"
    )
    op.execute("ALTER TABLE provider_environment_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_environment_cursors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_scope ON provider_environment_cursors TO weave_app USING(tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid) WITH CHECK(tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid)"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON provider_environment_cursors TO weave_app")
    op.execute("CREATE TABLE provider_tenant_cursor(singleton boolean PRIMARY KEY CHECK(singleton),tenant_id uuid)")
    op.execute("INSERT INTO provider_tenant_cursor VALUES(true,NULL)")
    op.execute("GRANT SELECT,UPDATE ON provider_tenant_cursor TO weave_catalog_reader")
    op.execute("""CREATE FUNCTION weave_provider_tenants(page_size integer) RETURNS SETOF uuid
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE prior uuid; candidate uuid; visited uuid[] := ARRAY[]::uuid[];
      BEGIN
        IF page_size IS NULL OR page_size<1 OR page_size>16 THEN RAISE EXCEPTION 'Bounded catalog page required'; END IF;
        SELECT tenant_id INTO prior FROM public.provider_tenant_cursor WHERE singleton FOR UPDATE;
        FOR candidate IN SELECT id FROM public.tenants WHERE prior IS NULL OR id>prior ORDER BY id LIMIT page_size LOOP
          visited := array_append(visited,candidate); RETURN NEXT candidate;
        END LOOP;
        IF array_length(visited,1) IS NULL OR array_length(visited,1)<page_size THEN
          FOR candidate IN SELECT id FROM public.tenants WHERE id<=prior ORDER BY id LIMIT page_size-coalesce(array_length(visited,1),0) LOOP
            visited := array_append(visited,candidate); RETURN NEXT candidate;
          END LOOP;
        END IF;
        IF array_length(visited,1)>0 THEN UPDATE public.provider_tenant_cursor SET tenant_id=visited[array_length(visited,1)] WHERE singleton; END IF;
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute("ALTER FUNCTION weave_provider_tenants(integer) OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_provider_tenants(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_provider_tenants(integer) TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0018_provider_inbox'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
