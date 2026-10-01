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

"""Immutable metadata-only observations of unavailable legacy execution evidence."""

from alembic import op

revision = "0012_secret_admission"
down_revision = "0011_schedules"


def upgrade():
    op.execute("""CREATE TABLE run_policy_blocks(
        tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        run_id uuid PRIMARY KEY,reason_code text NOT NULL CHECK(reason_code='WV-LEGACY-UNAVAILABLE'),
        observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        terminal_signals_verified boolean NOT NULL DEFAULT false,
        FOREIGN KEY(tenant_id,project_id,environment_id,run_id)
        REFERENCES runs(tenant_id,project_id,environment_id,id))""")
    op.execute("ALTER TABLE run_policy_blocks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_policy_blocks FORCE ROW LEVEL SECURITY")
    condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON run_policy_blocks USING ({condition}) WITH CHECK ({condition})")
    op.execute("GRANT SELECT,INSERT ON run_policy_blocks TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0012_secret_admission'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
