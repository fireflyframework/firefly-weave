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

"""Container log requests, the snapshots a target's runner reads for them, and the role that may read them.

A person asks for a snapshot of what a target's worker containers printed; the target's runner claims the request,
reads the lines with a fixed command and reports them, and the snapshot is kept for one hour. Requests and snapshots
live in their own tables, apart from deployment jobs, so a log request never waits for, or blocks, the one active job
of a target. Neither table counts toward usage: requests are rate limited per target and both expire after an hour.
``deployment_log_reader`` joins the role check; ``runs`` and ``weave_purge_run`` are unchanged.
"""

from alembic import op

revision = "0046_operations_log_requests"
down_revision = "0045_ai_agent_conversations"

KEY = "tenant_id,project_id,environment_id"
SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
ENVIRONMENT = f"FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))
# The roles of 0043_operations_logs, plus the container log reader.
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
    "deployment_log_reader",
)


def secure(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT {grants} ON {table} TO weave_app")


def requests() -> None:
    """One row per request: what to read, who asked, and how far the runner got. Pending requests are read per
    target in the order they were made, the per-target rate counts the last minute, and expiry reads by end time."""
    op.execute(f"""CREATE TABLE deployment_log_requests(
        {SCOPE_COLUMNS},id uuid NOT NULL,target_id uuid NOT NULL,requested_by uuid NOT NULL,
        component text NOT NULL CHECK(component ~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{{0,99}}$'),
        members jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(members)='array' AND jsonb_array_length(members)<=5),
        since_seconds integer NOT NULL CHECK(since_seconds BETWEEN 60 AND 86400),
        tail_lines integer NOT NULL CHECK(tail_lines BETWEEN 1 AND 1000),
        state text NOT NULL CHECK(state IN ('pending','claimed','ready','failed')),
        failure text CHECK(failure IN ('no_answer','unknown_component','no_members','unreadable')),
        runner_id uuid,claim_token uuid,claim_expires_at timestamptz,
        created_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,read_at timestamptz,
        CHECK(expires_at>created_at),CHECK((state='failed')=(failure IS NOT NULL)),
        CHECK(state='pending' OR (runner_id IS NOT NULL AND claim_token IS NOT NULL)),
        PRIMARY KEY({KEY},id),{ENVIRONMENT},
        FOREIGN KEY({KEY},target_id) REFERENCES deployment_targets({KEY},id))""")
    op.execute(f"CREATE INDEX deployment_log_requests_target ON deployment_log_requests({KEY},target_id,created_at)")
    op.execute(f"CREATE INDEX deployment_log_requests_expiry ON deployment_log_requests({KEY},expires_at)")
    secure("deployment_log_requests", "SELECT,INSERT,UPDATE,DELETE")


def snapshots() -> None:
    """The lines a runner reported for one request, already redacted and cut, kept as long as the request.

    At most 256 KiB of line text; the document's own ceiling leaves room for JSON's quoting and escapes."""
    op.execute(f"""CREATE TABLE deployment_log_snapshots(
        {SCOPE_COLUMNS},request_id uuid NOT NULL,
        members jsonb NOT NULL CHECK(jsonb_typeof(members)='array' AND jsonb_array_length(members)<=5
          AND octet_length(members::text)<=2097152),
        truncated boolean NOT NULL DEFAULT false,bytes integer NOT NULL CHECK(bytes BETWEEN 0 AND 262144),
        read_at timestamptz NOT NULL,
        PRIMARY KEY({KEY},request_id),
        FOREIGN KEY({KEY},request_id) REFERENCES deployment_log_requests({KEY},id) ON DELETE CASCADE)""")
    secure("deployment_log_snapshots", "SELECT,INSERT,DELETE")


def upgrade() -> None:
    requests()
    snapshots()
    accepted = ",".join("'" + role + "'" for role in ROLES)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    for table in ("deployment_log_requests", "deployment_log_snapshots"):
        op.execute(f"ANALYZE {table}")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
