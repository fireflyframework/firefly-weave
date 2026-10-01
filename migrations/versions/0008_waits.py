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

"""Durable signal inbox, timer arbitration and persisted retry scheduling."""

from alembic import op

revision = "0008_waits"
down_revision = "0007_workers"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE signal_receipts(id uuid PRIMARY KEY,{scope},run_id uuid NOT NULL,
        external_event_id text NOT NULL,name text NOT NULL,payload jsonb NOT NULL,
        request_hash text NOT NULL,accepted_at timestamptz NOT NULL,consumed boolean NOT NULL DEFAULT false,
        UNIQUE(tenant_id,project_id,run_id,external_event_id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    op.execute("ALTER TABLE run_deadlines ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE")
    op.execute(f"ALTER TABLE run_deadlines ADD UNIQUE({key},id)")
    op.execute(f"""CREATE TABLE wait_wakeups({scope},wait_id uuid NOT NULL,
        FOREIGN KEY({key},wait_id) REFERENCES run_deadlines({key},id),
        wakeup_kind text NOT NULL CHECK(wakeup_kind IN ('signal','timeout')),created_at timestamptz NOT NULL,
        UNIQUE(tenant_id,project_id,wait_id,wakeup_kind),UNIQUE(wait_id))""")
    op.execute("ALTER TABLE task_intents ADD COLUMN next_attempt_at timestamptz")
    op.execute("GRANT UPDATE(next_attempt_at) ON task_intents TO weave_app")
    for table in ("signal_receipts", "wait_wakeups"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(consumed) ON signal_receipts TO weave_app")
    op.execute("CREATE INDEX signals_pending ON signal_receipts(run_id,name,accepted_at) WHERE NOT consumed")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='weave_catalog_reader') "
        "THEN CREATE ROLE weave_catalog_reader NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO weave_catalog_reader")
    op.execute("GRANT SELECT(id) ON tenants TO weave_catalog_reader")
    op.execute("CREATE POLICY scheduler_catalog ON tenants FOR SELECT TO weave_catalog_reader USING (true)")
    # Ownership transfer needs CREATE temporarily; revoke before the migration commits.
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute("ALTER FUNCTION weave_tenant_ids() OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_tenant_ids() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_tenant_ids() TO weave_scheduler")
    op.execute("GRANT SELECT ON weave_schema_version,alembic_version TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0008_waits'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
