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
"""Transactional metadata outbox, immutable pins, and fenced durable attempts."""
from alembic import op
revision = "0017_outbox"
down_revision = "0016_broker_receipts"
def upgrade():
    op.execute("ALTER TABLE connection_source_bindings DROP CONSTRAINT connection_source_bindings_source_kind_check")
    op.execute("ALTER TABLE connection_source_bindings ADD CHECK(source_kind IN ('kafka-trigger','outbox-subscription'))")
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute("""CREATE TABLE integration_events(id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
        environment_id uuid,payload jsonb NOT NULL,UNIQUE(tenant_id,project_id,id),
        FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
        FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE event_subscriptions(id uuid PRIMARY KEY,{scope},name text NOT NULL,revision integer NOT NULL,
        binding_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES principals(id),payload jsonb NOT NULL,
        active boolean NOT NULL DEFAULT true,UNIQUE({key},id),UNIQUE({key},name,revision),
        FOREIGN KEY({key},binding_id) REFERENCES connection_source_bindings({key},id))""")
    op.execute(f"CREATE UNIQUE INDEX one_active_subscription ON event_subscriptions({key},name) WHERE active")
    op.execute(f"""CREATE TABLE event_deliveries(id uuid PRIMARY KEY,{scope},event_id uuid NOT NULL,subscription_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','leased','retry','delivered','incident')),
        attempts integer NOT NULL DEFAULT 0,attempt_limit integer NOT NULL DEFAULT 3,
        token uuid,lease_until timestamptz,next_at timestamptz NOT NULL DEFAULT clock_timestamp(),code text,
        UNIQUE({key},id),UNIQUE(event_id,subscription_id),
        FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES integration_events(tenant_id,project_id,id),
        FOREIGN KEY({key},subscription_id) REFERENCES event_subscriptions({key},id))""")
    op.execute(f"""CREATE TABLE delivery_attempts({scope},delivery_id uuid NOT NULL,generation integer NOT NULL,id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
        token uuid NOT NULL,started_at timestamptz NOT NULL DEFAULT clock_timestamp(),finished_at timestamptz,
        outcome text,provider_version text,PRIMARY KEY(delivery_id,generation),
        FOREIGN KEY({key},delivery_id) REFERENCES event_deliveries({key},id))""")
    op.execute("""CREATE FUNCTION weave_delivery_scope() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF EXISTS(SELECT 1 FROM integration_events e WHERE e.id=NEW.event_id AND
          e.environment_id IS NOT NULL AND e.environment_id<>NEW.environment_id) THEN
          RAISE EXCEPTION 'Integration event environment mismatch'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER delivery_scope BEFORE INSERT ON event_deliveries FOR EACH ROW EXECUTE FUNCTION weave_delivery_scope()")
    for table in ("integration_events","event_subscriptions","event_deliveries","delivery_attempts"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(active) ON event_subscriptions TO weave_app")
    op.execute("GRANT UPDATE(status,attempts,attempt_limit,token,lease_until,next_at,code) ON event_deliveries TO weave_app")
    op.execute("GRANT UPDATE(finished_at,outcome,provider_version) ON delivery_attempts TO weave_app")
    op.execute("CREATE INDEX delivery_ready ON event_deliveries(tenant_id,project_id,environment_id,next_at,id) WHERE status IN ('pending','retry','leased')")
    op.execute("CREATE TABLE outbox_environment_cursors(tenant_id uuid PRIMARY KEY REFERENCES tenants(id),environment_id uuid)")
    op.execute("ALTER TABLE outbox_environment_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox_environment_cursors FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_scope ON outbox_environment_cursors TO weave_app USING (tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid)")
    op.execute("GRANT SELECT,INSERT,UPDATE ON outbox_environment_cursors TO weave_app")
    op.execute("CREATE TABLE outbox_tenant_cursor(singleton boolean PRIMARY KEY CHECK(singleton),tenant_id uuid)")
    op.execute("INSERT INTO outbox_tenant_cursor VALUES(true,NULL)")
    op.execute("GRANT SELECT,UPDATE ON outbox_tenant_cursor TO weave_catalog_reader")
    op.execute("""CREATE FUNCTION weave_outbox_tenants(page_size integer) RETURNS SETOF uuid
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE prior uuid; candidate uuid; visited uuid[] := ARRAY[]::uuid[];
      BEGIN
        IF page_size IS NULL OR page_size<1 OR page_size>16 THEN RAISE EXCEPTION 'Bounded catalog page required'; END IF;
        SELECT tenant_id INTO prior FROM public.outbox_tenant_cursor WHERE singleton FOR UPDATE;
        FOR candidate IN SELECT id FROM public.tenants WHERE prior IS NULL OR id>prior ORDER BY id LIMIT page_size LOOP
          visited := array_append(visited,candidate); RETURN NEXT candidate; END LOOP;
        IF array_length(visited,1) IS NULL OR array_length(visited,1)<page_size THEN
          FOR candidate IN SELECT id FROM public.tenants WHERE id<=prior ORDER BY id
            LIMIT page_size-coalesce(array_length(visited,1),0) LOOP
            visited := array_append(visited,candidate); RETURN NEXT candidate; END LOOP;
        END IF;
        IF array_length(visited,1)>0 THEN UPDATE public.outbox_tenant_cursor SET tenant_id=visited[array_length(visited,1)] WHERE singleton; END IF;
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute("ALTER FUNCTION weave_outbox_tenants(integer) OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_outbox_tenants(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_outbox_tenants(integer) TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0017_outbox'")
def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
