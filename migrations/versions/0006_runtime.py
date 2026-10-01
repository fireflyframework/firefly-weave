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

"""Run snapshots, accepted events, step projections and durable pending work."""

from alembic import op

revision = "0006_runtime"
down_revision = "0005_connections"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    run_fk = f"FOREIGN KEY({key},run_id) REFERENCES runs({key},id)"
    op.execute(f"""CREATE TABLE runs(
        id uuid PRIMARY KEY,{scope},activation_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES principals(id),
        artifact jsonb NOT NULL,activation jsonb NOT NULL,request jsonb NOT NULL,state jsonb NOT NULL,
        UNIQUE({key},id),FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id))""")
    op.execute(f"""CREATE TABLE run_events(
        {scope},run_id uuid NOT NULL,id uuid NOT NULL,sequence bigint NOT NULL CHECK(sequence>0),
        type text NOT NULL,data jsonb NOT NULL,created_at timestamptz NOT NULL,request_hash text NOT NULL,
        transition jsonb NOT NULL,response jsonb NOT NULL,PRIMARY KEY(run_id,id),UNIQUE(run_id,sequence),{run_fk})""")
    op.execute(f"""CREATE TABLE step_instances(
        {scope},run_id uuid NOT NULL,node_id text NOT NULL,status text NOT NULL,output jsonb NOT NULL,
        PRIMARY KEY(run_id,node_id),{run_fk})""")
    op.execute(f"""CREATE TABLE task_intents(
        id uuid PRIMARY KEY,{scope},run_id uuid NOT NULL,node_id text NOT NULL,payload jsonb NOT NULL,
        worker_release_id uuid,operation_key text NOT NULL UNIQUE,status text NOT NULL DEFAULT 'ready',
        UNIQUE(run_id,node_id),UNIQUE({key},id),{run_fk})""")
    op.execute(f"""CREATE TABLE run_deadlines(
        {scope},run_id uuid NOT NULL,node_id text NOT NULL,deadline timestamptz NOT NULL,
        consumed boolean NOT NULL DEFAULT false,PRIMARY KEY(run_id,node_id),{run_fk})""")
    op.execute(
        "CREATE INDEX task_intents_ready ON task_intents(tenant_id,project_id,environment_id,id) WHERE status='ready'"
    )
    op.execute("CREATE INDEX run_deadlines_due ON run_deadlines(tenant_id,deadline) WHERE NOT consumed")
    for table in ("runs", "run_events", "step_instances", "task_intents", "run_deadlines"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(state) ON runs TO weave_app")
    op.execute("GRANT UPDATE(status,output) ON step_instances TO weave_app")
    op.execute("GRANT UPDATE(status) ON task_intents TO weave_app")
    op.execute("GRANT UPDATE(consumed) ON run_deadlines TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0006_runtime'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
