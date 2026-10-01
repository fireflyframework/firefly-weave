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

"""Immutable environment connection revisions, grants, and explicit test jobs."""

from alembic import op

revision = "0005_connections"
down_revision = "0004_definitions"


def upgrade():
    environment_fk = "FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id)"
    revision_fk = (
        "FOREIGN KEY(tenant_id,project_id,environment_id,revision_id) "
        "REFERENCES connection_revisions(tenant_id,project_id,environment_id,id)"
    )
    op.execute(f"""CREATE TABLE connection_revisions(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        name text NOT NULL,revision integer NOT NULL CHECK(revision>0),connector_version_id uuid NOT NULL,
        payload jsonb NOT NULL,UNIQUE(tenant_id,project_id,environment_id,name,revision),
        UNIQUE(tenant_id,project_id,environment_id,id),{environment_fk},
        FOREIGN KEY(tenant_id,project_id,connector_version_id)
        REFERENCES definition_versions(tenant_id,project_id,id))""")
    op.execute(f"""CREATE TABLE connection_grants(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,revision_id uuid NOT NULL,
        handle text NOT NULL,PRIMARY KEY(tenant_id,project_id,environment_id,revision_id,handle),{revision_fk})""")
    op.execute(f"""CREATE TABLE connection_test_jobs(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        revision_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES principals(id),
        UNIQUE(tenant_id,project_id,environment_id,id),{revision_fk})""")
    op.execute("""CREATE TABLE connection_test_results(
        job_id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        payload jsonb NOT NULL,FOREIGN KEY(tenant_id,project_id,environment_id,job_id)
        REFERENCES connection_test_jobs(tenant_id,project_id,environment_id,id))""")
    op.execute(f"""CREATE TABLE activation_connections(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        activation_id uuid NOT NULL,slot text NOT NULL,revision_id uuid NOT NULL,
        PRIMARY KEY(tenant_id,project_id,environment_id,activation_id,slot),{revision_fk},
        FOREIGN KEY(tenant_id,project_id,environment_id,activation_id)
        REFERENCES activation_revisions(tenant_id,project_id,environment_id,id))""")
    for table in (
        "connection_revisions",
        "connection_grants",
        "connection_test_jobs",
        "connection_test_results",
        "activation_connections",
    ):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0005_connections'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
