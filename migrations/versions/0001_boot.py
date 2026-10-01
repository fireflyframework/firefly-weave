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

"""Adopt the B1 forward-only schema boundary without replacing retained state."""

import sqlalchemy as sa
from alembic import op

revision = "0001_boot"
down_revision = None


def upgrade():
    connection = op.get_bind()
    exists = connection.scalar(sa.text("SELECT to_regclass('public.weave_schema_version')"))
    if exists is None:
        op.execute(
            "CREATE TABLE weave_schema_version (singleton boolean PRIMARY KEY DEFAULT true "
            "CHECK(singleton), version text NOT NULL)"
        )
        op.execute("INSERT INTO weave_schema_version(version) VALUES('0001_boot')")
    elif connection.execute(sa.text("SELECT version FROM weave_schema_version")).scalars().all() != ["0001_boot"]:
        raise RuntimeError("Incompatible legacy schema")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
