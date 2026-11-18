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

"""Run log entries that workers and connectors write while they hold a task, and each environment's daily count.

Entries are immutable for the app: it inserts, reads and prunes them. They go with their run (a purge cascades), and
they don't count toward usage: their own quotas bound them, and retention prunes them. Each entry copies its run's
workflow name and test flag, which never change after the run starts. ``runs``, the facts tables and
``weave_purge_run`` are unchanged; no role is added, and the role check is rebuilt from the same roles.
"""

from alembic import op

revision = "0043_operations_logs"
down_revision = "0042_operations_alerts"

KEY = "tenant_id,project_id,environment_id"
SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
ENVIRONMENT = f"FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))
# The same roles as 0042_operations_alerts: this migration adds none.
ROLES = (
    "platform_admin",
    "tenant_admin",
    "developer",
    "deployer",
    "operator",
    "viewer",
    "worker",
    "task_participant",
    "task_manager",
    "email_reader",
    "email_sender",
    "email_manager",
    "execution_manager",
    "lumi_user",
    "lumi_manager",
    "file_reader",
    "file_manager",
    "deployment_reader",
    "deployment_planner",
    "deployment_approver",
    "deployment_operator",
    "deployment_runner",
    "worker_operator",
    "transaction_reconciler",
    "step_tester",
    "alert_manager",
)
FIELDS = "weave_log_fields_valid(jsonb)"


def secure(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT {grants} ON {table} TO weave_app")


def entries() -> None:
    """One row per entry, keyed for one run's entries in time order and for newest-first reads across runs."""
    op.execute(f"""CREATE FUNCTION {FIELDS.replace("(jsonb)", "(fields jsonb)")} RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
      BEGIN
        RETURN jsonb_typeof(fields)='object' AND (SELECT count(*) FROM jsonb_object_keys(fields))<=16
          AND octet_length(fields::text)<=8192;
      END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {FIELDS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FIELDS} TO weave_app")
    # task_id comes from the lease the insert joins, so it always names a task of the run. It has no foreign key: a
    # purge deletes a run's tasks before the run, and a cascade from tasks would need a fourth index.
    op.execute(f"""CREATE TABLE execution_logs(
        {SCOPE_COLUMNS},id uuid NOT NULL,run_id uuid NOT NULL,task_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation>0),
        node_id text NOT NULL CHECK(node_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$' AND char_length(node_id)<=512),
        instance_key text NOT NULL DEFAULT '' CHECK(char_length(instance_key)<=512),
        worker_id uuid NOT NULL,at timestamptz NOT NULL,received_at timestamptz NOT NULL,
        source text NOT NULL CHECK(source IN ('worker','connector','ai')),
        level text NOT NULL CHECK(level IN ('debug','info','warning','error')),
        code text CHECK(code ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{{0,99}}$'),
        message text NOT NULL CHECK(char_length(message)<=512),
        fields jsonb NOT NULL DEFAULT '{{}}' CHECK({FIELDS.replace("(jsonb)", "(fields)")}),
        redactions integer NOT NULL DEFAULT 0 CHECK(redactions>=0),truncated boolean NOT NULL DEFAULT false,
        bytes integer NOT NULL CHECK(bytes>=0),workflow_name text NOT NULL CHECK(char_length(workflow_name)<=200),
        test boolean NOT NULL,
        PRIMARY KEY({KEY},id),FOREIGN KEY({KEY},run_id) REFERENCES runs({KEY},id) ON DELETE CASCADE)""")
    op.execute(f"CREATE INDEX execution_logs_run ON execution_logs({KEY},run_id,at,id)")
    op.execute(f"CREATE INDEX execution_logs_recent ON execution_logs({KEY},at DESC,id DESC)")
    op.execute(f"CREATE INDEX execution_logs_workflow ON execution_logs({KEY},workflow_name,at DESC,id DESC)")
    secure("execution_logs", "SELECT,INSERT,DELETE")
    op.execute("GRANT SELECT,DELETE ON execution_logs TO weave_retention_owner")
    op.execute("CREATE POLICY operations_inventory ON execution_logs TO weave_retention_owner USING(true)")


def days() -> None:
    """Entries an environment accepted and dropped per UTC day, for the daily quota; pruned after 35 days."""
    op.execute(f"""CREATE TABLE execution_log_days(
        {SCOPE_COLUMNS},day date NOT NULL,accepted bigint NOT NULL DEFAULT 0 CHECK(accepted>=0),
        dropped bigint NOT NULL DEFAULT 0 CHECK(dropped>=0),PRIMARY KEY({KEY},day),{ENVIRONMENT})""")
    secure("execution_log_days", "SELECT,INSERT,UPDATE,DELETE")


def upgrade() -> None:
    entries()
    days()
    accepted = ",".join("'" + role + "'" for role in ROLES)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    for table in ("execution_logs", "execution_log_days"):
        op.execute(f"ANALYZE {table}")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
