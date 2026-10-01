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
"""Append-only installation status facts and monotonic presentation with full scoped RLS."""

from alembic import op

revision = "0020_whatsapp_status"
down_revision = "0019_teams_references"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE whatsapp_message_states(id uuid PRIMARY KEY,{scope},installation_id text NOT NULL,
        message_id text NOT NULL,recipient text NOT NULL,progress integer NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 3),
        failed_seen boolean NOT NULL DEFAULT false,deleted_seen boolean NOT NULL DEFAULT false,
        fact_count integer NOT NULL DEFAULT 0 CHECK(fact_count>=0),UNIQUE({key},id),
        UNIQUE({key},installation_id,message_id))""")
    op.execute(f"""CREATE TABLE whatsapp_status_facts(id uuid PRIMARY KEY,{scope},state_id uuid NOT NULL,
        status text NOT NULL CHECK(status IN ('sent','delivered','read','failed','deleted')),
        provider_timestamp text NOT NULL,error_codes jsonb NOT NULL,facts_digest text NOT NULL,
        UNIQUE({key},id),UNIQUE({key},state_id,status,provider_timestamp),
        FOREIGN KEY({key},state_id) REFERENCES whatsapp_message_states({key},id))""")
    op.execute(f"""CREATE TABLE whatsapp_status_observations({scope},source_id uuid NOT NULL,fact_id uuid NOT NULL,
        connection_revision_id uuid NOT NULL,event_id text NOT NULL,PRIMARY KEY({key},source_id,fact_id),
        FOREIGN KEY({key},source_id) REFERENCES provider_sources({key},id),
        FOREIGN KEY({key},fact_id) REFERENCES whatsapp_status_facts({key},id),
        FOREIGN KEY({key},connection_revision_id) REFERENCES connection_revisions({key},id))""")
    for table in ("whatsapp_message_states", "whatsapp_status_facts", "whatsapp_status_observations"):
        condition = (
            "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND "
            "project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND "
            "environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY whatsapp_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(progress,failed_seen,deleted_seen,fact_count) ON whatsapp_message_states TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0020_whatsapp_status'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
