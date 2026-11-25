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

"""An index of the unsettled transaction records, so the ones earlier versions left are found among the settled ones.

Until now the record of a transaction that ended with its loop iteration kept the state before its end. The operations
loop repairs such records from each run's history, and looks for them in the ended runs of one environment at a time.
A run keeps its settled records until it is purged, so without an index that search reads every record of the
environment; this one holds only the unsettled records, which are the running transactions and the few to repair.

Building it reads ``run_transactions`` once under a SHARE lock: changes of transaction records, and the run transitions
that make them, wait for the build. No column or row changes, and no record is repaired here.
"""

from alembic import op

revision = "0049_unsettled_transactions"
down_revision = "0048_operations_step_fact_keys"


def upgrade() -> None:
    op.execute(
        "CREATE INDEX run_transactions_held ON run_transactions(tenant_id,project_id,environment_id,run_id) "
        "WHERE state IN ('open','deciding','committing','compensating','in_doubt')"
    )
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
