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

"""Scoped terminal archives and narrowly authorized, atomic execution removal."""
from alembic import op

revision = "0025_run_lifecycle"
down_revision = "0024_run_filters"

OWNED = (
    "run_event_evidence", "run_events", "step_instances", "task_intents",
    "task_leases", "completion_receipts", "run_deadlines", "wait_wakeups",
    "signal_receipts", "incidents", "incident_resolution_receipts",
    "run_policy_blocks", "runtime_capacity_blocks", "human_tasks",
    "human_task_decisions", "human_task_audit", "runs",
)

def upgrade():
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute("ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN "
        "('tenant_admin','developer','deployer','operator','viewer','worker','task_participant',"
        "'task_manager','email_reader','email_sender','email_manager','execution_manager'))")
    # No run FK: the small tombstone survives deletion to prevent idempotent resurrection.
    op.execute("""CREATE TABLE run_archives(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        run_id uuid PRIMARY KEY,archived boolean NOT NULL DEFAULT false,
        revision integer NOT NULL DEFAULT 0,archived_at timestamptz,purged_at timestamptz,
        FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id))""")
    condition = " AND ".join(f"{c}=nullif(current_setting('weave.{c}',true),'')::uuid"
        for c in ('tenant_id','project_id','environment_id'))
    op.execute("ALTER TABLE run_archives ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_archives FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON run_archives USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT,UPDATE ON run_archives TO weave_app,weave_retention_owner")
    op.execute("CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON run_archives "
        "FOR EACH ROW EXECUTE FUNCTION weave_usage_charge('run_id')")
    for table in OWNED:
        op.execute(f"GRANT SELECT,DELETE ON {table} TO weave_retention_owner")
    op.execute("GRANT UPDATE(state) ON runs TO weave_retention_owner")
    op.execute("GRANT SELECT,UPDATE(response) ON mutation_idempotency TO weave_retention_owner")
    # Existing accounting must not recreate a capacity row after deleting its parent.
    op.execute("""DO $migration$ DECLARE body text; BEGIN
        SELECT pg_get_functiondef('public.weave_reservation_touch(text,jsonb)'::regprocedure) INTO body;
        IF position('IF relation_name=''runs'' THEN' IN body)=0 THEN
            RAISE EXCEPTION 'Unexpected reservation function'; END IF;
        EXECUTE replace(body,'IF relation_name=''runs'' THEN',
            'IF relation_name=''runs'' AND EXISTS(SELECT 1 FROM public.runs WHERE id=identifier) THEN');
    END $migration$""")
    op.execute("""CREATE FUNCTION weave_purge_run(target uuid,expected integer) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE tenant uuid:=nullif(current_setting('weave.tenant_id',true),'')::uuid;
        project uuid:=nullif(current_setting('weave.project_id',true),'')::uuid;
        environment uuid:=nullif(current_setting('weave.environment_id',true),'')::uuid;
        snapshot jsonb; task_ids uuid[];
      BEGIN
        IF environment IS NULL OR NOT public.weave_operation_fenced(tenant,project) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Scoped operation required'; END IF;
        SELECT state INTO snapshot FROM public.runs WHERE tenant_id=tenant AND project_id=project
          AND environment_id=environment AND id=target FOR UPDATE;
        IF snapshot IS NULL OR snapshot->>'status' NOT IN ('succeeded','failed','cancelled','timed_out') THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Terminal execution required'; END IF;
        PERFORM 1 FROM public.run_archives WHERE run_id=target AND tenant_id=tenant AND project_id=project
          AND environment_id=environment AND archived AND revision=expected AND purged_at IS NULL FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Current archive required'; END IF;
        IF EXISTS(SELECT 1 FROM public.task_leases l JOIN public.task_intents t ON t.id=l.task_id
          WHERE t.run_id=target AND l.expires_at>clock_timestamp() AND l.status='active') THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Worker lease still active'; END IF;
        SELECT array_agg(id) INTO task_ids FROM public.human_tasks WHERE run_id=target;
        UPDATE public.mutation_idempotency SET response=jsonb_build_object('id',target,'_weave_purged',true)
          WHERE tenant_id=tenant AND project_id=project AND (
            response->>'id'=target::text OR response->>'run_id'=target::text
            OR response->>'id'=ANY(SELECT unnest(task_ids)::text)
            OR position(target::text IN operation)>0);
        DELETE FROM public.human_task_decisions WHERE task_id=ANY(task_ids);
        DELETE FROM public.human_task_audit WHERE task_id=ANY(task_ids);
        DELETE FROM public.human_tasks WHERE run_id=target;
        DELETE FROM public.completion_receipts WHERE task_id IN(SELECT id FROM public.task_intents WHERE run_id=target);
        DELETE FROM public.task_leases WHERE task_id IN(SELECT id FROM public.task_intents WHERE run_id=target);
        DELETE FROM public.task_intents WHERE run_id=target;
        DELETE FROM public.wait_wakeups WHERE wait_id IN(SELECT id FROM public.run_deadlines WHERE run_id=target);
        DELETE FROM public.run_deadlines WHERE run_id=target;
        DELETE FROM public.signal_receipts WHERE run_id=target;
        DELETE FROM public.incident_resolution_receipts WHERE incident_id IN(SELECT id FROM public.incidents WHERE run_id=target);
        DELETE FROM public.incidents WHERE run_id=target;
        DELETE FROM public.run_event_evidence WHERE run_id=target;
        DELETE FROM public.run_events WHERE run_id=target;
        DELETE FROM public.step_instances WHERE run_id=target;
        DELETE FROM public.run_policy_blocks WHERE run_id=target;
        DELETE FROM public.runtime_capacity_blocks WHERE run_id=target;
        -- Foreign keys deliberately block independent email, trigger, retry, and schedule records.
        -- Any blocker rolls back the entire function, including receipt redaction.
        DELETE FROM public.runs WHERE id=target;
        UPDATE public.run_archives SET purged_at=clock_timestamp(),revision=revision+1 WHERE run_id=target;
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
    op.execute("ALTER FUNCTION weave_purge_run(uuid,integer) OWNER TO weave_retention_owner")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
    op.execute("REVOKE ALL ON FUNCTION weave_purge_run(uuid,integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_purge_run(uuid,integer) TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0025_run_lifecycle'")

def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
