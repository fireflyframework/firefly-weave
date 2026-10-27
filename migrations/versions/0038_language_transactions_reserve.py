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

"""Workflow transactions settle at a full quota, a person can retry a stuck undo, and records name their incident.

Undoing a transaction is part of stopping a run, so its work spends the run's control reserve, as a stop does. A
stop must leave the run inactive; settlement work leaves it running, so its control context is marked as settling:
the reserve it spent is refreshed when its transaction commits, without the stop's check. Retrying a stuck undo gives
its task a new deadline, so the task's payload becomes updatable. ``run_transactions.incident_key`` names the latest
in-doubt incident of a transaction, so incidents and reads find each other without the run's state, and the
compatibility inventory counts transactions in doubt and undoing. Adding columns with no default changes only the
catalog, whatever the size of the tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_language_transactions_reserve"
down_revision = "0037_language_transactions"


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or applied in body:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the transaction reserve migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def settling_control() -> None:
    """``weave_settle_begin`` spends a running run's reserve on settlement work; the stop's check is kept for stops."""
    op.execute("ALTER TABLE operation_control_context ADD COLUMN settles boolean NOT NULL DEFAULT false")
    op.execute("""CREATE FUNCTION weave_settle_begin(identifier uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE item jsonb; tenant uuid; project uuid; BEGIN
      item:=public.weave_resource('run',identifier);
      tenant:=nullif(current_setting('weave.tenant_id',true),'')::uuid;
      project:=nullif(current_setting('weave.project_id',true),'')::uuid;
      IF item IS NULL OR (item->>'tenant_id')::uuid IS DISTINCT FROM tenant OR
        (item->>'project_id')::uuid IS DISTINCT FROM project OR NOT public.weave_operation_fenced(tenant,project)
        OR NOT public.weave_resource_active('run',item) OR NOT EXISTS(
          SELECT 1 FROM public.operation_reservations WHERE kind='run' AND resource_id=identifier AND amount>0) THEN
        RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Reserved control unavailable'; END IF;
      IF (SELECT count(*) FROM public.operation_control_context WHERE transaction_id=txid_current())>=100 THEN
        RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Control transaction limit'; END IF;
      -- A stop already begun in this transaction keeps its check: the run must end.
      INSERT INTO public.operation_control_context VALUES(txid_current(),tenant,project,'run',identifier,true)
        ON CONFLICT DO NOTHING;
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
    op.execute("ALTER FUNCTION weave_settle_begin(uuid) OWNER TO weave_retention_owner")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
    op.execute("REVOKE ALL ON FUNCTION weave_settle_begin(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_settle_begin(uuid) TO weave_app")
    active = "public.weave_resource_active(c.kind,public.weave_resource(c.kind,c.resource_id)) THEN"
    patch_function(
        "public.weave_control_finish()", active, "(NOT c.settles AND " + active[:-5] + ") THEN", applied="settles"
    )
    # Settlement work refreshes its reserve whatever the quota: the next stop or undo of the run needs it.
    refresh = "PERFORM public.weave_reserve_refresh(c.kind,c.resource_id);"
    patch_function(
        "public.weave_control_finish()",
        refresh,
        "PERFORM public.weave_reserve_refresh(c.kind,c.resource_id,NOT c.settles);",
        applied="NOT c.settles);",
    )


def records() -> None:
    op.execute("GRANT UPDATE(payload) ON task_intents TO weave_app")
    op.execute("ALTER TABLE run_transactions ADD COLUMN incident_key text CHECK(char_length(incident_key) <= 600)")
    op.execute("GRANT UPDATE(incident_key) ON run_transactions TO weave_app")
    # A record charged before this migration had no incident key: the column counts once it holds one.
    anchor = "IF relation='run_calls' AND item->'effect'='null'::jsonb THEN item:=item-'effect'; END IF;"
    patch_function(
        "public.weave_charged_row(text,jsonb)",
        anchor,
        anchor + "\n        IF relation='run_transactions' AND item->'incident_key'='null'::jsonb THEN "
        "item:=item-'incident_key'; END IF;",
        applied="incident_key",
    )


def inventory() -> None:
    """The compatibility inventory counts transactions in doubt and transactions undoing, across tenants."""
    op.execute(
        "CREATE POLICY operations_owner ON run_transactions TO weave_retention_owner USING(true) WITH CHECK(true)"
    )
    anchor = "FROM public.operation_usage"
    patch_function(
        "public.weave_compatibility_counts()",
        anchor,
        "|| jsonb_build_object("
        "'transaction.in_doubt',(SELECT count(*) FROM public.run_transactions WHERE state='in_doubt'),"
        "'transaction.compensating',(SELECT count(*) FROM public.run_transactions WHERE state='compensating')) "
        + anchor,
        applied="transaction.in_doubt",
    )


def upgrade() -> None:
    settling_control()
    records()
    inventory()
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
