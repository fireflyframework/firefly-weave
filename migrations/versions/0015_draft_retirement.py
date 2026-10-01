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

"""Append-only draft retirement preserves every authored revision."""
from alembic import op

revision = "0015_draft_retirement"
down_revision = "0014_history"


def upgrade():
    op.execute("""CREATE TABLE draft_retirements(
        tenant_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
        document_revision bigint NOT NULL, revision bigint NOT NULL,
        principal_id uuid NOT NULL REFERENCES principals(id),
        retired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(tenant_id,project_id,id),
        CHECK(revision=document_revision+1),
        FOREIGN KEY(tenant_id,project_id,id,document_revision)
            REFERENCES draft_revisions(tenant_id,project_id,id,revision))""")
    op.execute("ALTER TABLE draft_retirements ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE draft_retirements FORCE ROW LEVEL SECURITY")
    condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON draft_retirements USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT ON draft_retirements TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0015_draft_retirement'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
