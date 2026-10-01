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

"""Add complete events without inventing provenance for preexisting audit rows."""

from alembic import op

revision = "0003_access_audit"
down_revision = "0002_access"


def upgrade():
    op.execute("ALTER TABLE access_audit ADD COLUMN event jsonb")
    op.execute("UPDATE weave_schema_version SET version='0003_access_audit'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
