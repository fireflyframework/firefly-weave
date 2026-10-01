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

"""Forward operational admission, bounded catalog traversal and scoped maintenance evidence."""

from alembic import op

revision = "0021_operations"
down_revision = "0020_whatsapp_status"


def upgrade():
    op.execute("ALTER TABLE task_intents ADD COLUMN enqueued_at timestamptz")
    op.execute("ALTER TABLE task_intents ALTER COLUMN enqueued_at SET DEFAULT clock_timestamp()")
    op.execute("""DO $migration$ DECLARE body text; BEGIN
      SELECT pg_get_functiondef('public.weave_scheduler_tenants(integer)'::regprocedure) INTO body;
      IF position('IF page_size<1 OR page_size>16' IN body)=0 THEN
        RAISE EXCEPTION 'Unexpected scheduler function; verify predecessor'; END IF;
      EXECUTE replace(body,'IF page_size<1 OR page_size>16',
        'IF page_size IS NULL OR page_size<1 OR page_size>16');
    END $migration$""")
    op.execute("""CREATE FUNCTION weave_operation_fenced(tenant uuid,project uuid) RETURNS boolean
      LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
      SELECT tenant IS NOT NULL AND project IS NOT NULL AND EXISTS(
        SELECT 1 FROM pg_catalog.pg_locks
        WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted AND objsubid=1
          AND classid=((hashtextextended('weave.operations:'||tenant::text||':'||project::text,0)>>32)&4294967295)::oid
          AND objid=(hashtextextended('weave.operations:'||tenant::text||':'||project::text,0)&4294967295)::oid)
      $$""")
    op.execute("REVOKE ALL ON FUNCTION weave_operation_fenced(uuid,uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_operation_fenced(uuid,uuid) TO weave_app")
    op.execute("ALTER TABLE debug_sessions ADD COLUMN capacity_reserved boolean NOT NULL DEFAULT false")
    op.execute("UPDATE debug_sessions SET capacity_reserved=true WHERE expires_at>clock_timestamp()")
    op.execute("""CREATE FUNCTION weave_debug_admission() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $$
      DECLARE live_count integer; creator_count integer; retained_count integer;
      BEGIN
        IF NOT public.weave_operation_fenced(NEW.tenant_id,NEW.project_id) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Outer operation admission required'; END IF;
        NEW.capacity_reserved:=NEW.expires_at>clock_timestamp();
        IF TG_OP='INSERT' THEN
          UPDATE public.debug_sessions SET capacity_reserved=false WHERE id IN (
            SELECT id FROM public.debug_sessions WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id
              AND capacity_reserved AND expires_at<=clock_timestamp() ORDER BY id LIMIT 16);
          SELECT count(*) INTO live_count FROM (SELECT 1 FROM public.debug_sessions
            WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id AND expires_at>clock_timestamp()
            LIMIT 16) AS bounded;
          SELECT count(*) INTO creator_count FROM (SELECT 1 FROM public.debug_sessions
            WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id AND creator_id=NEW.creator_id
              AND expires_at>clock_timestamp() LIMIT 4) AS bounded;
          SELECT count(*) INTO retained_count FROM (SELECT 1 FROM public.debug_sessions
            WHERE tenant_id=NEW.tenant_id AND project_id=NEW.project_id LIMIT 1000) AS bounded;
          IF live_count>=16 OR creator_count>=4 OR retained_count>=1000 THEN
            RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Debug admission limit'; END IF;
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION weave_debug_admission() FROM PUBLIC")
    op.execute("CREATE INDEX debug_expiry_admission ON debug_sessions(tenant_id,project_id,creator_id,expires_at)")
    op.execute("CREATE TRIGGER debug_admission BEFORE INSERT OR UPDATE ON debug_sessions FOR EACH ROW EXECUTE FUNCTION weave_debug_admission()")
    op.execute("""CREATE TABLE runtime_capacity_blocks(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,run_id uuid PRIMARY KEY,
      sequence bigint NOT NULL,policy text NOT NULL DEFAULT 'weave/operations-v1' CHECK(policy='weave/operations-v1'),
      reason text NOT NULL DEFAULT 'WV-RUNTIME-LIMIT' CHECK(reason='WV-RUNTIME-LIMIT'),
      observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),active boolean NOT NULL DEFAULT true,
      FOREIGN KEY(tenant_id,project_id,environment_id,run_id) REFERENCES runs(tenant_id,project_id,environment_id,id))""")
    op.execute("ALTER TABLE runtime_capacity_blocks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_capacity_blocks FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_scope ON runtime_capacity_blocks USING (tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid)")
    op.execute("GRANT SELECT,INSERT,UPDATE(sequence,active,observed_at) ON runtime_capacity_blocks TO weave_app")
    op.execute("INSERT INTO runtime_capacity_blocks(tenant_id,project_id,environment_id,run_id,sequence,active) SELECT tenant_id,project_id,environment_id,id,(state->>'accepted_sequence')::bigint,false FROM runs")
    retention_schema()
    usage_schema()
    compatibility_schema()
    op.execute("UPDATE weave_schema_version SET version='0021_operations'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")


def retention_schema():
    op.execute("""DO $owner$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='weave_retention_owner') THEN
        IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolcreaterole)) THEN
          RAISE EXCEPTION 'Operations migration requires explicit role provisioning authority'; END IF;
        CREATE ROLE weave_retention_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        COMMENT ON ROLE weave_retention_owner IS 'weave operations narrow owner v1';
      ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='weave_retention_owner' AND
        (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolinherit))
        OR shobj_description('weave_retention_owner'::regrole,'pg_authid') IS DISTINCT FROM
          'weave operations narrow owner v1'
        OR EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid='weave_retention_owner'::regrole) THEN
        RAISE EXCEPTION 'Unrecognized or privileged operations owner; manual preflight required';
      END IF;
    END $owner$""")
    op.execute("ALTER TABLE debug_sessions ADD UNIQUE(tenant_id,project_id,id)")
    op.execute("""CREATE TABLE retention_holds(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,debug_id uuid NOT NULL,reference_id uuid NOT NULL,
      PRIMARY KEY(tenant_id,project_id,debug_id,reference_id),
      FOREIGN KEY(tenant_id,project_id,debug_id) REFERENCES debug_sessions(tenant_id,project_id,id))""")
    op.execute("""CREATE TABLE retention_plans(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,id uuid PRIMARY KEY,principal_id uuid NOT NULL REFERENCES principals(id),
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),expires_at timestamptz NOT NULL,
      payload jsonb NOT NULL CHECK(octet_length(payload::text)<=65536),
      candidates jsonb NOT NULL CHECK(jsonb_typeof(candidates)='array' AND jsonb_array_length(candidates)<=100
        AND octet_length(candidates::text)<=65536),
      UNIQUE(tenant_id,project_id,id),FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))""")
    op.execute("""CREATE TABLE retention_applications(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,plan_id uuid PRIMARY KEY,principal_id uuid NOT NULL REFERENCES principals(id),
      applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),manifest jsonb NOT NULL CHECK(octet_length(manifest::text)<=65536),
      FOREIGN KEY(tenant_id,project_id,plan_id) REFERENCES retention_plans(tenant_id,project_id,id))""")
    condition = ("tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND "
        "project_id=nullif(current_setting('weave.project_id',true),'')::uuid")
    for table in ('retention_holds','retention_plans','retention_applications'):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY operation_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT ON {table} TO weave_app,weave_retention_owner")
    op.execute("GRANT INSERT ON retention_holds TO weave_app")
    op.execute("GRANT INSERT ON retention_plans,retention_applications TO weave_retention_owner")
    op.execute("GRANT SELECT,DELETE ON debug_sessions TO weave_retention_owner")
    op.execute("GRANT UPDATE(capacity_reserved) ON debug_sessions TO weave_retention_owner")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
    op.execute("ALTER FUNCTION weave_debug_admission() OWNER TO weave_retention_owner")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
    op.execute("GRANT USAGE ON SCHEMA public TO weave_retention_owner")
    op.execute("GRANT EXECUTE ON FUNCTION weave_operation_fenced(uuid,uuid) TO weave_retention_owner")
    op.execute("CREATE INDEX retention_plan_expiry ON retention_plans(tenant_id,project_id,expires_at)")
    op.execute("""CREATE FUNCTION weave_hold_fence() RETURNS trigger LANGUAGE plpgsql
      SET search_path=pg_catalog,public AS $$ BEGIN
      IF NOT public.weave_operation_fenced(NEW.tenant_id,NEW.project_id) THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Outer operation admission required'; END IF;
      RETURN NEW; END $$""")
    op.execute("REVOKE ALL ON FUNCTION weave_hold_fence() FROM PUBLIC")
    op.execute("CREATE TRIGGER hold_fence BEFORE INSERT OR UPDATE ON retention_holds FOR EACH ROW EXECUTE FUNCTION weave_hold_fence()")
    op.execute("""CREATE FUNCTION weave_retention_plan(actor uuid,page_size integer,after_id uuid DEFAULT NULL) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE tenant uuid := nullif(current_setting('weave.tenant_id',true),'')::uuid;
        project uuid := nullif(current_setting('weave.project_id',true),'')::uuid;
        identifier uuid := gen_random_uuid(); items jsonb; created timestamptz := clock_timestamp();
        cutoff timestamptz := created-interval '24 hours'; expiry timestamptz := created+interval '15 minutes';
        result jsonb; more boolean; last_id uuid;
      BEGIN
        IF actor IS NULL OR page_size IS NULL OR page_size<1 OR page_size>100 OR
          NOT public.weave_operation_fenced(tenant,project) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Bounded scoped retention operation required'; END IF;
        IF (SELECT count(*) FROM (SELECT 1 FROM public.retention_plans WHERE tenant_id=tenant AND project_id=project
              LIMIT 10000) b)>=10000 OR
           (SELECT count(*) FROM (SELECT 1 FROM public.retention_plans WHERE tenant_id=tenant AND project_id=project
              AND expires_at>clock_timestamp() LIMIT 100) b)>=100 THEN
          RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Retention plan limit'; END IF;
        SELECT coalesce(jsonb_agg(jsonb_build_object('id',d.id,'revision',d.revision,
          'reason',CASE WHEN EXISTS(SELECT 1 FROM public.retention_holds h WHERE h.tenant_id=tenant
            AND h.project_id=project AND h.debug_id=d.id) THEN 'referenced' ELSE 'expired_debug' END)
          ORDER BY d.id),'[]'::jsonb) INTO items FROM
          (SELECT id,revision FROM public.debug_sessions WHERE tenant_id=tenant AND project_id=project
           AND expires_at<cutoff AND (after_id IS NULL OR id>after_id) ORDER BY id LIMIT page_size) d;
        last_id:=(items->-1->>'id')::uuid;
        SELECT EXISTS(SELECT 1 FROM public.debug_sessions WHERE tenant_id=tenant AND project_id=project
          AND expires_at<cutoff AND id>last_id) INTO more;
        result:=jsonb_build_object('id',identifier,'expires_at',expiry,'candidates',items,'policy','weave/operations-v1',
          'scope',jsonb_build_object('tenant_id',tenant,'project_id',project,'environment_id',NULL),
          'principal_id',actor,'created_at',created,'cutoff',cutoff,'complete',NOT more,
          'next_cursor',CASE WHEN more THEN last_id ELSE NULL END);
        INSERT INTO public.retention_plans(tenant_id,project_id,id,principal_id,created_at,expires_at,payload,candidates)
          VALUES(tenant,project,identifier,actor,created,expiry,result,items);
        RETURN result;
      END $$""")
    op.execute("""CREATE FUNCTION weave_retention_apply(identifier uuid,actor uuid) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE tenant uuid := nullif(current_setting('weave.tenant_id',true),'')::uuid;
        project uuid := nullif(current_setting('weave.project_id',true),'')::uuid;
        plan public.retention_plans%ROWTYPE; candidate jsonb; removed uuid; receipt jsonb;
        deleted jsonb := '[]'; blocked jsonb := '[]';
      BEGIN
        IF actor IS NULL OR NOT public.weave_operation_fenced(tenant,project) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Scoped admitted retention operation required'; END IF;
        SELECT manifest INTO receipt FROM public.retention_applications
          WHERE tenant_id=tenant AND project_id=project AND plan_id=identifier AND principal_id=actor;
        IF FOUND THEN RETURN receipt; END IF;
        SELECT * INTO plan FROM public.retention_plans WHERE tenant_id=tenant AND project_id=project AND id=identifier;
        IF NOT FOUND OR plan.principal_id<>actor OR plan.expires_at<=clock_timestamp() THEN
          RAISE EXCEPTION USING ERRCODE='WQ003',MESSAGE='Retention plan unavailable'; END IF;
        FOR candidate IN SELECT value FROM jsonb_array_elements(plan.candidates) LOOP
          removed := NULL;
          IF candidate->>'reason'='expired_debug' THEN
            DELETE FROM public.debug_sessions d WHERE d.tenant_id=tenant AND d.project_id=project
              AND d.id=(candidate->>'id')::uuid AND d.revision=(candidate->>'revision')::integer
              AND d.expires_at<(plan.payload->>'cutoff')::timestamptz
              AND NOT EXISTS(SELECT 1 FROM public.retention_holds h WHERE h.tenant_id=tenant
                AND h.project_id=project AND h.debug_id=d.id)
              RETURNING id INTO removed;
          END IF;
          IF removed IS NOT NULL THEN deleted:=deleted||jsonb_build_array(removed);
          ELSE blocked:=blocked||jsonb_build_array(candidate->>'id'); END IF;
        END LOOP;
        receipt:=jsonb_build_object('plan_id',identifier,'deleted',deleted,'blocked',blocked,'policy','weave/operations-v1');
        INSERT INTO public.retention_applications(tenant_id,project_id,plan_id,principal_id,manifest)
          VALUES(tenant,project,identifier,actor,receipt);
        RETURN receipt;
      END $$""")
    for function in ('weave_retention_plan(uuid,integer,uuid)','weave_retention_apply(uuid,uuid)'):
        op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
        op.execute(f"ALTER FUNCTION {function} OWNER TO weave_retention_owner")
        op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO weave_app")


# Entire-row JSON text counts metadata as well as payloads. This is logical data,
# not PostgreSQL page/index/WAL size. Tenant/global identity tables are separate.
QUOTA_TABLES = (
    'definition_versions','definition_sources','definition_retirements','draft_revisions','draft_retirements',
    'activation_revisions','mutation_idempotency','connection_revisions','connection_grants','connection_test_jobs',
    'connection_test_results','activation_connections','activation_connector_releases','worker_releases','worker_instances',
    'worker_connection_grants','runs','run_events','step_instances','task_intents','task_leases','completion_receipts',
    'run_deadlines','signal_receipts','wait_wakeups','trigger_routes','trigger_receipts','incidents',
    'incident_resolution_receipts','run_retry_links','run_policy_blocks','run_event_evidence','schedules',
    'schedule_revisions','schedule_occurrences','debug_sessions','connection_source_bindings','broker_routes',
    'broker_events','broker_receipts','broker_incidents','integration_events','event_subscriptions','event_deliveries',
    'delivery_attempts','provider_sources','provider_receipts','provider_intents','teams_references','teams_lifecycle_events',
    'teams_reference_commands','whatsapp_message_states','whatsapp_status_facts','whatsapp_status_observations',
    'runtime_capacity_blocks','retention_holds','retention_plans','retention_applications','access_audit',
)


def usage_schema():
    import json
    import sqlalchemy as sa
    from firefly_weave.contracts.operational_policy import OperationsPolicy

    policy=OperationsPolicy.model_validate(op.get_context().config.attributes.get('operations_policy',{}))
    op.execute("CREATE TABLE operation_policy(singleton boolean PRIMARY KEY CHECK(singleton),fingerprint text NOT NULL,limits jsonb NOT NULL)")
    op.get_bind().execute(sa.text("INSERT INTO operation_policy VALUES(true,:fingerprint,cast(:limits AS jsonb))"),
        {'fingerprint':policy.fingerprint,'limits':policy.model_dump_json()})
    op.execute("GRANT SELECT ON operation_policy TO weave_app,weave_retention_owner")
    op.execute("""CREATE TABLE operation_usage(tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      environment_id uuid NOT NULL,metric text NOT NULL CHECK(length(metric)<=80),value bigint NOT NULL CHECK(value>=0),
      PRIMARY KEY(tenant_id,project_id,environment_id,metric),
      FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))""")
    op.execute("ALTER TABLE operation_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE operation_usage FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY usage_application ON operation_usage TO weave_app USING(tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND project_id=nullif(current_setting('weave.project_id',true),'')::uuid)")
    op.execute("CREATE POLICY usage_owner ON operation_usage TO weave_retention_owner USING(true) WITH CHECK(true)")
    op.execute("GRANT SELECT ON operation_usage TO weave_app")
    op.execute("GRANT SELECT,INSERT,UPDATE ON operation_usage TO weave_retention_owner")
    large_row_accounting()
    control_schema()
    op.execute("""CREATE FUNCTION weave_row_metrics(relation text,item jsonb) RETURNS jsonb
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,public AS $$
      DECLARE result jsonb := '{}'::jsonb; enabled boolean;
      BEGIN
        IF item IS NULL OR item='{}'::jsonb THEN RETURN result; END IF;
        result:=jsonb_build_object('ordinary_bytes',coalesce((item->>'_weave_bytes')::bigint,octet_length(item::text)));
        CASE relation
          WHEN 'runs' THEN result:=result||jsonb_build_object('runs_retained',1,'runs_active',
            CASE WHEN item->'state'->>'status' IN ('succeeded','failed','cancelled','timed_out') THEN 0 ELSE 1 END);
          WHEN 'task_intents' THEN
            enabled:=item->>'status' IN ('ready','leased');
            result:=result||jsonb_build_object('tasks_active',CASE WHEN enabled THEN 1 ELSE 0 END,
              'task_input_bytes',CASE WHEN enabled THEN octet_length((item->'payload'->'input')::text) ELSE 0 END);
          WHEN 'run_deadlines' THEN result:=result||jsonb_build_object('waits_active',CASE WHEN (item->>'consumed')::boolean THEN 0 ELSE 1 END);
          WHEN 'runtime_capacity_blocks' THEN result:=result||jsonb_build_object('ordinary_bytes',greatest(1024,octet_length(item::text)));
          WHEN 'debug_sessions' THEN result:=result||jsonb_build_object('debug_rows',1,'ordinary_bytes',
            greatest(octet_length(item::text),CASE WHEN coalesce((item->>'capacity_reserved')::boolean,false) THEN 67108864 ELSE 0 END));
          WHEN 'provider_sources' THEN result:=result||jsonb_build_object('sources_retained',1,'sources_enabled',CASE WHEN (item->>'disabled')::boolean THEN 0 ELSE 1 END);
          WHEN 'broker_routes' THEN result:=result||jsonb_build_object('sources_enabled',CASE WHEN (item->>'disabled')::boolean THEN 0 ELSE 1 END);
          WHEN 'connection_source_bindings' THEN result:=result||jsonb_build_object('bindings_retained',1);
          WHEN 'provider_intents' THEN result:=result||jsonb_build_object('provider_pending',CASE WHEN (item->>'pending')::boolean THEN 1 ELSE 0 END);
          WHEN 'provider_receipts' THEN result:=result||jsonb_build_object('provider_receipts',1,'provider_pending_bytes',CASE WHEN item->>'state'='pending' THEN octet_length((item->'mapped')::text) ELSE 0 END);
          WHEN 'event_deliveries' THEN result:=result||jsonb_build_object('outbox_pending',CASE WHEN item->>'status' IN ('pending','retry','leased') THEN 1 ELSE 0 END);
          ELSE
            IF relation IN ('teams_references','teams_lifecycle_events','teams_reference_commands','whatsapp_message_states','whatsapp_status_facts','whatsapp_status_observations') THEN
              result:=result||jsonb_build_object(relation,1); END IF;
        END CASE;
        RETURN result;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION weave_row_metrics(text,jsonb) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_row_metrics(text,jsonb) TO weave_retention_owner")
    op.execute("""CREATE FUNCTION weave_usage_charge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $$
      DECLARE old_row jsonb; new_row jsonb; item jsonb;
        tenant uuid; project uuid; environment uuid; dimension uuid; charges jsonb; prior jsonb;
        field text; amount numeric; measured bigint; ceiling bigint; limits jsonb; ordinary_delta bigint;
        zero uuid := '00000000-0000-0000-0000-000000000000';
      BEGIN
        IF TG_OP='INSERT' THEN old_row:='{}'::jsonb;
        ELSIF TG_TABLE_NAME='runs' THEN old_row:=public.weave_runs_account(OLD);
        ELSIF TG_TABLE_NAME='run_events' THEN old_row:=public.weave_run_events_account(OLD);
        ELSE old_row:=to_jsonb(OLD); END IF;
        IF TG_OP='DELETE' THEN new_row:='{}'::jsonb;
        ELSIF TG_TABLE_NAME='runs' THEN new_row:=public.weave_runs_account(NEW);
        ELSIF TG_TABLE_NAME='run_events' THEN new_row:=public.weave_run_events_account(NEW);
        ELSE new_row:=to_jsonb(NEW); END IF;
        item:=CASE WHEN TG_OP='DELETE' THEN old_row ELSE new_row END;
        tenant:=coalesce(item->>'tenant_id',item->'event'->'scope'->>'tenant_id')::uuid;
        project:=coalesce(item->>'project_id',item->'event'->'scope'->>'project_id')::uuid;
        environment:=coalesce((item->>'environment_id')::uuid,zero);
        IF tenant IS NULL OR project IS NULL THEN RETURN coalesce(NEW,OLD); END IF;
        IF NOT public.weave_operation_fenced(tenant,project) AND
          NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Outer operation admission required'; END IF;
        IF TG_OP='UPDATE' AND (old_row->>'tenant_id',old_row->>'project_id',old_row->>'environment_id')
           IS DISTINCT FROM (new_row->>'tenant_id',new_row->>'project_id',new_row->>'environment_id') THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Immutable allocation scope required'; END IF;
        SELECT p.limits INTO limits FROM public.operation_policy p WHERE singleton;
        ordinary_delta:=public.weave_allocation_delta(TG_TABLE_NAME,old_row,new_row,TG_ARGV);
        IF TG_TABLE_NAME='runtime_capacity_blocks' THEN
          ordinary_delta:=ordinary_delta
            +CASE WHEN new_row<>'{}'::jsonb THEN greatest(0,1024-octet_length(new_row::text)) ELSE 0 END
            -CASE WHEN old_row<>'{}'::jsonb THEN greatest(0,1024-octet_length(old_row::text)) ELSE 0 END;
        END IF;
        IF TG_TABLE_NAME='debug_sessions' THEN
          ordinary_delta:=ordinary_delta
            +CASE WHEN coalesce((new_row->>'capacity_reserved')::boolean,false) THEN greatest(0,67108864-octet_length(new_row::text)) ELSE 0 END
            -CASE WHEN coalesce((old_row->>'capacity_reserved')::boolean,false) THEN greatest(0,67108864-octet_length(old_row::text)) ELSE 0 END;
        END IF;
        prior:=public.weave_row_metrics(TG_TABLE_NAME,old_row);
        charges:=public.weave_row_metrics(TG_TABLE_NAME,new_row);
        FOR field IN SELECT jsonb_object_keys(prior||charges) LOOP
          amount:=coalesce((charges->>field)::numeric,0)-coalesce((prior->>field)::numeric,0);
          IF field='ordinary_bytes' THEN amount:=ordinary_delta; END IF;
          IF amount=0 THEN CONTINUE; END IF;
          IF (CASE WHEN field='outbox_pending' THEN EXISTS(SELECT 1 FROM public.operation_control_deliveries WHERE id=(item->>'id')::uuid) ELSE false END) THEN
            PERFORM public.weave_counter_delta(tenant,project,'control_outbox_pending',amount,TG_OP<>'INSERT');
            CONTINUE;
          END IF;
          dimension:=CASE WHEN field IN ('ordinary_bytes','runs_retained','debug_rows') THEN zero ELSE environment END;
          IF abs(amount)>9223372036854775807 THEN RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Allocation overflow'; END IF;
          SELECT coalesce(value,0) INTO measured FROM public.operation_usage
            WHERE tenant_id=tenant AND project_id=project AND environment_id=dimension AND metric=field;
          IF coalesce(measured,0)::numeric+amount NOT BETWEEN 0 AND 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Allocation arithmetic limit'; END IF;
          IF amount<0 THEN
            UPDATE public.operation_usage SET value=(value::numeric+amount)::bigint
              WHERE tenant_id=tenant AND project_id=project AND environment_id=dimension AND metric=field
              RETURNING value INTO measured;
            IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Allocation counter mismatch'; END IF;
          ELSE
            INSERT INTO public.operation_usage(tenant_id,project_id,environment_id,metric,value)
              VALUES(tenant,project,dimension,field,amount::bigint)
              ON CONFLICT(tenant_id,project_id,environment_id,metric) DO UPDATE
                SET value=(operation_usage.value::numeric+amount)::bigint RETURNING value INTO measured;
          END IF;
          ceiling:=(limits->>field)::bigint;
          IF amount>0 AND ceiling IS NOT NULL AND measured>ceiling THEN
            RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Operation allocation limit'; END IF;
        END LOOP;
        PERFORM public.weave_reservation_touch(TG_TABLE_NAME,item);
        RETURN coalesce(NEW,OLD);
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
    op.execute("ALTER FUNCTION weave_usage_charge() OWNER TO weave_retention_owner")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
    op.execute("REVOKE ALL ON FUNCTION weave_usage_charge() FROM PUBLIC")
    # Inventory actual existing facts without assigning invented creation times.
    for table in QUOTA_TABLES:
        projection=f"public.weave_{table}_account(t)" if table in ('runs','run_events') else 'to_jsonb(t)'
        op.execute(f"""INSERT INTO operation_usage(tenant_id,project_id,environment_id,metric,value)
          SELECT tenant,project,CASE WHEN metric IN ('ordinary_bytes','runs_retained','debug_rows')
            THEN '00000000-0000-0000-0000-000000000000'::uuid ELSE environment END,metric,sum(amount::bigint)
          FROM (SELECT coalesce(item->>'tenant_id',item->'event'->'scope'->>'tenant_id')::uuid AS tenant,
            coalesce(item->>'project_id',item->'event'->'scope'->>'project_id')::uuid AS project,
            coalesce((item->>'environment_id')::uuid,'00000000-0000-0000-0000-000000000000'::uuid) AS environment,
            m.key AS metric,m.value AS amount
            FROM (SELECT {projection} AS item FROM public.{table} t) r,
              LATERAL jsonb_each_text(public.weave_row_metrics('{table}',r.item)) m) facts
          WHERE tenant IS NOT NULL AND project IS NOT NULL
          GROUP BY tenant,project,CASE WHEN metric IN ('ordinary_bytes','runs_retained','debug_rows')
            THEN '00000000-0000-0000-0000-000000000000'::uuid ELSE environment END,metric
          ON CONFLICT(tenant_id,project_id,environment_id,metric) DO UPDATE
            SET value=operation_usage.value+excluded.value""")
        inspector=sa.inspect(op.get_bind())
        keys=inspector.get_pk_constraint(table)["constrained_columns"]
        if not keys:
            keys=inspector.get_unique_constraints(table)[0]["column_names"]
        arguments=",".join("'"+key+"'" for key in keys)
        op.execute(f"CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION weave_usage_charge({arguments})")
    for kind,table in (('run','runs'),('provider','provider_sources'),('binding','connection_source_bindings'),
        ('teams','teams_references'),('webhook','trigger_routes'),('broker','broker_routes'),('subscription','event_subscriptions')):
        op.execute(f"SELECT weave_reserve_refresh('{kind}',id,false) FROM {table}")


def large_row_accounting():
    """Measure separate JSONB columns without constructing an oversized combined object."""
    import sqlalchemy as sa

    bind=op.get_bind()
    inspector=sa.inspect(bind)
    quote=bind.dialect.identifier_preparer.quote
    for table in ('runs','run_events'):
        columns=[column['name'] for column in inspector.get_columns(table)]
        literals=["'"+name.replace("'","''")+"'" for name in columns]
        sizes=[f"coalesce(octet_length(to_jsonb((item).{quote(name)})::text)::bigint,4)"
               f"+octet_length(to_jsonb({literal}::text)::text)+4"
               for name,literal in zip(columns,literals,strict=True)]
        op.execute(f"""CREATE FUNCTION weave_{table}_bytes(item public.{table}) RETURNS bigint
          LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public AS $$ BEGIN
          IF (SELECT array_agg(attname::text ORDER BY attnum) FROM pg_attribute
            WHERE attrelid='public.{table}'::regclass AND attnum>0 AND NOT attisdropped)
            IS DISTINCT FROM ARRAY[{','.join(literals)}]::text[] THEN
            RAISE EXCEPTION USING ERRCODE='WQ003',MESSAGE='Accounting schema changed'; END IF;
          RETURN {'+'.join(sizes)}; END $$""")
        identity=['id','tenant_id','project_id','environment_id']+(['run_id'] if table=='run_events' else [])
        fields=','.join(f"'{name}',(item).{name}" for name in identity)
        if table=='runs':
            fields+=""",'state',jsonb_build_object('status',(item).state->'status','accepted_sequence',(item).state->'accepted_sequence'),
              '_weave_state_bytes',octet_length((item).state::text),
              '_weave_activation_bytes',octet_length((item).activation::text),
              '_weave_branches',(SELECT count(*) FROM jsonb_each(coalesce((item).state->'branches','{}'::jsonb))),
              '_weave_joins',(SELECT count(*) FROM jsonb_each(coalesce((item).state->'joins','{}'::jsonb)))"""
        op.execute(f"""CREATE FUNCTION weave_{table}_account(item public.{table}) RETURNS jsonb
          LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
          SELECT jsonb_build_object({fields},'_weave_bytes',public.weave_{table}_bytes(item)) $$""")
        for suffix in ('bytes','account'):
            function=f'weave_{table}_{suffix}(public.{table})'
            op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO weave_retention_owner")
        if table=='runs':
            op.execute("GRANT EXECUTE ON FUNCTION weave_runs_bytes(public.runs) TO weave_app")


def control_schema():
    """Private allocation provenance and prepaid, target-bound safety controls."""
    op.execute("""CREATE TABLE operation_reservations(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,kind text NOT NULL,resource_id uuid NOT NULL,
      amount bigint NOT NULL CHECK(amount>=0),outbox_slots integer NOT NULL CHECK(outbox_slots BETWEEN 0 AND 64),PRIMARY KEY(kind,resource_id),
      FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))""")
    op.execute("""CREATE TABLE operation_allocations(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,relation text NOT NULL,row_key text NOT NULL,
      control_bytes bigint NOT NULL CHECK(control_bytes>=0),PRIMARY KEY(relation,row_key),
      FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))""")
    op.execute("""CREATE TABLE operation_control_deliveries(
      tenant_id uuid NOT NULL,project_id uuid NOT NULL,id uuid PRIMARY KEY,
      FOREIGN KEY(id) REFERENCES event_deliveries(id))""")
    op.execute("""CREATE TABLE operation_control_context(
      transaction_id bigint NOT NULL,tenant_id uuid NOT NULL,project_id uuid NOT NULL,
      kind text NOT NULL,resource_id uuid NOT NULL,PRIMARY KEY(transaction_id,kind,resource_id),
      FOREIGN KEY(kind,resource_id) REFERENCES operation_reservations(kind,resource_id))""")
    for table in ('operation_reservations','operation_allocations','operation_control_context','operation_control_deliveries'):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY operations_owner ON {table} TO weave_retention_owner USING(true) WITH CHECK(true)")
        op.execute(f"CREATE POLICY operations_reader ON {table} TO weave_app USING(tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND project_id=nullif(current_setting('weave.project_id',true),'')::uuid)")
        op.execute(f"GRANT SELECT ON {table} TO weave_app")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO weave_retention_owner")
    reads=('runs','task_intents','task_leases','incidents','run_deadlines','integration_events',
        'provider_sources','connection_source_bindings','teams_references','trigger_routes','broker_routes','event_subscriptions')
    for table in reads:
        op.execute(f"GRANT SELECT ON {table} TO weave_retention_owner")
        op.execute(f"CREATE POLICY operations_inventory ON {table} FOR SELECT TO weave_retention_owner USING(true)")
    op.execute("GRANT SELECT,INSERT ON runtime_capacity_blocks TO weave_retention_owner")
    op.execute("CREATE POLICY operations_capacity ON runtime_capacity_blocks TO weave_retention_owner USING(true) WITH CHECK(true)")
    op.execute("""CREATE FUNCTION weave_counter_delta(tenant uuid,project uuid,metric_name text,delta numeric,strict boolean)
      RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE measured numeric; ceiling bigint; BEGIN
      SELECT coalesce(sum(value),0)+delta INTO measured FROM public.operation_usage
        WHERE tenant_id=tenant AND project_id=project AND metric=metric_name;
      IF measured<0 OR measured>9223372036854775807 THEN
        RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Allocation arithmetic limit'; END IF;
      INSERT INTO public.operation_usage VALUES(tenant,project,'00000000-0000-0000-0000-000000000000',metric_name,measured::bigint)
        ON CONFLICT(tenant_id,project_id,environment_id,metric) DO UPDATE SET value=excluded.value;
      IF strict AND delta>0 THEN
        IF metric_name='ordinary_bytes' THEN
          SELECT (limits->>'ordinary_bytes')::bigint INTO ceiling FROM public.operation_policy WHERE singleton;
        ELSIF metric_name IN ('control_outbox_pending','control_outbox_reserved') THEN
          ceiling:=64000;
          SELECT coalesce(sum(value::numeric),0) INTO measured FROM public.operation_usage
            WHERE tenant_id=tenant AND project_id=project AND metric IN ('control_outbox_pending','control_outbox_reserved');
        ELSE
          SELECT (limits->>'control_bytes')::bigint INTO ceiling FROM public.operation_policy WHERE singleton;
          SELECT coalesce(sum(value::numeric),0) INTO measured FROM public.operation_usage
            WHERE tenant_id=tenant AND project_id=project AND metric IN ('control_bytes','control_reserved');
        END IF;
        IF measured>ceiling THEN
          RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Control allocation limit'; END IF;
      END IF;
      END $$""")
    op.execute("""CREATE FUNCTION weave_resource(kind_name text,identifier uuid) RETURNS jsonb
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ DECLARE result jsonb; BEGIN
      CASE kind_name
        WHEN 'run' THEN SELECT public.weave_runs_account(r) INTO result FROM public.runs r WHERE id=identifier;
        WHEN 'provider' THEN SELECT to_jsonb(r) INTO result FROM public.provider_sources r WHERE id=identifier;
        WHEN 'binding' THEN SELECT to_jsonb(r) INTO result FROM public.connection_source_bindings r WHERE id=identifier;
        WHEN 'teams' THEN SELECT to_jsonb(r) INTO result FROM public.teams_references r WHERE id=identifier;
        WHEN 'webhook' THEN SELECT to_jsonb(r) INTO result FROM public.trigger_routes r WHERE id=identifier;
        WHEN 'broker' THEN SELECT to_jsonb(r) INTO result FROM public.broker_routes r WHERE id=identifier;
        WHEN 'subscription' THEN SELECT to_jsonb(r) INTO result FROM public.event_subscriptions r WHERE id=identifier;
        ELSE RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Control target unavailable';
      END CASE; RETURN result; END $$""")
    op.execute("""CREATE FUNCTION weave_resource_active(kind_name text,item jsonb) RETURNS boolean
      LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$ SELECT CASE kind_name
        WHEN 'run' THEN item->'state'->>'status' NOT IN ('succeeded','failed','cancelled','timed_out')
        WHEN 'binding' THEN NOT (item->>'revoked')::boolean
        WHEN 'teams' THEN item->>'state'='active'
        WHEN 'subscription' THEN (item->>'active')::boolean
        ELSE NOT (item->>'disabled')::boolean END $$""")
    op.execute("""CREATE FUNCTION weave_reserve_refresh(kind_name text,identifier uuid,strict boolean DEFAULT true)
      RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE item jsonb; tenant uuid; project uuid; previous bigint; needed numeric:=0; affected bigint:=0; previous_slots integer; needed_slots integer:=0;
      BEGIN
      item:=public.weave_resource(kind_name,identifier);
      IF item IS NULL THEN RETURN; END IF;
      tenant:=(item->>'tenant_id')::uuid; project:=(item->>'project_id')::uuid;
      IF EXISTS(SELECT 1 FROM public.operation_control_context WHERE transaction_id=txid_current()
        AND kind=kind_name AND resource_id=identifier) THEN RETURN; END IF;
      IF public.weave_resource_active(kind_name,item) THEN
        IF kind_name='run' THEN
          needed_slots:=64;
          SELECT (SELECT count(*) FROM public.task_intents WHERE run_id=identifier)
            +(SELECT count(*) FROM public.task_leases l JOIN public.task_intents t ON t.id=l.task_id WHERE t.run_id=identifier AND l.status='active')
            +(SELECT count(*) FROM public.incidents WHERE run_id=identifier AND status='active')
            +(SELECT count(*) FROM public.run_deadlines WHERE run_id=identifier AND NOT consumed) INTO affected;
          needed:=2*((item->>'_weave_state_bytes')::numeric+32768
            +3*((item->>'_weave_branches')::numeric+(item->>'_weave_joins')::numeric))
            +(item->>'_weave_activation_bytes')::numeric+16777216+64*2048+65536+576*affected;
        ELSE needed:=65536; END IF;
      END IF;
      SELECT amount,outbox_slots INTO previous,previous_slots FROM public.operation_reservations WHERE kind=kind_name AND resource_id=identifier;
      IF NOT FOUND THEN
        previous:=0; previous_slots:=0;
        PERFORM public.weave_counter_delta(tenant,project,'ordinary_bytes',512,strict);
      END IF;
      IF needed>9223372036854775807 THEN RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Reservation arithmetic limit'; END IF;
      PERFORM public.weave_counter_delta(tenant,project,'control_reserved',needed-previous,strict);
      PERFORM public.weave_counter_delta(tenant,project,'control_outbox_reserved',needed_slots-previous_slots,strict);
      INSERT INTO public.operation_reservations VALUES(tenant,project,kind_name,identifier,needed::bigint,needed_slots)
        ON CONFLICT(kind,resource_id) DO UPDATE SET amount=excluded.amount,outbox_slots=excluded.outbox_slots;
      END $$""")
    op.execute("""CREATE FUNCTION weave_control_begin(kind_name text,identifier uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE item jsonb; tenant uuid; project uuid; BEGIN
      item:=public.weave_resource(kind_name,identifier);
      tenant:=nullif(current_setting('weave.tenant_id',true),'')::uuid;
      project:=nullif(current_setting('weave.project_id',true),'')::uuid;
      IF item IS NULL OR (item->>'tenant_id')::uuid IS DISTINCT FROM tenant OR
        (item->>'project_id')::uuid IS DISTINCT FROM project OR NOT public.weave_operation_fenced(tenant,project)
        OR NOT public.weave_resource_active(kind_name,item) OR NOT EXISTS(
          SELECT 1 FROM public.operation_reservations WHERE kind=kind_name AND resource_id=identifier AND amount>0) THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Reserved control unavailable'; END IF;
      IF (SELECT count(*) FROM public.operation_control_context WHERE transaction_id=txid_current())>=100 THEN
        RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Control transaction limit'; END IF;
      INSERT INTO public.operation_control_context VALUES(txid_current(),tenant,project,kind_name,identifier)
        ON CONFLICT DO NOTHING;
      END $$""")
    op.execute("""CREATE FUNCTION weave_control_finish() RETURNS void LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $$ DECLARE c record; BEGIN
      FOR c IN SELECT * FROM public.operation_control_context WHERE transaction_id=txid_current() LOOP
        IF NOT public.weave_operation_fenced(c.tenant_id,c.project_id) OR
          public.weave_resource_active(c.kind,public.weave_resource(c.kind,c.resource_id)) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Control did not reach safe state'; END IF;
        DELETE FROM public.operation_control_context WHERE transaction_id=txid_current()
          AND kind=c.kind AND resource_id=c.resource_id;
        PERFORM public.weave_reserve_refresh(c.kind,c.resource_id);
      END LOOP; END $$""")
    op.execute("""CREATE FUNCTION weave_control_closed() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $$ BEGIN
      IF EXISTS(SELECT 1 FROM public.operation_control_context WHERE transaction_id=NEW.transaction_id) THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Unfinished reserved control'; END IF;
      RETURN NULL; END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER control_closed AFTER INSERT ON operation_control_context DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION weave_control_closed()")
    op.execute("""CREATE FUNCTION weave_control_target(relation_name text,item jsonb) RETURNS uuid
      LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public AS $$ DECLARE identifier uuid; BEGIN
      IF relation_name='runs' THEN RETURN (item->>'id')::uuid; END IF;
      IF relation_name IN ('run_events','run_event_evidence','task_intents','incidents','run_deadlines','runtime_capacity_blocks')
        THEN RETURN (item->>'run_id')::uuid; END IF;
      IF relation_name='task_leases' THEN SELECT run_id INTO identifier FROM public.task_intents WHERE id=(item->>'task_id')::uuid;
      ELSIF relation_name='wait_wakeups' THEN SELECT run_id INTO identifier FROM public.run_deadlines WHERE id=(item->>'wait_id')::uuid;
      ELSIF relation_name='integration_events' THEN identifier:=(item->'payload'->>'resource_id')::uuid;
      ELSIF relation_name='event_deliveries' THEN SELECT (payload->>'resource_id')::uuid INTO identifier FROM public.integration_events WHERE id=(item->>'event_id')::uuid;
      END IF; RETURN identifier; END $$""")
    op.execute("""CREATE FUNCTION weave_allocation_delta(relation_name text,old_row jsonb,new_row jsonb,keys text[])
      RETURNS bigint LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
      DECLARE item jsonb:=CASE WHEN new_row='{}'::jsonb THEN old_row ELSE new_row END;
        tenant uuid; project uuid; identity_json jsonb; identity_key text; previous bigint:=0; retained bigint:=0;
        old_size bigint:=CASE WHEN old_row='{}'::jsonb THEN 0 ELSE coalesce((old_row->>'_weave_bytes')::bigint,octet_length(old_row::text)) END;
        new_size bigint:=CASE WHEN new_row='{}'::jsonb THEN 0 ELSE coalesce((new_row->>'_weave_bytes')::bigint,octet_length(new_row::text)) END;
        delta bigint; overhead bigint:=0; target uuid; c record; spending boolean:=false;
      BEGIN
      tenant:=coalesce(item->>'tenant_id',item->'event'->'scope'->>'tenant_id')::uuid;
      project:=coalesce(item->>'project_id',item->'event'->'scope'->>'project_id')::uuid;
      SELECT jsonb_object_agg(k,item->k) INTO identity_json FROM unnest(keys) k;
      IF identity_json IS NULL THEN RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Allocation identity missing'; END IF;
      identity_key:=encode(sha256(convert_to(identity_json::text,'UTF8')),'hex');
      SELECT control_bytes INTO previous FROM public.operation_allocations WHERE relation=relation_name AND row_key=identity_key;
      previous:=coalesce(previous,0); retained:=least(previous,new_size);
      target:=public.weave_control_target(relation_name,item);
      FOR c IN SELECT * FROM public.operation_control_context WHERE transaction_id=txid_current()
        AND tenant_id=tenant AND project_id=project LOOP
        spending:=(c.kind='run' AND target=c.resource_id) OR
          (CASE WHEN relation_name=CASE c.kind WHEN 'provider' THEN 'provider_sources' WHEN 'binding' THEN 'connection_source_bindings'
            WHEN 'teams' THEN 'teams_references' WHEN 'webhook' THEN 'trigger_routes' WHEN 'broker' THEN 'broker_routes'
            WHEN 'subscription' THEN 'event_subscriptions' END THEN (item->>'id')::uuid=c.resource_id ELSE false END) OR
          (relation_name='access_audit' AND item->>'target'=c.resource_id::text AND item->>'action' IN
            ('run.cancel','provider.source.disable','connection.source.revoke','teams.reference.revoke','trigger.disable','broker.disable','subscription.disable'));
        EXIT WHEN spending;
      END LOOP;
      IF spending AND new_size>old_size THEN
        delta:=new_size-old_size;
        IF previous=0 THEN overhead:=512; END IF;
        UPDATE public.operation_reservations SET amount=amount-delta-overhead
          WHERE kind=c.kind AND resource_id=c.resource_id AND amount>=delta+overhead;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Reserved control bound exceeded'; END IF;
        PERFORM public.weave_counter_delta(tenant,project,'control_reserved',-delta-overhead,true);
        retained:=retained+delta;
        IF relation_name='event_deliveries' AND old_row='{}'::jsonb THEN
          UPDATE public.operation_reservations SET outbox_slots=outbox_slots-1
            WHERE kind=c.kind AND resource_id=c.resource_id AND outbox_slots>0;
          IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Reserved control fanout exceeded'; END IF;
          PERFORM public.weave_counter_delta(tenant,project,'control_outbox_reserved',-1,true);
          INSERT INTO public.operation_control_deliveries VALUES(tenant,project,(item->>'id')::uuid);
        END IF;
      END IF;
      IF retained>0 THEN
        INSERT INTO public.operation_allocations VALUES(tenant,project,relation_name,identity_key,retained)
          ON CONFLICT(relation,row_key) DO UPDATE SET control_bytes=excluded.control_bytes;
      ELSIF previous>0 THEN
        DELETE FROM public.operation_allocations WHERE relation=relation_name AND row_key=identity_key;
        overhead:=-512;
      END IF;
      IF retained<>previous OR overhead<>0 THEN
        PERFORM public.weave_counter_delta(tenant,project,'control_bytes',retained-previous+overhead,false);
      END IF;
      RETURN (new_size-retained)-(old_size-previous);
      END $$""")
    op.execute("""CREATE FUNCTION weave_reservation_touch(relation_name text,item jsonb) RETURNS void
      LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ DECLARE kind_name text; identifier uuid; BEGIN
      kind_name:=CASE relation_name WHEN 'runs' THEN 'run' WHEN 'provider_sources' THEN 'provider'
        WHEN 'connection_source_bindings' THEN 'binding' WHEN 'teams_references' THEN 'teams'
        WHEN 'trigger_routes' THEN 'webhook' WHEN 'broker_routes' THEN 'broker' WHEN 'event_subscriptions' THEN 'subscription' END;
      IF kind_name IS NOT NULL THEN identifier:=(item->>'id')::uuid;
      ELSIF relation_name IN ('task_intents','task_leases','incidents','run_deadlines') THEN
        identifier:=public.weave_control_target(relation_name,item); kind_name:='run';
      END IF;
      IF identifier IS NOT NULL THEN PERFORM public.weave_reserve_refresh(kind_name,identifier); END IF;
      IF relation_name='runs' THEN
        INSERT INTO public.runtime_capacity_blocks(tenant_id,project_id,environment_id,run_id,sequence,active)
          VALUES((item->>'tenant_id')::uuid,(item->>'project_id')::uuid,(item->>'environment_id')::uuid,
            identifier,(item->'state'->>'accepted_sequence')::bigint,false) ON CONFLICT DO NOTHING;
      END IF;
      END $$""")
    functions=(
        'weave_counter_delta(uuid,uuid,text,numeric,boolean)','weave_resource(text,uuid)',
        'weave_resource_active(text,jsonb)','weave_reserve_refresh(text,uuid,boolean)',
        'weave_control_begin(text,uuid)','weave_control_finish()','weave_control_closed()',
        'weave_control_target(text,jsonb)','weave_allocation_delta(text,jsonb,jsonb,text[])',
        'weave_reservation_touch(text,jsonb)',
    )
    for function in functions:
        op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
        op.execute(f"ALTER FUNCTION {function} OWNER TO weave_retention_owner")
        op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
    for function in ('weave_control_begin(text,uuid)','weave_control_finish()'):
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO weave_app")


def compatibility_schema():
    op.execute("""CREATE FUNCTION weave_compatibility_counts() RETURNS jsonb LANGUAGE sql STABLE
      SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT jsonb_build_object('tasks.claim',coalesce(sum(value) FILTER(WHERE metric='tasks_active'),0),
        'provider',coalesce(sum(value) FILTER(WHERE metric='provider_pending'),0),
        'outbox',coalesce(sum(value) FILTER(WHERE metric IN ('outbox_pending','control_outbox_pending')),0),
        'runs.start',coalesce(sum(value) FILTER(WHERE metric='runs_active'),0)) FROM public.operation_usage $$""")
    op.execute("""CREATE FUNCTION weave_compatibility_policy() RETURNS text LANGUAGE sql STABLE
      SECURITY DEFINER SET search_path=pg_catalog,public AS $$ SELECT fingerprint FROM public.operation_policy WHERE singleton $$""")
    for table in ('tenants','activation_revisions','definition_versions','worker_releases','worker_instances',
                  'activation_connections','activation_connector_releases','connection_revisions'):
        op.execute(f"GRANT SELECT ON {table} TO weave_retention_owner")
        op.execute(f"CREATE POLICY operations_compatibility ON {table} FOR SELECT TO weave_retention_owner USING(true)")
    op.execute("""CREATE FUNCTION weave_activation_facts(t uuid,p uuid,e uuid,value jsonb,compiled jsonb)
      RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
      SELECT EXISTS(SELECT 1 FROM public.activation_revisions a JOIN public.definition_versions v
        ON v.tenant_id=a.tenant_id AND v.project_id=a.project_id AND v.id=a.version_id
        WHERE a.tenant_id=t AND a.project_id=p AND a.environment_id=e AND a.id::text=value->>'id'
        AND a.payload=value AND a.version_id::text=value->'request'->>'version_id'
        AND a.revision::text=value->>'revision' AND a.name=value->>'name'
        AND value->'request'->'scope'=jsonb_build_object('tenant_id',t,'project_id',p,'environment_id',e)
        AND v.kind='Workflow' AND v.artifact=compiled AND v.digest=value->'request'->>'artifact_digest'
        AND coalesce(value->'request'->'connection_revision_ids','{}'::jsonb)=coalesce((
          SELECT jsonb_object_agg(c.slot,c.revision_id::text) FROM public.activation_connections c
          WHERE c.tenant_id=t AND c.project_id=p AND c.environment_id=e AND c.activation_id=a.id),'{}'::jsonb)
        AND coalesce(value->'request'->'connector_release_ids','{}'::jsonb)=coalesce((
          SELECT jsonb_object_agg(c.connector_version_id::text,c.release_id::text)
          FROM public.activation_connector_releases c WHERE c.tenant_id=t AND c.project_id=p
          AND c.environment_id=e AND c.activation_id=a.id),'{}'::jsonb)
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(coalesce(value->'connector_execution_pins','[]'::jsonb)) pin
          WHERE NOT EXISTS(SELECT 1 FROM public.definition_versions cv WHERE cv.tenant_id=t AND cv.project_id=p
            AND cv.id::text=pin->>'connector_version_id' AND cv.kind='Connector'
            AND cv.definition_digest=pin->>'connector_digest'))
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(compiled->'executable'->'dependencies') d
          WHERE d->>'kind' IN ('Action','Connector') AND NOT EXISTS(
            SELECT 1 FROM public.definition_versions dv WHERE dv.tenant_id=t AND dv.project_id=p
              AND dv.kind=d->>'kind' AND dv.name||'@'||dv.version=d->>'reference'
              AND dv.definition_digest=d->>'digest' AND dv.document=d->'document')))
      $$""")
    op.execute("""CREATE FUNCTION weave_compatibility_tenants(after_id uuid,page_size integer) RETURNS SETOF uuid
      LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN
      IF page_size IS NULL OR page_size<1 OR page_size>16 THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Bounded compatibility catalog required'; END IF;
      RETURN QUERY SELECT id FROM public.tenants WHERE after_id IS NULL OR id>after_id ORDER BY id LIMIT page_size;
      END $$""")
    op.execute("""CREATE FUNCTION weave_compatibility_page(tenant uuid,after_kind text,after_id uuid,page_size integer)
      RETURNS TABLE(kind text,id uuid,project_id uuid,environment_id uuid,payload jsonb,logical_bytes bigint,facts_valid boolean)
      LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN
      IF tenant IS NULL OR page_size IS NULL OR page_size<1 OR page_size>25 OR
        (after_kind IS NULL)<>(after_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Bounded compatibility page required'; END IF;
      RETURN QUERY WITH requirements AS (
        SELECT 'run'::text kind,r.id,r.project_id,r.environment_id,
          jsonb_build_object('artifact',r.artifact,'activation',r.activation,'releases',coalesce((SELECT jsonb_agg(w.payload ORDER BY w.id)
            FROM public.worker_releases w WHERE w.tenant_id=r.tenant_id AND w.project_id=r.project_id
              AND w.environment_id=r.environment_id AND w.id::text IN (
                SELECT value FROM jsonb_each_text(coalesce(r.activation->'request'->'worker_release_ids','{}'::jsonb))
                UNION SELECT value FROM jsonb_each_text(coalesce(r.activation->'request'->'connector_release_ids','{}'::jsonb))
              )), '[]'::jsonb),
            'state',jsonb_build_object('status',r.state->'status','admission_policy',r.state->'admission_policy','unavailable',coalesce(r.state->'unavailable','false'::jsonb)),
            'state_bytes',octet_length(r.state::text)) payload,
          public.weave_activation_facts(r.tenant_id,r.project_id,r.environment_id,r.activation,r.artifact) facts_valid
          FROM public.runs r WHERE r.tenant_id=tenant AND r.state->>'status' NOT IN ('succeeded','failed','cancelled','timed_out')
        UNION ALL
        SELECT 'activation',a.id,a.project_id,a.environment_id,
          jsonb_build_object('artifact',v.artifact,'activation',a.payload,'releases',coalesce((SELECT jsonb_agg(w.payload ORDER BY w.id)
            FROM public.worker_releases w WHERE w.tenant_id=a.tenant_id AND w.project_id=a.project_id
              AND w.environment_id=a.environment_id AND w.id::text IN (
                SELECT value FROM jsonb_each_text(coalesce(a.payload->'request'->'worker_release_ids','{}'::jsonb))
                UNION SELECT value FROM jsonb_each_text(coalesce(a.payload->'request'->'connector_release_ids','{}'::jsonb))
              )), '[]'::jsonb)),
          public.weave_activation_facts(a.tenant_id,a.project_id,a.environment_id,a.payload,v.artifact)
          FROM public.activation_revisions a JOIN public.definition_versions v ON v.id=a.version_id
          WHERE a.tenant_id=tenant AND NOT coalesce((a.payload->>'retired')::boolean,false)
        UNION ALL
        SELECT 'provider',p.id,p.project_id,p.environment_id,
          jsonb_build_object('source',p.payload,'binding',(SELECT to_jsonb(b) FROM public.connection_source_bindings b WHERE b.id=p.binding_id)),
          p.payload->>'id'=p.id::text AND p.payload->>'binding_id'=p.binding_id::text
          AND p.payload->>'principal_id'=p.principal_id::text
          AND p.payload->'scope'=jsonb_build_object('tenant_id',p.tenant_id,'project_id',p.project_id,'environment_id',p.environment_id)
          AND (p.payload->>'activation_id') IS NOT DISTINCT FROM p.activation_id::text
          AND (p.payload->>'run_id') IS NOT DISTINCT FROM p.run_id::text
          AND EXISTS(SELECT 1 FROM public.connection_source_bindings b WHERE b.tenant_id=p.tenant_id
            AND b.project_id=p.project_id AND b.environment_id=p.environment_id AND b.id=p.binding_id
            AND b.source_kind='provider-source' AND b.source_id=p.id AND b.principal_id=p.principal_id
            AND b.connection_revision_id::text=p.payload->>'connection_revision_id')
          FROM public.provider_sources p
          WHERE p.tenant_id=tenant AND NOT p.disabled
        UNION ALL
        SELECT 'worker',w.id,w.project_id,w.environment_id,w.payload,w.payload->>'id'=w.id::text FROM public.worker_releases w
          WHERE w.tenant_id=tenant AND (EXISTS(SELECT 1 FROM public.worker_instances i WHERE i.release_id=w.id AND NOT i.revoked)
            OR EXISTS(SELECT 1 FROM public.task_intents t WHERE t.worker_release_id=w.id AND t.status IN ('ready','leased','retry_pending')))
        UNION ALL
        SELECT 'broker',p.id,p.project_id,p.environment_id,
          jsonb_build_object('source',p.payload,'binding',(SELECT to_jsonb(b) FROM public.connection_source_bindings b WHERE b.id=p.binding_id)),
          p.payload->>'id'=p.id::text AND p.payload->>'binding_id'=p.binding_id::text
          AND p.payload->>'principal_id'=p.principal_id::text
          AND (p.payload->>'activation_id') IS NOT DISTINCT FROM p.activation_id::text
          AND (p.payload->>'run_id') IS NOT DISTINCT FROM p.run_id::text
          FROM public.broker_routes p WHERE p.tenant_id=tenant AND NOT p.disabled
        UNION ALL
        SELECT 'subscription',p.id,p.project_id,p.environment_id,
          jsonb_build_object('source',p.payload,'binding',(SELECT to_jsonb(b) FROM public.connection_source_bindings b WHERE b.id=p.binding_id)),
          p.payload->>'id'=p.id::text AND p.payload->>'binding_id'=p.binding_id::text
          AND p.payload->>'principal_id'=p.principal_id::text
          FROM public.event_subscriptions p WHERE p.tenant_id=tenant AND p.active
        UNION ALL
        SELECT 'connection',c.id,c.project_id,c.environment_id,c.payload,
          c.payload->>'id'=c.id::text AND c.payload->>'revision'=c.revision::text
          AND c.payload->>'name'=c.name AND c.payload->>'connector_version_id'=c.connector_version_id::text
          AND EXISTS(SELECT 1 FROM public.definition_versions v WHERE v.tenant_id=c.tenant_id
            AND v.project_id=c.project_id AND v.id=c.connector_version_id AND v.kind='Connector'
            AND v.definition_digest=c.payload->>'connector_digest'
            AND v.name||'@'||v.version=c.payload->>'connector'
            AND v.document->'spec'->>'adapter'=c.payload->>'adapter')
          FROM public.connection_revisions c WHERE c.tenant_id=tenant AND (
            NOT EXISTS(SELECT 1 FROM public.connection_revisions newer WHERE newer.tenant_id=c.tenant_id
              AND newer.project_id=c.project_id AND newer.environment_id=c.environment_id
              AND newer.name=c.name AND newer.revision>c.revision)
            OR EXISTS(SELECT 1 FROM public.activation_connections ac WHERE ac.revision_id=c.id)
            OR EXISTS(SELECT 1 FROM public.connection_source_bindings b WHERE b.connection_revision_id=c.id AND NOT b.revoked))
      ), selected AS (
        SELECT r.*,octet_length(r.payload::text)::bigint bytes FROM requirements r
        WHERE after_kind IS NULL OR (r.kind,r.id)>(after_kind,after_id) ORDER BY r.kind,r.id LIMIT page_size
      ), bounded AS (SELECT s.*,sum(bytes) OVER(ORDER BY s.kind,s.id) total FROM selected s)
      SELECT b.kind,b.id,b.project_id,b.environment_id,
        CASE WHEN b.total<=8388608 THEN b.payload ELSE NULL::jsonb END,b.bytes,coalesce(b.facts_valid,false) FROM bounded b ORDER BY b.kind,b.id;
      END $$""")
    for function in ('weave_activation_facts(uuid,uuid,uuid,jsonb,jsonb)','weave_compatibility_counts()','weave_compatibility_policy()','weave_compatibility_tenants(uuid,integer)','weave_compatibility_page(uuid,text,uuid,integer)'):
        op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
        op.execute(f"ALTER FUNCTION {function} OWNER TO weave_retention_owner")
        op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        if not function.startswith("weave_activation_facts"):
            op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO weave_scheduler")
