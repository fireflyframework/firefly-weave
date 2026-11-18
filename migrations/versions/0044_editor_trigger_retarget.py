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

"""Moving a trigger to another activation of its workflow: revisions on webhooks, Kafka triggers, app event sources
and email sources, the column updates a retarget makes, and one live source binding per source.

A retarget changes a source's activation and the person it runs as, so the app may now update those columns, and
the stored ``payload`` that mirrors them, on the four source tables and on platform-event triggers. Nothing else
about a source becomes writable. Each of the four tables gains ``revision``, which a retarget or a disable
increments, so a change prepared from an older read is refused. A Kafka trigger or app event source that changes
hands gets a new source binding and the old one is revoked: a source still has at most one live binding, and
revoked ones stay as history. Existing rows read revision 1, which costs nothing toward usage, so usage charged
before this migration stays exact; nothing is backfilled.

A draft test activation can listen for test events through a webhook route on the test activation. When the sweep
purges a retired test activation, ``weave_purge_test_activation`` now deletes its listen routes too; its owner may
delete only routes of test activations.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_editor_trigger_retarget"
down_revision = "0042_operations_alerts"

SOURCES = ("trigger_routes", "broker_routes", "provider_sources", "email_sources")
PURGE = "public.weave_purge_test_activation(uuid,uuid,uuid,uuid)"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
UPDATES = {
    "trigger_routes": "activation_id,principal_id,payload,revision",
    "broker_routes": "activation_id,principal_id,binding_id,payload,revision",
    "provider_sources": "activation_id,principal_id,binding_id,payload,revision",
    "event_triggers": "activation_id,principal_id",
}


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or applied in body:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the trigger retarget migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def revisions() -> None:
    """A source's revision counts its changes; a column with a constant default changes only the catalog."""
    for table in SOURCES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0)")
    for table, columns in UPDATES.items():
        op.execute(f"GRANT UPDATE({columns}) ON {table} TO weave_app")


def live_bindings() -> None:
    """At most one live binding per source; a revoked binding no longer blocks its source's next one."""
    connection = op.get_bind()
    names = connection.scalars(
        sa.text(
            "SELECT conname FROM pg_constraint WHERE conrelid='connection_source_bindings'::regclass "
            "AND contype='u' AND pg_get_constraintdef(oid)="
            "'UNIQUE (tenant_id, project_id, environment_id, source_kind, source_id)'"
        )
    ).all()
    if len(names) != 1:
        raise RuntimeError("Unexpected source binding constraints; reconcile the trigger retarget migration")
    op.execute(
        "CREATE UNIQUE INDEX connection_source_bindings_live ON connection_source_bindings"
        "(tenant_id,project_id,environment_id,source_kind,source_id) WHERE NOT revoked"
    )
    op.execute(f'ALTER TABLE connection_source_bindings DROP CONSTRAINT "{names[0]}"')


def row_accounting() -> None:
    """A source at revision 1 costs what it cost before; a higher revision is charged with the change that set it."""
    anchor = "IF item IS NULL OR item='{}'::jsonb THEN RETURN item; END IF;"
    tables = ",".join(f"'{table}'" for table in SOURCES)
    patch_function(
        "public.weave_charged_row(text,jsonb)",
        anchor,
        anchor + f"\n        IF relation IN ({tables}) AND item->'revision'='1'::jsonb THEN "
        "item:=item-'revision'; END IF;",
        applied="item-'revision'",
    )


def listen_routes() -> None:
    """A purged test activation takes its listen routes with it, and its purge may delete nothing else.

    A route is stopped before its activation is purged, so its reserve was already released, as a purged run's is.
    """
    op.execute("GRANT DELETE ON trigger_routes TO weave_retention_owner")
    op.execute(
        f"CREATE POLICY test_listen_purge ON trigger_routes FOR DELETE TO weave_retention_owner USING ({EXACT} "
        "AND EXISTS(SELECT 1 FROM public.activation_revisions a WHERE a.tenant_id=trigger_routes.tenant_id "
        "AND a.project_id=trigger_routes.project_id AND a.environment_id=trigger_routes.environment_id "
        "AND a.id=trigger_routes.activation_id AND a.test))"
    )
    anchor = "DELETE FROM public.activation_revisions"
    patch_function(
        PURGE,
        anchor,
        "DELETE FROM public.trigger_routes WHERE tenant_id=t AND project_id=p AND environment_id=e "
        "AND activation_id=target;\n        " + anchor,
        applied="public.trigger_routes",
    )


def upgrade() -> None:
    revisions()
    live_bindings()
    row_accounting()
    listen_routes()
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
