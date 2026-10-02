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

"""Exact scoped run discovery indexes; business keys do not impose uniqueness."""

from alembic import op

revision = "0024_run_filters"
down_revision = "0023_email"


def upgrade():
    for name, column, key in (
        ("business", "request", "business_key"),
        ("correlation", "request", "correlation_key"),
        ("status", "state", "status"),
    ):
        op.execute(
            f"CREATE INDEX runs_{name}_page ON runs(tenant_id,project_id,environment_id,({column}->>'{key}'),id)"
        )
    op.execute("UPDATE weave_schema_version SET version='0024_run_filters'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
