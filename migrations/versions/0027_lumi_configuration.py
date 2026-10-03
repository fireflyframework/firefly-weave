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

"""Environment-only assistant configuration; no conversations or request payloads."""

from alembic import op

revision = "0027_lumi_configuration"
down_revision = "0026_decision_tables"

# Version-local catalog: later application roles must not rewrite migration history.
ROLES = (
    "tenant_admin", "developer", "deployer", "operator", "viewer", "worker",
    "task_participant", "task_manager", "email_reader", "email_sender", "email_manager",
    "execution_manager", "lumi_user", "lumi_manager", "file_reader", "file_manager",
)


def upgrade():
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    accepted = ",".join(repr(role) for role in ROLES)
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    op.execute("""CREATE TABLE lumi_configurations(
        tenant_id uuid NOT NULL, project_id uuid NOT NULL, environment_id uuid NOT NULL,
        revision integer NOT NULL CHECK(revision>0), payload jsonb NOT NULL,
        PRIMARY KEY(tenant_id,project_id,environment_id),
        FOREIGN KEY(tenant_id,project_id,environment_id) REFERENCES environments(tenant_id,project_id,id))""")
    condition = " AND ".join(f"{column}=nullif(current_setting('weave.{column}',true),'')::uuid"
        for column in ("tenant_id", "project_id", "environment_id"))
    op.execute("ALTER TABLE lumi_configurations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lumi_configurations FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON lumi_configurations USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT,UPDATE ON lumi_configurations TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0027_lumi_configuration'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
