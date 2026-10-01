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

"""Immutable scoped releases, worker instances, attempts and accepted receipts."""

from alembic import op

revision = "0007_workers"
down_revision = "0006_runtime"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE worker_releases(id uuid PRIMARY KEY,{scope},image_digest text NOT NULL,
        payload jsonb NOT NULL, UNIQUE({key},id),UNIQUE({key},image_digest),
        FOREIGN KEY({key}) REFERENCES environments(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE worker_instances(id uuid PRIMARY KEY,{scope},release_id uuid NOT NULL,
        principal_id uuid NOT NULL REFERENCES principals(id),payload jsonb NOT NULL,
        revoked boolean NOT NULL DEFAULT false,
        UNIQUE({key},id),FOREIGN KEY({key},release_id) REFERENCES worker_releases({key},id))""")
    op.execute(f"""CREATE TABLE task_leases({scope},task_id uuid NOT NULL,
        generation bigint NOT NULL CHECK(generation>0),
        owner uuid NOT NULL,token_hash text NOT NULL,claimed_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,deadline timestamptz NOT NULL,capability text NOT NULL,policy jsonb NOT NULL,
        status text NOT NULL DEFAULT 'active',PRIMARY KEY(task_id,generation),UNIQUE({key},task_id,generation),
        FOREIGN KEY({key},task_id) REFERENCES task_intents({key},id),
        FOREIGN KEY({key},owner) REFERENCES worker_instances({key},id),CHECK(expires_at<=deadline))""")
    op.execute(f"""CREATE TABLE completion_receipts({scope},task_id uuid NOT NULL,generation bigint NOT NULL,
        completion_id uuid NOT NULL,payload jsonb NOT NULL,PRIMARY KEY(task_id,completion_id),
        UNIQUE(task_id,generation),FOREIGN KEY({key},task_id,generation)
        REFERENCES task_leases({key},task_id,generation))""")
    op.execute(f"""CREATE TABLE worker_connection_grants({scope},release_id uuid NOT NULL,connection_id uuid NOT NULL,
        capability text NOT NULL,revoked boolean NOT NULL DEFAULT false,
        PRIMARY KEY(release_id,connection_id,capability),
        FOREIGN KEY({key},release_id) REFERENCES worker_releases({key},id),
        FOREIGN KEY({key},connection_id) REFERENCES connection_revisions({key},id))""")
    # Existing unsupported B5 worker fixtures are deliberately not adopted as trusted releases.
    op.execute(f"""ALTER TABLE task_intents ADD CONSTRAINT task_release_scope
        FOREIGN KEY({key},worker_release_id) REFERENCES worker_releases({key},id)""")
    for table in (
        "worker_releases",
        "worker_instances",
        "task_leases",
        "completion_receipts",
        "worker_connection_grants",
    ):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(revoked) ON worker_instances,worker_connection_grants TO weave_app")
    op.execute("GRANT UPDATE(expires_at,status) ON task_leases TO weave_app")
    op.execute("CREATE INDEX worker_attempt_owner ON task_leases(owner,expires_at) WHERE status='active'")
    op.execute("UPDATE weave_schema_version SET version='0007_workers'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
