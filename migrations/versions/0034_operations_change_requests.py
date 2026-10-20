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

"""Change requests: one reviewed infrastructure change per target, kept 90 days after it ends."""

from alembic import op

revision = "0034_operations_change_requests"
down_revision = "0033_language_workflow_calls"

KEY = "tenant_id,project_id,environment_id"
ACTIVE = "'observing','ready_to_plan','awaiting_approval','approved','draining','applying'"
ENDED = "'succeeded','failed','reconciliation_required','expired','declined','cancelled','superseded'"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))


def upgrade():
    # The same shape and scope policy as the other deployment records, plus the state the inbox and the
    # one-active-change rule read, and the end time retention reads.
    op.execute(f"""CREATE TABLE deployment_change_requests(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL,
        target_id uuid NOT NULL,deployment_id uuid,
        revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        payload jsonb NOT NULL CHECK(octet_length(payload::text)<=262144),
        state text NOT NULL CHECK(state IN ({ACTIVE},{ENDED})),
        ended_at timestamptz,
        CHECK((state IN ({ENDED}))=(ended_at IS NOT NULL)),
        UNIQUE({KEY},id),FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id),
        FOREIGN KEY({KEY},target_id) REFERENCES deployment_targets({KEY},id))""")
    op.execute(f"CREATE INDEX deployment_change_requests_scope ON deployment_change_requests({KEY},target_id,id)")
    op.execute(
        f"CREATE UNIQUE INDEX deployment_change_requests_one_active ON deployment_change_requests(target_id) "
        f"WHERE state IN ({ACTIVE})"
    )
    op.execute(
        f"CREATE INDEX deployment_change_requests_ended ON deployment_change_requests({KEY},ended_at,id) "
        "WHERE ended_at IS NOT NULL"
    )
    op.execute("ALTER TABLE deployment_change_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE deployment_change_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY exact_scope ON deployment_change_requests TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})"
    )
    # DELETE serves retention only: the operations loop removes ended change requests after 90 days.
    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON deployment_change_requests TO weave_app")
    op.execute("ANALYZE deployment_change_requests")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
