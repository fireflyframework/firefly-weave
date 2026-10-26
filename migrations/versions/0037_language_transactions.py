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

"""Workflow transactions: settlement work, what worker releases say about failures, and the transaction record.

Settlement work (a transaction's confirms, cancels and undos, and steps still in flight while it decides or goes
back) is marked on its task and call rows, so claims and scans run it while the run is paused or held by another
incident. ``run_transactions`` projects each transaction of a run for reads, and a run cannot be purged while one
of its transactions is unsettled. No column is added to ``runs`` and nothing is backfilled: no transaction exists
before this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_language_transactions"
down_revision = "0036_editor_studio_documents"

SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
SCOPE_KEY = "tenant_id,project_id,environment_id"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
STATES = (
    "'open','deciding','committing','completed','compensating','compensated','cancelled','in_doubt',"
    "'reconciled','force_ended'"
)
UNSETTLED = "'open','deciding','committing','compensating','in_doubt'"
PURGE = "public.weave_purge_run(uuid,integer)"
PURGE_ANCHOR = "DELETE FROM public.runs WHERE id=target;"
PURGE_RULES = f"""IF EXISTS(SELECT 1 FROM public.run_transactions WHERE run_id=target AND state IN ({UNSETTLED})) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Transaction not settled'; END IF;
        DELETE FROM public.run_transactions WHERE run_id=target;
        """
ROLES = (
    "platform_admin",
    "tenant_admin",
    "developer",
    "deployer",
    "operator",
    "viewer",
    "worker",
    "task_participant",
    "task_manager",
    "email_reader",
    "email_sender",
    "email_manager",
    "execution_manager",
    "lumi_user",
    "lumi_manager",
    "file_reader",
    "file_manager",
    "deployment_reader",
    "deployment_planner",
    "deployment_approver",
    "deployment_operator",
    "deployment_runner",
    "worker_operator",
    "transaction_reconciler",
)


def settlement() -> None:
    # Adding a column with a constant default changes only the catalog, whatever the table's size.
    op.execute("ALTER TABLE task_intents ADD COLUMN settlement boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE run_calls ADD COLUMN settlement boolean NOT NULL DEFAULT false")
    # A called participant's outcome: whether the child dispatched anything that may have applied.
    op.execute("ALTER TABLE run_calls ADD COLUMN effect text CHECK(effect IN ('none','unknown'))")
    op.execute("ALTER TABLE task_leases ADD COLUMN transaction_aware boolean NOT NULL DEFAULT false")
    op.execute("GRANT UPDATE(settlement) ON task_intents TO weave_app")
    # A transaction's hold or settle deadline is armed again on its one row when an earlier time is needed.
    op.execute("GRANT UPDATE(deadline) ON run_deadlines TO weave_app")
    # An in-doubt incident replaces the interim failure incident at the same key and keeps the failure behind it.
    op.execute("GRANT UPDATE(origin_code) ON incidents TO weave_app")
    op.execute("GRANT UPDATE(settlement,effect) ON run_calls TO weave_app")
    # Claims of settlement work and the delivery of revoked participant tasks read through these indexes.
    op.execute(
        f"CREATE INDEX task_intents_settlement ON task_intents({SCOPE_KEY},id) WHERE settlement AND status='ready'"
    )
    op.execute("CREATE INDEX task_intents_revoked ON task_intents(run_id) WHERE status='revoked'")


def awareness() -> None:
    """What each worker release declared, once, about reporting failures that applied nothing."""
    op.execute(f"""CREATE TABLE worker_release_awareness(
        {SCOPE_COLUMNS},release_id uuid PRIMARY KEY,transaction_aware boolean NOT NULL,declared_at timestamptz NOT NULL,
        FOREIGN KEY({SCOPE_KEY},release_id) REFERENCES worker_releases({SCOPE_KEY},id))""")
    tenant = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute("ALTER TABLE worker_release_awareness ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE worker_release_awareness FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_scope ON worker_release_awareness USING ({tenant}) WITH CHECK ({tenant})")
    op.execute("GRANT SELECT,INSERT ON worker_release_awareness TO weave_app")
    op.execute(
        "CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON worker_release_awareness "
        "FOR EACH ROW EXECUTE FUNCTION weave_usage_charge('release_id')"
    )


def projection() -> None:
    op.execute(f"""CREATE TABLE run_transactions(
        {SCOPE_COLUMNS},run_id uuid NOT NULL,instance_key text NOT NULL,node_id text NOT NULL,
        state text NOT NULL CHECK(state IN ({STATES})),business_key text CHECK(char_length(business_key) <= 200),
        cause jsonb,participants jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(participants)='array'),
        revision integer NOT NULL CHECK(revision>0),opened_at timestamptz NOT NULL,decided_at timestamptz,
        ended_at timestamptz,PRIMARY KEY(run_id,instance_key),
        FOREIGN KEY({SCOPE_KEY},run_id) REFERENCES runs({SCOPE_KEY},id))""")
    op.execute(
        f"CREATE INDEX run_transactions_unsettled ON run_transactions({SCOPE_KEY},state,opened_at) "
        "WHERE state IN ('in_doubt','compensating','deciding','committing')"
    )
    op.execute(
        f"CREATE INDEX run_transactions_business_key ON run_transactions({SCOPE_KEY},business_key) "
        "WHERE business_key IS NOT NULL"
    )
    op.execute("ALTER TABLE run_transactions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_transactions FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON run_transactions USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute("GRANT SELECT,INSERT ON run_transactions TO weave_app")
    op.execute(
        "GRANT UPDATE(state,business_key,cause,participants,revision,decided_at,ended_at) ON run_transactions "
        "TO weave_app"
    )
    op.execute("GRANT SELECT,DELETE ON run_transactions TO weave_retention_owner")
    op.execute(
        "CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON run_transactions "
        "FOR EACH ROW EXECUTE FUNCTION weave_usage_charge('run_id','instance_key')"
    )
    op.execute(f"ALTER TABLE run_facts ADD COLUMN transaction_state text CHECK(transaction_state IN ({STATES}))")


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str | None = None) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or (applied is not None and applied in body):
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the workflow transaction migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def row_accounting() -> None:
    """New columns cost nothing while they hold their default, so rows charged before this migration stay exact.

    Usage charges a row's JSON size. A column added to a charged table would grow every existing row's size without
    a charge; recharging every row would take time in proportion to the data. Instead a column counts only once it
    holds something else, which that row's own charge then pays for. Charges and recounts both measure through
    ``weave_charged_row``.
    """
    op.execute("""CREATE FUNCTION weave_charged_row(relation text,item jsonb) RETURNS jsonb
      LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,public AS $$
      BEGIN
        IF item IS NULL OR item='{}'::jsonb THEN RETURN item; END IF;
        IF relation IN ('task_intents','run_calls') AND item->'settlement'='false'::jsonb THEN
          item:=item-'settlement'; END IF;
        IF relation='run_calls' AND item->'effect'='null'::jsonb THEN item:=item-'effect'; END IF;
        IF relation='task_leases' AND item->'transaction_aware'='false'::jsonb THEN
          item:=item-'transaction_aware'; END IF;
        IF relation='run_facts' AND item->'transaction_state'='null'::jsonb THEN
          item:=item-'transaction_state'; END IF;
        RETURN item;
      END $$""")
    op.execute("REVOKE ALL ON FUNCTION weave_charged_row(text,jsonb) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION weave_charged_row(text,jsonb) TO weave_retention_owner")
    anchor = "IF item IS NULL OR item='{}'::jsonb THEN RETURN result; END IF;"
    patch_function(
        "public.weave_row_metrics(text,jsonb)",
        anchor,
        anchor + "\n        item:=public.weave_charged_row(relation,item);",
        applied="weave_charged_row",
    )
    anchor = "item:=CASE WHEN TG_OP='DELETE' THEN old_row ELSE new_row END;"
    patch_function(
        "public.weave_usage_charge()",
        anchor,
        "old_row:=public.weave_charged_row(TG_TABLE_NAME,old_row);\n        "
        "new_row:=public.weave_charged_row(TG_TABLE_NAME,new_row);\n        " + anchor,
        applied="weave_charged_row",
    )


def control_accounting() -> None:
    """A run's stop spends its own control reserve on its transaction records, like on its other rows.

    The reserve holds room to rewrite every unsettled transaction record of the run once: its participants, plus a
    kibibyte for its state, cause and timestamps.
    """
    anchor = "IF relation_name='runs' THEN RETURN (item->>'id')::uuid; END IF;"
    patch_function(
        "public.weave_control_target(text,jsonb)",
        anchor,
        "IF relation_name='run_transactions' THEN RETURN (item->>'run_id')::uuid; END IF;\n      " + anchor,
    )
    anchor = "+576*affected"
    predicate = "tenant_id=tenant AND project_id=project AND environment_id=(item->>'environment_id')::uuid"
    patch_function(
        "public.weave_reserve_refresh(text,uuid,boolean)",
        anchor,
        anchor
        + f"""
            +(SELECT coalesce(sum(1024+octet_length(participants::text)),0) FROM public.run_transactions
              WHERE {predicate} AND run_id=identifier AND state IN ({UNSETTLED}))""",
    )
    anchor = "'run_facts','step_facts','task_facts','incident_facts','run_calls','run_call_links') THEN"
    patch_function(
        "public.weave_reservation_touch(text,jsonb)",
        anchor,
        "'run_facts','step_facts','task_facts','incident_facts','run_calls','run_call_links','run_transactions') THEN",
    )


def upgrade() -> None:
    settlement()
    awareness()
    projection()
    row_accounting()
    control_accounting()
    patch_function(PURGE, PURGE_ANCHOR, PURGE_RULES + PURGE_ANCHOR, applied="Transaction not settled")
    accepted = ",".join("'" + role + "'" for role in ROLES)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    op.execute("ANALYZE run_transactions")
    op.execute("ANALYZE worker_release_awareness")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
