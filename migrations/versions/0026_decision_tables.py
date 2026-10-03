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

"""Admit immutable decision definitions under existing project-scoped catalog policies."""

from alembic import op

revision = "0026_decision_tables"
down_revision = "0025_run_lifecycle"


def upgrade():
    op.execute("ALTER TABLE definition_versions DROP CONSTRAINT definition_versions_kind_check")
    op.execute(
        "ALTER TABLE definition_versions ADD CONSTRAINT definition_versions_kind_check "
        "CHECK(kind IN ('Workflow','Action','Connector','DecisionTable'))"
    )
    op.execute("UPDATE weave_schema_version SET version='0026_decision_tables'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
