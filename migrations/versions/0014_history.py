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

"""Optional immutable evidence bound to exact scoped accepted facts."""
from alembic import op

revision = "0014_history"
down_revision = "0013_debug"


def upgrade():
    key = "tenant_id,project_id,environment_id,run_id,id,sequence"
    op.execute(f"ALTER TABLE run_events ADD UNIQUE({key})")
    op.execute(f"""CREATE TABLE run_event_evidence(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        run_id uuid NOT NULL,id uuid NOT NULL,sequence bigint NOT NULL,
        evidence jsonb NOT NULL CHECK(octet_length(evidence::text)<=16777216),
        PRIMARY KEY(run_id,id),FOREIGN KEY({key}) REFERENCES run_events({key}))""")
    op.execute("ALTER TABLE run_event_evidence ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_event_evidence FORCE ROW LEVEL SECURITY")
    condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON run_event_evidence USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT ON run_event_evidence TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0014_history'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
