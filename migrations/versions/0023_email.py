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
"""Scoped mail conversation facts, fenced submissions and poll cursors."""

from alembic import op

revision = "0023_email"
down_revision = "0022_human_tasks"


def upgrade():
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(
        "ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN "
        "('tenant_admin','developer','deployer','operator','viewer','worker',"
        "'task_participant','task_manager','email_reader','email_sender','email_manager'))"
    )
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    tables = {
        "email_conversations": f"""id uuid PRIMARY KEY,{scope},connection_revision_id uuid NOT NULL,subject text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),run_id uuid,FOREIGN KEY({key},run_id) REFERENCES runs({key},id),UNIQUE({key},id),FOREIGN KEY({key},connection_revision_id) REFERENCES connection_revisions({key},id)""",
        "email_messages": f"""id uuid PRIMARY KEY,{scope},conversation_id uuid,connection_revision_id uuid NOT NULL,direction text NOT NULL CHECK(direction IN ('inbound','outbound')),state text NOT NULL,accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),payload jsonb NOT NULL,transport_key text,UNIQUE({key},id),UNIQUE({key},connection_revision_id,transport_key),FOREIGN KEY({key},conversation_id) REFERENCES email_conversations({key},id),FOREIGN KEY({key},connection_revision_id) REFERENCES connection_revisions({key},id)""",
        "email_submissions": f"""id uuid PRIMARY KEY,{scope},principal_id uuid NOT NULL REFERENCES principals(id),request_id uuid NOT NULL,fingerprint text NOT NULL,connection_revision_id uuid NOT NULL,conversation_id uuid NOT NULL,message_id uuid NOT NULL,state text NOT NULL CHECK(state IN ('queued','attempting','accepted','rejected','unknown')),generation integer NOT NULL DEFAULT 0,lease_until timestamptz,request jsonb NOT NULL,result jsonb NOT NULL DEFAULT '{{}}'::jsonb,UNIQUE({key},principal_id,request_id),FOREIGN KEY({key},conversation_id) REFERENCES email_conversations({key},id),FOREIGN KEY({key},message_id) REFERENCES email_messages({key},id)""",
        "email_sources": f"""id uuid PRIMARY KEY,{scope},principal_id uuid NOT NULL REFERENCES principals(id),connection_revision_id uuid NOT NULL,activation_id uuid,mailbox text NOT NULL,uidvalidity bigint,last_uid bigint NOT NULL DEFAULT 0,generation integer NOT NULL DEFAULT 0,lease_until timestamptz,state text NOT NULL DEFAULT 'active',policy jsonb NOT NULL,UNIQUE({key},id),FOREIGN KEY({key},connection_revision_id) REFERENCES connection_revisions({key},id)""",
        "email_receipts": f"""id uuid PRIMARY KEY,{scope},source_id uuid NOT NULL,transport_key text NOT NULL,message_id uuid,state text NOT NULL,reason text,run_id uuid,signal_id uuid,correlation_id uuid,FOREIGN KEY({key},run_id) REFERENCES runs({key},id),UNIQUE({key},source_id,transport_key),FOREIGN KEY({key},source_id) REFERENCES email_sources({key},id),FOREIGN KEY({key},message_id) REFERENCES email_messages({key},id)""",
        "email_correlations": f"""id uuid PRIMARY KEY,{scope},source_id uuid NOT NULL,conversation_id uuid NOT NULL,run_id uuid NOT NULL,signal text NOT NULL,token_hash text UNIQUE,expires_at timestamptz NOT NULL,revoked boolean NOT NULL DEFAULT false,UNIQUE({key},id),FOREIGN KEY({key},source_id) REFERENCES email_sources({key},id),FOREIGN KEY({key},conversation_id) REFERENCES email_conversations({key},id),FOREIGN KEY({key},run_id) REFERENCES runs({key},id)""",
    }
    for table, definition in tables.items():
        op.execute(f"CREATE TABLE {table}({definition})")
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY email_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON {table} TO weave_app")
    op.execute(
        "ALTER TABLE email_receipts ADD FOREIGN KEY(tenant_id,project_id,environment_id,correlation_id) REFERENCES email_correlations(tenant_id,project_id,environment_id,id)"
    )
    op.execute(
        """CREATE INDEX email_parent_lookup ON email_messages(tenant_id,project_id,environment_id,connection_revision_id,((payload->>'message_id')))"""
    )
    op.execute("UPDATE weave_schema_version SET version='0023_email'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
