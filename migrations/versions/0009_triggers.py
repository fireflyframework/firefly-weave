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

"""Immutable connector execution pins and signed ingress receipts."""
from alembic import op

revision = "0009_triggers"
down_revision = "0008_waits"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE activation_connector_releases({scope},activation_id uuid NOT NULL,
        connector_version_id uuid NOT NULL,release_id uuid NOT NULL,PRIMARY KEY(activation_id,connector_version_id),
        FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id),
        FOREIGN KEY(tenant_id,project_id,connector_version_id) REFERENCES definition_versions(tenant_id,project_id,id),
        FOREIGN KEY({key},release_id) REFERENCES worker_releases({key},id))""")
    op.execute(f"""CREATE TABLE trigger_routes(id uuid PRIMARY KEY,{scope},principal_id uuid NOT NULL REFERENCES principals(id),
        activation_id uuid,run_id uuid,secret_ref text NOT NULL,tolerance_seconds integer NOT NULL,
        max_body_bytes integer NOT NULL,payload jsonb NOT NULL,disabled boolean NOT NULL DEFAULT false,
        UNIQUE({key},id),FOREIGN KEY({key},activation_id) REFERENCES activation_revisions({key},id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id),CHECK((activation_id IS NULL)<>(run_id IS NULL)))""")
    op.execute(f"""CREATE TABLE trigger_receipts(id uuid PRIMARY KEY,{scope},trigger_id uuid NOT NULL,
        event_id text NOT NULL,request_hash text NOT NULL,run_id uuid NOT NULL,payload jsonb NOT NULL,
        UNIQUE(trigger_id,event_id),FOREIGN KEY({key},trigger_id) REFERENCES trigger_routes({key},id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id))""")
    for table in ("activation_connector_releases", "trigger_routes", "trigger_receipts"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(disabled) ON trigger_routes TO weave_app")
    op.execute("DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='weave_trigger_reader') "
        "THEN CREATE ROLE weave_trigger_reader NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$")
    op.execute("GRANT USAGE ON SCHEMA public TO weave_trigger_reader")
    op.execute("GRANT SELECT(id,tenant_id,project_id,environment_id,secret_ref,tolerance_seconds,max_body_bytes,disabled) "
        "ON trigger_routes TO weave_trigger_reader")
    op.execute("CREATE POLICY ingress_lookup ON trigger_routes FOR SELECT TO weave_trigger_reader USING (true)")
    op.execute("""CREATE FUNCTION weave_trigger_route(identifier uuid)
        RETURNS TABLE(id uuid,tenant_id uuid,project_id uuid,environment_id uuid,secret_ref text,
            tolerance_seconds integer,max_body_bytes integer)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        SELECT id,tenant_id,project_id,environment_id,secret_ref,tolerance_seconds,max_body_bytes
        FROM public.trigger_routes WHERE id=identifier AND NOT disabled $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_trigger_reader")
    op.execute("ALTER FUNCTION weave_trigger_route(uuid) OWNER TO weave_trigger_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_trigger_reader")
    op.execute("REVOKE ALL ON FUNCTION weave_trigger_route(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_trigger_route(uuid) TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0009_triggers'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
