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

"""Append-only catalog, source revisions, drafts, activations and mutation replay."""

from alembic import op

revision = "0004_definitions"
down_revision = "0003_access_audit"


def upgrade():
    project_fk = "FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)"
    version_fk = "FOREIGN KEY(tenant_id,project_id,version_id) REFERENCES definition_versions(tenant_id,project_id,id)"
    statements = [
        f"""CREATE TABLE definition_versions(
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
            kind text NOT NULL CHECK(kind IN ('Workflow','Action','Connector')),
            name text NOT NULL, version text NOT NULL,
            digest text NOT NULL, definition_digest text NOT NULL, document jsonb NOT NULL, artifact jsonb NOT NULL,
            UNIQUE(tenant_id,project_id,kind,name,version), UNIQUE(tenant_id,project_id,id), {project_fk})""",
        f"""CREATE TABLE definition_sources(
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL, version_id uuid NOT NULL,
            source text NOT NULL, format text NOT NULL CHECK(format IN ('yaml','json')), source_hash text NOT NULL,
            envelope jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), {version_fk},
            UNIQUE(tenant_id,project_id,version_id,source_hash,format))""",
        f"""CREATE TABLE definition_retirements(
            tenant_id uuid NOT NULL, project_id uuid NOT NULL, version_id uuid NOT NULL,
            PRIMARY KEY(tenant_id,project_id,version_id), {version_fk})""",
        f"""CREATE TABLE draft_revisions(
            id uuid NOT NULL, tenant_id uuid NOT NULL, project_id uuid NOT NULL,
            revision integer NOT NULL CHECK(revision>0), document jsonb NOT NULL,
            PRIMARY KEY(tenant_id,project_id,id,revision), {project_fk})""",
        f"""CREATE TABLE activation_revisions(
            id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id uuid NOT NULL, environment_id uuid NOT NULL,
            name text NOT NULL, revision integer NOT NULL CHECK(revision>0), version_id uuid NOT NULL,
            payload jsonb NOT NULL, UNIQUE(tenant_id,project_id,environment_id,name,revision),
            UNIQUE(tenant_id,project_id,environment_id,id), {version_fk},
            FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id))""",
        f"""CREATE TABLE mutation_idempotency(
            tenant_id uuid NOT NULL, project_id uuid NOT NULL, principal_id uuid NOT NULL REFERENCES principals(id),
            operation text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL, response jsonb NOT NULL,
            PRIMARY KEY(tenant_id,project_id,principal_id,operation,idempotency_key), {project_fk})""",
    ]
    for sql in statements:
        op.execute(sql)
    for table in (
        "definition_versions",
        "definition_sources",
        "definition_retirements",
        "draft_revisions",
        "activation_revisions",
        "mutation_idempotency",
    ):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0004_definitions'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
