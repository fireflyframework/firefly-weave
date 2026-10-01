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

"""Local identities, scoped grants, FORCE RLS, and narrowly privileged scheduler."""

from alembic import op

revision = "0002_access"
down_revision = "0001_boot"


def upgrade():
    statements = [
        (
            "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='weave_app') THEN CREATE "
            "ROLE weave_app NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; IF NOT EXISTS(SELECT FROM "
            "pg_roles WHERE rolname='weave_scheduler') THEN CREATE ROLE weave_scheduler NOLOGIN "
            "NOSUPERUSER NOBYPASSRLS; END IF; END $$"
        ),
        (
            "CREATE TABLE principals(id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN "
            "('human','application','worker')), active boolean NOT NULL DEFAULT true)"
        ),
        (
            "CREATE TABLE identity_links(provider_id text NOT NULL, issuer text NOT NULL, subject "
            "text NOT NULL, principal_id uuid NOT NULL REFERENCES principals(id), PRIMARY "
            "KEY(provider_id,issuer,subject))"
        ),
        "CREATE TABLE platform_administrators(principal_id uuid PRIMARY KEY REFERENCES principals(id))",
        "CREATE TABLE tenants(id uuid PRIMARY KEY, name text NOT NULL)",
        (
            "CREATE TABLE projects(id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES "
            "tenants(id), name text NOT NULL, UNIQUE(tenant_id,id))"
        ),
        (
            "CREATE TABLE environments(id uuid PRIMARY KEY, tenant_id uuid NOT NULL, project_id "
            "uuid NOT NULL, name text NOT NULL, UNIQUE(tenant_id,project_id,id), FOREIGN "
            "KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))"
        ),
        (
            "CREATE TABLE role_bindings(id uuid PRIMARY KEY, principal_id uuid NOT NULL REFERENCES "
            "principals(id), tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid, "
            "environment_id uuid, role text NOT NULL CHECK(role IN "
            "('tenant_admin','developer','deployer','operator','viewer','worker')), resources "
            "jsonb NOT NULL DEFAULT '[]', CHECK(environment_id IS NULL OR project_id IS NOT NULL), "
            "FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id), FOREIGN "
            "KEY(tenant_id,project_id,environment_id) REFERENCES "
            "environments(tenant_id,project_id,id))"
        ),
        "CREATE INDEX role_bindings_principal ON role_bindings(principal_id)",
        (
            "CREATE TABLE access_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "principal_id uuid REFERENCES principals(id), action text NOT NULL, target text NOT "
            "NULL, created_at timestamptz NOT NULL DEFAULT now())"
        ),
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        "GRANT USAGE ON SCHEMA public TO weave_app,weave_scheduler",
        "GRANT SELECT ON weave_schema_version,alembic_version TO weave_app",
        (
            "GRANT SELECT,INSERT,UPDATE,DELETE ON "
            "principals,identity_links,platform_administrators,tenants,projects,environments,role_bindings TO weave_app"
        ),
        "GRANT INSERT ON access_audit TO weave_app",
        "GRANT USAGE ON SEQUENCE access_audit_id_seq TO weave_app",
    ]
    for sql in statements:
        op.execute(sql)
    for table in ("tenants", "projects", "environments", "role_bindings"):
        tenant = "id" if table == "tenants" else "tenant_id"
        context = f"{tenant} = nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} USING ({context}) WITH CHECK ({context})")
    # Authentication may enumerate only this linked principal's grants before tenant binding.
    op.execute(
        "CREATE POLICY own_grants ON role_bindings FOR SELECT USING (principal_id = "
        "nullif(current_setting('weave.principal_id',true),'')::uuid)"
    )
    op.execute(
        "CREATE FUNCTION weave_tenant_ids() RETURNS SETOF uuid LANGUAGE sql SECURITY DEFINER "
        "SET search_path=pg_catalog AS 'SELECT id FROM public.tenants'"
    )
    op.execute("REVOKE ALL ON FUNCTION weave_tenant_ids() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_tenant_ids() TO weave_scheduler")
    op.execute("UPDATE weave_schema_version SET version='0002_access'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
