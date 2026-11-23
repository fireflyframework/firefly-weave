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

"""Purging a run removes what its platform events left in the trigger tables.

An event offered to triggers is stored once in ``event_trigger_payloads`` with its source run's transaction facts
(business key, run and incident IDs), and ``event_trigger_starts`` records what each trigger did with it. Until now a
purge left both behind. Now ``weave_purge_run``:

- refuses while one of the run's events still has a requested start, since that start reads the event when it runs;
  such a start runs, or expires a day after its event;
- deletes the start rows of the run's events that name no run still kept, and the start that started the run once
  the run whose event it was is purged too;
- deletes each payload that no start names any more, of the run's events and of the event that started the run, and
  empties the run's other payloads to ``{}``.

A start that started a run still kept stays: the trigger's history shows that run, and the chain's budget of starts
counts it while the chain may still grow. Its payload row stays because the start's foreign key names it, but holds
nothing the purged run recorded. Both rows go when the started run is purged.

The purge finds a run's payloads by their source run and a payload's starts by their event, so two indexes are added;
the second also serves the foreign key check when a payload is deleted. Building them reads both tables under a SHARE
lock, so new trigger starts wait for the builds. No column or row changes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_event_trigger_purge"
down_revision = "0046_operations_log_requests"

PURGE = "public.weave_purge_run(uuid,integer)"
PURGE_ANCHOR = "DELETE FROM public.runs WHERE id=target;"
SAME_EVENT = (
    "s.tenant_id=p.tenant_id AND s.project_id=p.project_id AND s.environment_id=p.environment_id "
    "AND s.event_id=p.event_id"
)
PURGE_RULES = f"""IF EXISTS(SELECT 1 FROM public.event_trigger_payloads p JOIN public.event_trigger_starts s
          ON {SAME_EVENT} WHERE p.source_run_id=target AND s.status='requested') THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Event trigger start pending'; END IF;
        DECLARE started_by uuid; BEGIN
          DELETE FROM public.event_trigger_starts s USING public.event_trigger_payloads p
            WHERE p.source_run_id=target AND {SAME_EVENT}
            AND (s.run_id IS NULL OR NOT EXISTS(SELECT 1 FROM public.runs r WHERE r.id=s.run_id));
          DELETE FROM public.event_trigger_starts s WHERE s.run_id=target
            AND NOT EXISTS(SELECT 1 FROM public.runs r WHERE r.id=s.source_run_id) RETURNING s.event_id INTO started_by;
          DELETE FROM public.event_trigger_payloads p WHERE p.source_run_id=target
            AND NOT EXISTS(SELECT 1 FROM public.event_trigger_starts s WHERE {SAME_EVENT});
          DELETE FROM public.event_trigger_payloads p WHERE p.tenant_id=tenant AND p.project_id=project
            AND p.environment_id=environment AND p.event_id=started_by
            AND NOT EXISTS(SELECT 1 FROM public.event_trigger_starts s WHERE {SAME_EVENT});
          UPDATE public.event_trigger_payloads SET payload='{{}}' WHERE source_run_id=target;
        END;
        """


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or applied in body:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the event trigger purge migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def upgrade() -> None:
    # A run's payloads, by the run whose events they hold.
    op.execute("CREATE INDEX event_trigger_payloads_source ON event_trigger_payloads(source_run_id)")
    # An event's starts: the purge reads them, and deleting a payload checks that none still names it.
    op.execute("CREATE INDEX event_trigger_starts_event ON event_trigger_starts(event_id)")
    op.execute("GRANT SELECT,DELETE ON event_trigger_starts TO weave_retention_owner")
    op.execute("GRANT SELECT,UPDATE(payload),DELETE ON event_trigger_payloads TO weave_retention_owner")
    patch_function(PURGE, PURGE_ANCHOR, PURGE_RULES + PURGE_ANCHOR, applied="Event trigger start pending")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
