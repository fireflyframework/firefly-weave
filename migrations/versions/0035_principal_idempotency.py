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


"""Principal creation answers a repeated Idempotency-Key with the principal it created, platform-wide."""

from alembic import op

revision = "0035_principal_idempotency"
down_revision = "0034_operations_change_requests"


def upgrade():
    # Principals are platform records, so their idempotency store has no tenant scope: each key belongs to the
    # administrator who sent it. Stored answers are read and inserted, never rewritten; keys past their retention
    # period are deleted by the next keyed creation of the same administrator.
    op.execute(
        "CREATE TABLE principal_idempotency(principal_id uuid NOT NULL REFERENCES principals(id),"
        "operation text NOT NULL,idempotency_key text NOT NULL CHECK(char_length(idempotency_key) BETWEEN 1 AND 200),"
        "request_hash text NOT NULL,response jsonb NOT NULL CHECK(octet_length(response::text)<=4096),"
        "created_at timestamptz NOT NULL DEFAULT now(),"
        "PRIMARY KEY(principal_id,operation,idempotency_key))"
    )
    op.execute("GRANT SELECT,INSERT,DELETE ON principal_idempotency TO weave_app")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
