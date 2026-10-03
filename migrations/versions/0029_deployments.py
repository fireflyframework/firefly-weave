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

"""Persist environment-scoped deployment intent, approvals and fenced runner jobs."""

from alembic import op

revision = "0029_deployments"
down_revision = "0028_files"


def upgrade():
    roles = (
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
    )
    accepted = ",".join("'" + role + "'" for role in roles)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    tables = ("targets", "deployments", "observations", "plans", "approvals", "runners", "jobs", "reports")
    for name in tables:
        extra = ""
        if name == "jobs":
            extra = (
                ",state text NOT NULL,generation integer NOT NULL DEFAULT 0,runner_id uuid,"
                "lease_token uuid,lease_expires_at timestamptz"
            )
        if name == "reports":
            extra = ",job_id uuid NOT NULL,runner_id uuid NOT NULL,request_hash text NOT NULL"
        parent = "" if name == "targets" else f",FOREIGN KEY({key},target_id) REFERENCES deployment_targets({key},id)"
        op.execute(f"""CREATE TABLE deployment_{name}(
            id uuid PRIMARY KEY,{scope},target_id uuid NOT NULL,deployment_id uuid,
            revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            payload jsonb NOT NULL CHECK(octet_length(payload::text)<=262144){extra},
            UNIQUE({key},id),FOREIGN KEY({key}) REFERENCES environments(tenant_id,project_id,id){parent})""")
        op.execute(f"CREATE INDEX deployment_{name}_scope ON deployment_{name}({key},target_id,id)")
        condition = (
            "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND "
            "project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND "
            "environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
        )
        op.execute(f"ALTER TABLE deployment_{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE deployment_{name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY exact_scope ON deployment_{name} TO weave_app USING ({condition}) WITH CHECK ({condition})"
        )
        privileges = (
            "SELECT,INSERT" if name in {"observations", "plans", "approvals", "reports"} else "SELECT,INSERT,UPDATE"
        )
        op.execute(f"GRANT {privileges} ON deployment_{name} TO weave_app")
    op.execute("""CREATE UNIQUE INDEX deployment_one_active_target ON deployment_jobs(target_id)
        WHERE state IN ('queued','claimed','running','verifying')""")
    op.execute("CREATE INDEX deployment_jobs_claim ON deployment_jobs(tenant_id,project_id,environment_id,state,id)")
    op.execute("UPDATE weave_schema_version SET version='0029_deployments'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
