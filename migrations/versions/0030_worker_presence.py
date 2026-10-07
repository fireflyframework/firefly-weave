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

"""Scoped observed worker presence and revisioned drain control without lease revocation."""

from alembic import op

revision = "0030_worker_presence"
down_revision = "0029_deployments"


def upgrade():
    op.execute(
        "ALTER TABLE worker_instances ADD COLUMN last_seen_at timestamptz, "
        "ADD COLUMN draining boolean NOT NULL DEFAULT false, "
        "ADD COLUMN control_revision bigint NOT NULL DEFAULT 1 CHECK(control_revision>0)"
    )
    op.execute("GRANT UPDATE(last_seen_at,draining,control_revision) ON worker_instances TO weave_app")
    op.execute("DROP POLICY tenant_scope ON worker_instances")
    condition = (
        "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND "
        "project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND "
        "environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
    )
    op.execute(
        f"CREATE POLICY exact_scope ON worker_instances TO weave_app USING ({condition}) WITH CHECK ({condition})"
    )
    op.execute("UPDATE weave_schema_version SET version='0030_worker_presence'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
