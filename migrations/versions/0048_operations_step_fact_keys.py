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

"""Step facts under an activation in byte order, so the agent log reads them as one key range.

The agent log reads the facts under each activation it shows: the keys that start with the activation's key and
``#`` (its turns, calls and reviews). The primary key orders ``instance_key`` by the database's collation, so the read
went through every fact of the step in the run; and under a glibc locale ``#`` and ``$`` weigh nothing at the first
level, so a range in that collation would miss keys or add others. This index orders the keys by their bytes
(collation "C"), where the keys that start with ``k#`` are exactly those from ``k#`` up to ``k$``. It holds only the
keys with a ``#`` (agent turns, calls and reviews, and transaction phases), so steps without them add nothing to it.

Building it reads ``step_facts`` once under a SHARE lock: writes of step facts, and the run transitions that make
them, wait for the build. No column or row changes.
"""

from alembic import op

revision = "0048_operations_step_fact_keys"
down_revision = "0047_event_trigger_purge"


def upgrade() -> None:
    op.execute(
        'CREATE INDEX step_facts_key_range ON step_facts(run_id,node_id,instance_key COLLATE "C") '
        "WHERE strpos(instance_key,'#')>0"
    )
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
