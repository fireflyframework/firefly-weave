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

"""Bounded creator-owned project simulation checkpoints with immutable expiry."""
from alembic import op

revision = "0013_debug"
down_revision = "0012_secret_admission"


def upgrade():
    op.execute("""CREATE TABLE debug_sessions(
        tenant_id uuid NOT NULL, project_id uuid NOT NULL, id uuid PRIMARY KEY,
        creator_id uuid NOT NULL REFERENCES principals(id), revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
        created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
        expires_at timestamptz NOT NULL DEFAULT statement_timestamp()+interval '3600 seconds',
        state jsonb NOT NULL,
        FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id))""")
    op.execute("ALTER TABLE debug_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE debug_sessions FORCE ROW LEVEL SECURITY")
    condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON debug_sessions USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT ON debug_sessions TO weave_app")
    op.execute("GRANT INSERT(tenant_id,project_id,id,creator_id,state),UPDATE(state,revision) ON debug_sessions TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0013_debug'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
