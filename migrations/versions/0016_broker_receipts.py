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

"""Scoped standing connection authority and durable Kafka receipts."""
from alembic import op

revision = "0016_broker_receipts"
down_revision = "0015_draft_retirement"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE connection_source_bindings(id uuid PRIMARY KEY,{scope},
        connection_revision_id uuid NOT NULL,source_kind text NOT NULL CHECK(source_kind='kafka-trigger'),
        source_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES principals(id),
        source_fingerprint text NOT NULL,generation bigint NOT NULL DEFAULT 1,revoked boolean NOT NULL DEFAULT false,
        UNIQUE({key},id),UNIQUE({key},source_kind,source_id),
        FOREIGN KEY({key},connection_revision_id) REFERENCES connection_revisions({key},id))""")
    op.execute(f"""CREATE TABLE broker_routes(id uuid PRIMARY KEY,{scope},binding_id uuid NOT NULL,
        principal_id uuid NOT NULL REFERENCES principals(id),activation_id uuid,run_id uuid,
        payload jsonb NOT NULL,disabled boolean NOT NULL DEFAULT false,blocked boolean NOT NULL DEFAULT false,owner_token uuid,
        generation bigint NOT NULL DEFAULT 0,lease_until timestamptz,last_admitted_at timestamptz,
        eligible_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE({key},id),FOREIGN KEY({key},binding_id) REFERENCES connection_source_bindings({key},id),
        FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id),CHECK((activation_id IS NULL)<>(run_id IS NULL)))""")
    op.execute(f"""CREATE TABLE broker_events(id uuid PRIMARY KEY,{scope},trigger_id uuid NOT NULL,
        cluster_id text NOT NULL,event_id uuid NOT NULL,request_hash text NOT NULL,run_id uuid NOT NULL,
        payload jsonb NOT NULL,UNIQUE({key},id),UNIQUE(tenant_id,project_id,trigger_id,cluster_id,event_id),
        FOREIGN KEY({key},trigger_id) REFERENCES broker_routes({key},id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    op.execute(f"""CREATE TABLE broker_receipts(id uuid PRIMARY KEY,{scope},trigger_id uuid NOT NULL,
        cluster_id text NOT NULL,topic text NOT NULL,partition_id integer NOT NULL CHECK(partition_id>=0),
        offset_id bigint NOT NULL CHECK(offset_id>=0),event_id uuid,request_hash text,semantic_id uuid,
        payload jsonb NOT NULL,UNIQUE({key},id),
        UNIQUE(tenant_id,project_id,trigger_id,cluster_id,topic,partition_id,offset_id),
        FOREIGN KEY({key},trigger_id) REFERENCES broker_routes({key},id),
        FOREIGN KEY({key},semantic_id) REFERENCES broker_events({key},id))""")
    op.execute(f"""CREATE TABLE broker_incidents(id uuid PRIMARY KEY,{scope},trigger_id uuid NOT NULL,
        receipt_id uuid NOT NULL,code text NOT NULL,evidence jsonb NOT NULL,
        FOREIGN KEY({key},trigger_id) REFERENCES broker_routes({key},id),
        FOREIGN KEY({key},receipt_id) REFERENCES broker_receipts({key},id) DEFERRABLE INITIALLY DEFERRED)""")
    for table in ("connection_source_bindings", "broker_routes", "broker_events", "broker_receipts", "broker_incidents"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(revoked,generation) ON connection_source_bindings TO weave_app")
    op.execute("GRANT UPDATE(disabled,blocked,owner_token,generation,lease_until,last_admitted_at,eligible_at) ON broker_routes TO weave_app")
    op.execute("CREATE TABLE broker_environment_cursors(tenant_id uuid PRIMARY KEY REFERENCES tenants(id),environment_id uuid)")
    op.execute("ALTER TABLE broker_environment_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE broker_environment_cursors FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_scope ON broker_environment_cursors TO weave_app USING (tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid)")
    op.execute("GRANT SELECT,INSERT,UPDATE ON broker_environment_cursors TO weave_app")
    op.execute("CREATE TABLE broker_tenant_cursor(singleton boolean PRIMARY KEY CHECK(singleton),tenant_id uuid)")
    op.execute("INSERT INTO broker_tenant_cursor VALUES(true,NULL)")
    op.execute("GRANT SELECT,UPDATE ON broker_tenant_cursor TO weave_catalog_reader")
    op.execute("""CREATE FUNCTION weave_broker_tenants(page_size integer) RETURNS SETOF uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE prior uuid; candidate uuid; visited uuid[] := ARRAY[]::uuid[];
        BEGIN
          IF page_size IS NULL OR page_size<1 OR page_size>16 THEN RAISE EXCEPTION 'Bounded catalog page required'; END IF;
          SELECT tenant_id INTO prior FROM public.broker_tenant_cursor WHERE singleton FOR UPDATE;
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
            UPDATE public.broker_tenant_cursor SET tenant_id=visited[array_length(visited,1)] WHERE singleton;
          END IF;
        END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute("ALTER FUNCTION weave_broker_tenants(integer) OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_broker_tenants(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_broker_tenants(integer) TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0016_broker_receipts'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
