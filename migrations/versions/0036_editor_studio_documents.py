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

"""Studio documents: a platform draft's canvas and test data, the latest two revisions of each, per tenant."""

from alembic import op

revision = "0036_editor_studio_documents"
down_revision = "0035_principal_idempotency"

TENANT = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"


def upgrade() -> None:
    # No foreign key to drafts: draft revisions are append-only rows, and retiring a draft deletes its documents
    # in the same transaction (definitions/service.py retire_draft).
    op.execute("""CREATE TABLE studio_documents(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,draft_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('canvas','test-data')),
        revision integer NOT NULL CHECK(revision>=1),draft_revision integer NOT NULL CHECK(draft_revision>=1),
        document jsonb NOT NULL CHECK(jsonb_typeof(document)='object'),
        contains_run_data boolean NOT NULL DEFAULT false,
        source_environment_ids uuid[] NOT NULL DEFAULT '{}',
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_by uuid NOT NULL,
        PRIMARY KEY(tenant_id,project_id,draft_id,kind,revision))""")
    op.execute("ALTER TABLE studio_documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE studio_documents FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_scope ON studio_documents USING ({TENANT}) WITH CHECK ({TENANT})")
    # Saves add a revision and delete all but the latest two; retirement deletes the rest. Rows never change.
    op.execute("GRANT SELECT,INSERT,DELETE ON studio_documents TO weave_app")
    op.execute("GRANT SELECT,DELETE ON studio_documents TO weave_retention_owner")
    op.execute(
        "CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON studio_documents FOR EACH ROW "
        "EXECUTE FUNCTION weave_usage_charge('tenant_id','project_id','draft_id','kind','revision')"
    )
    op.execute("ANALYZE studio_documents")
    op.execute("UPDATE weave_schema_version SET version='0036_editor_studio_documents'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
