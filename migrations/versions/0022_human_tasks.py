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

"""Scoped human assignments, immutable decisions and independent manual controls."""

from alembic import op

revision = "0022_human_tasks"
down_revision = "0021_operations"


def upgrade():
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(
        "ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN "
        "('tenant_admin','developer','deployer','operator','viewer','worker','task_participant','task_manager'))"
    )
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE human_task_groups({scope},id uuid PRIMARY KEY,name text NOT NULL,
        revision integer NOT NULL CHECK(revision>0),UNIQUE({key},id),UNIQUE({key},name),
        FOREIGN KEY({key}) REFERENCES environments(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE human_task_memberships({scope},group_id uuid NOT NULL,
        principal_id uuid NOT NULL REFERENCES principals(id),PRIMARY KEY(group_id,principal_id),
        FOREIGN KEY({key},group_id) REFERENCES human_task_groups({key},id))""")
    op.execute(f"""CREATE TABLE human_assignment_bindings({scope},id uuid PRIMARY KEY,name text NOT NULL,
        revision integer NOT NULL CHECK(revision>0),enabled boolean NOT NULL,payload jsonb NOT NULL,
        UNIQUE({key},id),UNIQUE({key},name),FOREIGN KEY({key}) REFERENCES environments(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE human_tasks({scope},id uuid PRIMARY KEY,run_id uuid NOT NULL,node_id text NOT NULL,
        revision integer NOT NULL CHECK(revision>0),status text NOT NULL CHECK(status IN
        ('ready','claimed','completed','expired','cancelled')),claimant_id uuid REFERENCES principals(id),
        assignment jsonb NOT NULL,title text NOT NULL,context jsonb NOT NULL,form_schema jsonb NOT NULL,
        decisions jsonb NOT NULL,created_at timestamptz NOT NULL,due_at timestamptz,expires_at timestamptz,
        completed_at timestamptz,decision_actor_id uuid REFERENCES principals(id),output jsonb,
        UNIQUE({key},id),UNIQUE(run_id,node_id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    op.execute(f"""CREATE TABLE human_task_decisions({scope},task_id uuid PRIMARY KEY,
        actor_id uuid NOT NULL REFERENCES principals(id),output jsonb NOT NULL,accepted_at timestamptz NOT NULL,
        FOREIGN KEY({key},task_id) REFERENCES human_tasks({key},id))""")
    op.execute(f"""CREATE TABLE human_task_audit({scope},task_id uuid NOT NULL,revision integer NOT NULL,
        action text NOT NULL,actor_id uuid REFERENCES principals(id),created_at timestamptz NOT NULL,reason text,
        PRIMARY KEY(task_id,revision),FOREIGN KEY({key},task_id) REFERENCES human_tasks({key},id))""")
    op.execute(
        "CREATE INDEX human_tasks_queue ON "
        "human_tasks(tenant_id,project_id,environment_id,id) WHERE status IN ('ready','claimed')"
    )
    for table in (
        "human_task_groups",
        "human_task_memberships",
        "human_assignment_bindings",
        "human_tasks",
        "human_task_decisions",
        "human_task_audit",
    ):
        condition = " AND ".join(
            f"{column}=nullif(current_setting('weave.{column}',true),'')::uuid"
            for column in ("tenant_id", "project_id", "environment_id")
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY exact_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(revision) ON human_task_groups TO weave_app")
    op.execute("GRANT DELETE ON human_task_memberships TO weave_app")
    op.execute("GRANT UPDATE(revision,enabled,payload) ON human_assignment_bindings TO weave_app")
    op.execute(
        "GRANT "
        "UPDATE(revision,status,claimant_id,assignment,completed_at,decision_actor_id,output) "
        "ON human_tasks TO weave_app"
    )
    op.execute("UPDATE weave_schema_version SET version='0022_human_tasks'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
