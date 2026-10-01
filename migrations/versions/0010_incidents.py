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

"""Additive incident projection and immutable resolution receipts / retry links."""

from alembic import op
import sqlalchemy as sa

revision = "0010_incidents"
down_revision = "0009_triggers"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE incidents(id uuid PRIMARY KEY,{scope},run_id uuid NOT NULL,
        incident_key text NOT NULL,node_id text,generation integer,origin_code text NOT NULL,code text NOT NULL,
        status text NOT NULL CHECK(status IN ('active','closed','resolved')),revision integer NOT NULL CHECK(revision>0),
        actor_id uuid REFERENCES principals(id),resolved_at timestamptz,resolution jsonb,
        UNIQUE(run_id,incident_key),UNIQUE({key},id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    op.execute(f"""CREATE TABLE incident_resolution_receipts({scope},incident_id uuid NOT NULL,receipt_id uuid NOT NULL,
        actor_id uuid NOT NULL REFERENCES principals(id),request_hash text NOT NULL,response jsonb NOT NULL,
        PRIMARY KEY(incident_id,receipt_id),FOREIGN KEY({key},incident_id) REFERENCES incidents({key},id))""")
    op.execute(f"""CREATE TABLE run_retry_links({scope},run_id uuid PRIMARY KEY,parent_run_id uuid NOT NULL,
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id),
        FOREIGN KEY({key},parent_run_id) REFERENCES runs({key},id))""")
    for table in ("incidents", "incident_resolution_receipts", "run_retry_links"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(code,status,revision,actor_id,resolved_at,resolution) ON incidents TO weave_app")
    # Explicit deployment prerequisite: the migration identity has catalog-only
    # EXECUTE. No runtime role or role-membership options are modified here.
    connection = op.get_bind()
    permitted = connection.scalar(sa.text("SELECT has_function_privilege(current_user,'weave_tenant_ids()','EXECUTE')"))
    if not permitted:
        raise RuntimeError("Migration requires explicit EXECUTE on weave_tenant_ids() for tenant-scoped backfill")
    tenants = list(connection.execute(sa.text("SELECT weave_tenant_ids()")).scalars())
    previous = connection.scalar(sa.text("SELECT current_setting('weave.tenant_id',true)"))
    for tenant in tenants:
        connection.execute(sa.text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": str(tenant)})
        op.execute("""INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,
            node_id,generation,origin_code,code,status,revision)
            SELECT md5(r.id::text || ':' || i.key)::uuid,r.tenant_id,r.project_id,r.environment_id,r.id,i.key,
            i.value->>'node_id',(i.value->>'generation')::integer,i.value->>'code',i.value->>'code','active',1
            FROM runs r CROSS JOIN LATERAL jsonb_each(coalesce(r.state->'incidents','{}'::jsonb)) i
            WHERE r.tenant_id=current_setting('weave.tenant_id')::uuid AND r.state->>'status'='suspended' """)
        op.execute("""INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,
            node_id,generation,origin_code,code,status,revision)
            SELECT md5(id::text || ':@legacy')::uuid,tenant_id,project_id,environment_id,id,'@legacy',
            NULL,NULL,state->>'incident',state->>'incident','active',1 FROM runs
            WHERE tenant_id=current_setting('weave.tenant_id')::uuid AND state->>'status'='suspended' AND state->>'incident' IS NOT NULL
            AND coalesce(state->'incidents','{}'::jsonb)='{}'::jsonb""")
    connection.execute(sa.text("SELECT set_config('weave.tenant_id',:tenant,true)"), {"tenant": previous or ""})
    op.execute("UPDATE weave_schema_version SET version='0010_incidents'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
