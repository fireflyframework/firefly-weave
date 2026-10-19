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

"""Collation-safe accounting lookups so every charged write stays a primary-key lookup."""

from alembic import op

revision = "0031_operations_key_collation"
down_revision = "0030_worker_presence"

# The accounting trigger passes TG_TABLE_NAME (type name, collation "C") down the call chain, so
# PL/pgSQL gives every text parameter and variable collation "C". Compared with a default-collation
# key column, that rules out the primary key and turns each lookup into a scan of the whole table.
PATCHES = (
    (
        "weave_reserve_refresh(text,uuid,boolean)",
        "kind=kind_name AND resource_id=identifier",
        'kind=(kind_name COLLATE "default") AND resource_id=identifier',
        2,
    ),
    (
        "weave_allocation_delta(text,jsonb,jsonb,text[])",
        "WHERE relation=relation_name AND row_key=identity_key",
        'WHERE relation=(relation_name COLLATE "default") AND row_key=(identity_key COLLATE "default")',
        2,
    ),
)


def upgrade():
    for function, before, after, occurrences in PATCHES:
        # CREATE OR REPLACE keeps the owner, grants, and SET options of the shared function.
        op.execute(f"""DO $migration$ DECLARE body text; BEGIN
          SELECT pg_get_functiondef('public.{function}'::regprocedure) INTO body;
          IF (length(body)-length(replace(body,$p${before}$p$,'')))/length($p${before}$p$)<>{occurrences} THEN
            RAISE EXCEPTION 'Unexpected accounting function {function}; verify predecessor'; END IF;
          EXECUTE replace(body,$p${before}$p$,$p${after}$p$);
        END $migration$""")
    op.execute("UPDATE weave_schema_version SET version='0031_operations_key_collation'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
