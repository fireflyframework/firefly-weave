# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Retained scoped Teams revocation fences, lifecycle evidence and administrative receipts."""

from alembic import op

revision = "0019_teams_references"
down_revision = "0018_provider_inbox"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(
        f"CREATE TABLE teams_references(id uuid PRIMARY KEY,{scope},source_id uuid NOT NULL,generation integer NOT NULL CHECK(generation>0),state text NOT NULL CHECK(state IN ('active','revoked')),payload jsonb NOT NULL,UNIQUE({key},id),FOREIGN KEY({key},source_id) REFERENCES provider_sources({key},id))"
    )
    op.execute(
        f"CREATE TABLE teams_lifecycle_events({scope},reference_id uuid NOT NULL,kind text NOT NULL,event_id text NOT NULL,fingerprint text NOT NULL,PRIMARY KEY({key},reference_id,kind,event_id),FOREIGN KEY({key},reference_id) REFERENCES teams_references({key},id))"
    )
    op.execute(
        f"CREATE TABLE teams_reference_commands({scope},request_id uuid NOT NULL,fingerprint text NOT NULL,result jsonb NOT NULL,PRIMARY KEY({key},request_id))"
    )
    for table in ("teams_references", "teams_lifecycle_events", "teams_reference_commands"):
        condition = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY teams_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute("GRANT UPDATE(source_id,generation,state,payload) ON teams_references TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0019_teams_references'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
