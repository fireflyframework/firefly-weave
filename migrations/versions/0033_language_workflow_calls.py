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

"""Workflow calls: durable call rows of a calling run, the link of each child run to its call, and purge rules."""

import sqlalchemy as sa
from alembic import op

revision = "0033_language_workflow_calls"
down_revision = "0032_operations_facts"

SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
SCOPE_KEY = "tenant_id,project_id,environment_id"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
ENDED = "'succeeded','failed','cancelled','timed_out'"
STATUSES = f"'requested','started',{ENDED},'rejected','abandoned','superseded'"
PURGE = "public.weave_purge_run(uuid,integer)"
PURGE_ANCHOR = "DELETE FROM public.runs WHERE id=target;"
PURGE_RULES = f"""IF EXISTS(SELECT 1 FROM public.run_calls WHERE caller_run_id=target
          AND (status='requested' OR (status='started' AND mode='wait'))) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Child runs still active'; END IF;
        IF EXISTS(SELECT 1 FROM public.run_call_links l JOIN public.run_calls c
          ON c.caller_run_id=l.caller_run_id AND c.node_id=l.node_id AND c.generation=l.generation
          WHERE l.run_id=target AND c.mode='wait' AND NOT c.delivered AND c.status IN ({ENDED})) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Call result not yet delivered'; END IF;
        DELETE FROM public.run_calls WHERE caller_run_id=target;
        DELETE FROM public.run_call_links WHERE run_id=target;
        """


def tables() -> None:
    # No foreign key to the child: a child run is purged on its own, and its call row keeps the outcome.
    op.execute(f"""CREATE TABLE run_calls(
        {SCOPE_COLUMNS},id uuid PRIMARY KEY,caller_run_id uuid NOT NULL,node_id text NOT NULL,
        generation integer NOT NULL DEFAULT 0 CHECK(generation>=0),
        mode text NOT NULL CHECK(mode IN ('wait','detach')),on_failure text CHECK(on_failure IN ('stop','continue')),
        workflow_reference text NOT NULL,activation_id uuid NOT NULL,input jsonb NOT NULL,business_key text,
        depth integer NOT NULL CHECK(depth BETWEEN 1 AND 64),trace_context jsonb,
        status text NOT NULL CHECK(status IN ({STATUSES})),cancel_requested boolean NOT NULL DEFAULT false,
        delivered boolean NOT NULL DEFAULT false,child_run_id uuid UNIQUE,result jsonb,code text,
        requested_at timestamptz NOT NULL,started_at timestamptz,ended_at timestamptz,
        UNIQUE(caller_run_id,node_id,generation),CHECK((mode='wait')=(on_failure IS NOT NULL)),
        FOREIGN KEY({SCOPE_KEY},caller_run_id) REFERENCES runs({SCOPE_KEY},id))""")
    # No foreign key to the caller: the link outlives a purged caller, like a retry link's source.
    op.execute(f"""CREATE TABLE run_call_links(
        {SCOPE_COLUMNS},run_id uuid PRIMARY KEY,caller_run_id uuid NOT NULL,node_id text NOT NULL,
        generation integer NOT NULL CHECK(generation>=0),mode text NOT NULL CHECK(mode IN ('wait','detach')),
        depth integer NOT NULL CHECK(depth BETWEEN 1 AND 64),
        FOREIGN KEY({SCOPE_KEY},run_id) REFERENCES runs({SCOPE_KEY},id))""")


def indexes() -> None:
    # Every scan and every trigger lookup below reads through an index.
    op.execute(f"CREATE INDEX run_calls_requested ON run_calls({SCOPE_KEY},requested_at,id) WHERE status='requested'")
    op.execute(
        f"CREATE INDEX run_calls_cancelling ON run_calls({SCOPE_KEY},requested_at,id) "
        "WHERE cancel_requested AND status='started'"
    )
    op.execute(
        f"CREATE INDEX run_calls_undelivered ON run_calls(caller_run_id) "
        f"WHERE mode='wait' AND NOT delivered AND status IN ({ENDED})"
    )
    op.execute("CREATE INDEX run_call_links_caller ON run_call_links(caller_run_id)")


def secure(table: str, key: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute(f"GRANT SELECT,DELETE ON {table} TO weave_retention_owner")
    op.execute(
        f"CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION weave_usage_charge('{key}')"
    )


def purge() -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": PURGE}
    )
    if not isinstance(body, str) or body.count(PURGE_ANCHOR) != 1 or "Child runs still active" in body:
        raise RuntimeError("Unexpected definition of weave_purge_run; reconcile the workflow call migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(PURGE_ANCHOR, PURGE_RULES + PURGE_ANCHOR))


def upgrade() -> None:
    tables()
    indexes()
    secure("run_calls", "id")
    secure("run_call_links", "run_id")
    op.execute(
        "GRANT UPDATE(status,child_run_id,result,code,started_at,ended_at,delivered,cancel_requested) "
        "ON run_calls TO weave_app"
    )
    purge()
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    op.execute("ANALYZE run_calls")
    op.execute("ANALYZE run_call_links")
    op.execute("UPDATE weave_schema_version SET version='0033_language_workflow_calls'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
