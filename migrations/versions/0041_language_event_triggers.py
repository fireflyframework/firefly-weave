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

"""Platform-event triggers: which events start a workflow, what each event started, and how deep a run sits in a chain.

``event_triggers`` binds an activation to platform events; a trigger is created and later disabled, never edited.
Disabling one is a safety control, so each enabled trigger holds a reserve, as webhook routes do, and a disable works
at a full quota. ``event_trigger_starts`` records each event offered to a trigger, written in the event's own commit:
when that commit stops a run, the record spends the run's control reserve, as its other rows do. Start records keep
no foreign key to the run they start or to the event, so a purged run is never blocked by its trigger's history.
``run_facts.event_depth`` counts reactions in a chain and costs nothing while it is 0, so rows charged before this
migration cost the same. Runs may come from a platform event. Tasks the platform runs itself are found through
their own index and never pinned to a worker release. Every change to an existing table adds a column with a constant
default, or a check that skips the rows already there, so none reads or rewrites a table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_language_event_triggers"
down_revision = "0040_editor_step_tests"

SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
SCOPE_KEY = "tenant_id,project_id,environment_id"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
EVENTS = (
    "'transaction.completed','transaction.compensated','transaction.cancelled','transaction.in_doubt',"
    "'transaction.reconciled','transaction.force_ended'"
)
ORIGINS = "'manual','webhook','schedule','broker','email','provider','retry','call','test','event'"
START_CODES = "'WV-TRIGGER-START','WV-TRIGGER-DISABLED','WV-TRIGGER-EVENT-DEPTH'"
RESOLVE_TASK_TYPE = "weave-platform.transaction-resolve"


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or applied in body:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the platform-event trigger migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def scoped(table: str, key: str, updatable: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT SELECT,INSERT ON {table} TO weave_app")
    op.execute(f"GRANT UPDATE({updatable}) ON {table} TO weave_app")
    op.execute(
        f"CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION weave_usage_charge('{key}')"
    )


def triggers() -> None:
    op.execute(f"""CREATE TABLE event_triggers(
        {SCOPE_COLUMNS},id uuid PRIMARY KEY,name text NOT NULL,activation_id uuid NOT NULL,workflow_name text NOT NULL,
        events text[] NOT NULL CHECK(cardinality(events) BETWEEN 1 AND 12 AND events <@ ARRAY[{EVENTS}]::text[]),
        source_workflows text[] CHECK(source_workflows IS NULL OR cardinality(source_workflows) BETWEEN 1 AND 100),
        may_reconcile boolean NOT NULL DEFAULT false,principal_id uuid NOT NULL REFERENCES principals(id),
        disabled boolean NOT NULL DEFAULT false,revision integer NOT NULL CHECK(revision>0),
        created_at timestamptz NOT NULL,disabled_at timestamptz,CHECK(disabled=(disabled_at IS NOT NULL)),
        UNIQUE({SCOPE_KEY},id),
        FOREIGN KEY({SCOPE_KEY},activation_id) REFERENCES activation_revisions({SCOPE_KEY},id))""")
    # Names are unique among enabled triggers; a disabled trigger frees its name and keeps its record.
    op.execute(
        f"CREATE UNIQUE INDEX event_triggers_enabled_name ON event_triggers({SCOPE_KEY},name) WHERE NOT disabled"
    )
    # Fan-out reads the enabled triggers of the event's environment in ID order.
    op.execute(f"CREATE INDEX event_triggers_enabled ON event_triggers({SCOPE_KEY},id) WHERE NOT disabled")
    op.execute(f"CREATE INDEX event_triggers_activation ON event_triggers({SCOPE_KEY},activation_id)")
    scoped("event_triggers", "id", "disabled,disabled_at,revision")
    # Reserve accounting reads the trigger it controls, as it reads webhook routes.
    op.execute("GRANT SELECT ON event_triggers TO weave_retention_owner")
    op.execute("CREATE POLICY operations_inventory ON event_triggers FOR SELECT TO weave_retention_owner USING(true)")


def starts() -> None:
    op.execute(f"""CREATE TABLE event_trigger_starts(
        {SCOPE_COLUMNS},id uuid PRIMARY KEY,trigger_id uuid NOT NULL,event_id uuid NOT NULL,
        event_type text NOT NULL CHECK(event_type IN ({EVENTS})),source_run_id uuid NOT NULL,
        depth integer NOT NULL CHECK(depth>=1),payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
        status text NOT NULL CHECK(status IN ('requested','started','rejected','skipped')),
        code text CHECK(code IN ({START_CODES})),cause text CHECK(char_length(cause) BETWEEN 1 AND 64),
        run_id uuid UNIQUE,requested_at timestamptz NOT NULL,started_at timestamptz,
        UNIQUE(trigger_id,event_id),
        CONSTRAINT event_trigger_starts_started_run CHECK((status='started')=(run_id IS NOT NULL)),
        CONSTRAINT event_trigger_starts_refusal_code CHECK(status IN ('requested','started') OR code IS NOT NULL),
        FOREIGN KEY({SCOPE_KEY},trigger_id) REFERENCES event_triggers({SCOPE_KEY},id))""")
    # The start scan reads requested starts oldest first; the starts listing pages one trigger newest first.
    op.execute(
        f"CREATE INDEX event_trigger_starts_requested ON event_trigger_starts({SCOPE_KEY},requested_at,id) "
        "WHERE status='requested'"
    )
    op.execute(
        f"CREATE INDEX event_trigger_starts_trigger ON event_trigger_starts({SCOPE_KEY},trigger_id,requested_at,id)"
    )
    scoped("event_trigger_starts", "id", "status,code,cause,run_id,started_at")


def run_chains() -> None:
    connection = op.get_bind()
    names = (
        connection.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid='run_facts'::regclass AND contype='c' "
                "AND pg_get_constraintdef(oid) LIKE '%origin%'"
            )
        )
        .scalars()
        .all()
    )
    if len(names) != 1:
        raise RuntimeError("Unexpected origin check on run_facts; reconcile the platform-event trigger migration")
    op.execute(f'ALTER TABLE run_facts DROP CONSTRAINT "{names[0]}"')
    # The new list holds every value the old one did: NOT VALID skips reading the table, and the check still holds for
    # every row written from now on.
    op.execute(f"ALTER TABLE run_facts ADD CONSTRAINT run_facts_origin_check CHECK(origin IN ({ORIGINS})) NOT VALID")
    # A constant default changes only the catalog, whatever the table's size.
    op.execute("ALTER TABLE run_facts ADD COLUMN event_depth integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE run_facts ADD CONSTRAINT run_facts_event_depth_check CHECK(event_depth>=0) NOT VALID")
    # A row charged before this migration had no depth: the column counts once a run sits in a chain.
    anchor = "item:=item-'transaction_state'; END IF;"
    patch_function(
        "public.weave_charged_row(text,jsonb)",
        anchor,
        anchor + "\n        IF relation='run_facts' AND item->'event_depth'='0'::jsonb THEN "
        "item:=item-'event_depth'; END IF;",
        applied="event_depth",
    )


def control_accounting() -> None:
    """Disabling a trigger spends its own reserve; a start written while its source run stops spends the run's."""
    anchor = (
        "WHEN 'subscription' THEN SELECT to_jsonb(r) INTO result FROM public.event_subscriptions r WHERE id=identifier;"
    )
    patch_function(
        "public.weave_resource(text,uuid)",
        anchor,
        anchor + "\n        WHEN 'event_trigger' THEN SELECT to_jsonb(r) INTO result FROM public.event_triggers r "
        "WHERE id=identifier;",
        applied="'event_trigger'",
    )
    anchor = "WHEN 'event_subscriptions' THEN 'subscription' END;"
    patch_function(
        "public.weave_reservation_touch(text,jsonb)",
        anchor,
        "WHEN 'event_subscriptions' THEN 'subscription' WHEN 'event_triggers' THEN 'event_trigger' END;",
        applied="'event_trigger'",
    )
    anchor = "WHEN 'subscription' THEN 'event_subscriptions' END THEN"
    patch_function(
        "public.weave_allocation_delta(text,jsonb,jsonb,text[])",
        anchor,
        "WHEN 'subscription' THEN 'event_subscriptions' WHEN 'event_trigger' THEN 'event_triggers' END THEN",
        applied="'event_trigger'",
    )
    anchor = "'broker.disable','subscription.disable')"
    patch_function(
        "public.weave_allocation_delta(text,jsonb,jsonb,text[])",
        anchor,
        "'broker.disable','subscription.disable','event_trigger.disable')",
        applied="'event_trigger.disable'",
    )
    anchor = "IF relation_name='runs' THEN RETURN (item->>'id')::uuid; END IF;"
    patch_function(
        "public.weave_control_target(text,jsonb)",
        anchor,
        "IF relation_name='event_trigger_starts' THEN RETURN (item->>'source_run_id')::uuid; END IF;\n      " + anchor,
        applied="'event_trigger_starts'",
    )


def platform_tasks() -> None:
    # Only the platform runs these tasks, so none may be pinned to a worker release: no worker can claim them.
    op.execute(
        "ALTER TABLE task_intents ADD CONSTRAINT task_intents_platform_unpinned "
        "CHECK(payload->>'task_type' NOT LIKE 'weave-platform.%' OR worker_release_id IS NULL) NOT VALID"
    )
    op.execute(
        f"CREATE INDEX task_intents_platform_ready ON task_intents({SCOPE_KEY},id) "
        f"WHERE status='ready' AND payload->>'task_type'='{RESOLVE_TASK_TYPE}'"
    )


def upgrade() -> None:
    triggers()
    starts()
    run_chains()
    control_accounting()
    platform_tasks()
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    op.execute("ANALYZE event_triggers")
    op.execute("ANALYZE event_trigger_starts")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
