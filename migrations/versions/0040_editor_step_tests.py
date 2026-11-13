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

"""Running one step of a draft for real: test versions and activations, their details, each environment's test
policy, and the step tester role.

A step test writes a one-step workflow version and an activation of it, both marked ``test`` so that no list,
lookup or compile catalog meets them, and a details row that names who started it, for which draft step, and when
it expires. The app still can't change or delete versions or activations: a test activation is retired through its
details row, and ``weave_purge_test_activation`` deletes only a retired test activation that no run uses. Each
environment's test policy is off until an administrator turns it on. Existing rows read ``test = false``, which costs
nothing toward usage; nothing is backfilled, and ``runs`` and ``run_facts`` are unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_editor_step_tests"
down_revision = "0039_transactions_reserve"

SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
SCOPE_KEY = "tenant_id,project_id,environment_id"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
EFFECTS = "'read_only','idempotent','idempotency_key','non_idempotent'"
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
    "step_tester",
)
PURGE = "weave_purge_test_activation(uuid,uuid,uuid,uuid)"


def patch_function(signature: str, anchor: str, replacement: str, *, applied: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1 or applied in body:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the step test migration")
    # CREATE OR REPLACE keeps the owner, the grants and the SET options of the function.
    connection.exec_driver_sql(body.replace(anchor, replacement))


def secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} USING ({EXACT}) WITH CHECK ({EXACT})")


def test_flags() -> None:
    """Every read of versions and activations says whether it reads test rows; normal reads use these indexes."""
    # Adding a column with a constant default changes only the catalog, whatever the table's size.
    op.execute("ALTER TABLE definition_versions ADD COLUMN test boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE activation_revisions ADD COLUMN test boolean NOT NULL DEFAULT false")
    op.execute(
        "CREATE INDEX definition_versions_not_test ON definition_versions(tenant_id,project_id,kind,name) "
        "WHERE NOT test"
    )
    op.execute(
        f"CREATE INDEX activation_revisions_not_test ON activation_revisions({SCOPE_KEY},name,revision) WHERE NOT test"
    )


def details() -> None:
    """Who started each test activation, for which draft and step, what it may change, and until when."""
    op.execute(f"""CREATE TABLE test_activation_details(
        {SCOPE_COLUMNS},activation_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES principals(id),
        source text NOT NULL CHECK(source IN ('step','draft')),draft_id uuid NOT NULL,
        draft_revision integer NOT NULL CHECK(draft_revision>0),
        step_id text CHECK(char_length(step_id) BETWEEN 1 AND 200),run_id uuid,
        side_effect text NOT NULL CHECK(side_effect IN ({EFFECTS})),
        acknowledged_side_effect text CHECK(acknowledged_side_effect IN ({EFFECTS})),
        real_tools jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(real_tools)='array'),
        created_at timestamptz NOT NULL,expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
        retired_at timestamptz,
        CHECK((source='step')=(step_id IS NOT NULL)),
        PRIMARY KEY({SCOPE_KEY},activation_id),
        FOREIGN KEY({SCOPE_KEY},activation_id) REFERENCES activation_revisions({SCOPE_KEY},id))""")
    # Limits count a person's recent and running tests; the sweep finds expired and retired ones.
    op.execute(
        f"CREATE INDEX test_activation_details_principal ON test_activation_details({SCOPE_KEY},principal_id,"
        "created_at)"
    )
    op.execute(
        f"CREATE INDEX test_activation_details_expiry ON test_activation_details({SCOPE_KEY},expires_at) "
        "WHERE retired_at IS NULL"
    )
    op.execute(
        f"CREATE INDEX test_activation_details_retired ON test_activation_details({SCOPE_KEY},retired_at) "
        "WHERE retired_at IS NOT NULL"
    )
    secure("test_activation_details")
    op.execute("GRANT SELECT,INSERT ON test_activation_details TO weave_app")
    op.execute("GRANT UPDATE(retired_at,run_id) ON test_activation_details TO weave_app")


def policies() -> None:
    """Whether an environment allows test calls; no row reads as disabled at revision 0."""
    op.execute(f"""CREATE TABLE environment_test_policies(
        {SCOPE_COLUMNS},test_calls text NOT NULL CHECK(test_calls IN ('enabled','disabled')),
        revision integer NOT NULL CHECK(revision>0),updated_at timestamptz NOT NULL,
        updated_by uuid NOT NULL REFERENCES principals(id),PRIMARY KEY({SCOPE_KEY}),
        FOREIGN KEY({SCOPE_KEY}) REFERENCES environments(tenant_id,project_id,id))""")
    secure("environment_test_policies")
    op.execute("GRANT SELECT,INSERT,UPDATE ON environment_test_policies TO weave_app")


def purge() -> None:
    """Delete a retired test activation, its details, connections, releases and its own version, once no run uses it.

    Returns false while the activation isn't retired, a run, a call or a run's facts still name it, or no such
    activation exists; refuses any activation that isn't a test. Foreign keys block anything else that names it,
    including a run started at the same moment, which rolls the whole purge back.
    """
    op.execute("""CREATE FUNCTION weave_purge_test_activation(t uuid,p uuid,e uuid,target uuid)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE flagged boolean; synthetic uuid; retired timestamptz; versioned boolean;
      BEGIN
        IF t IS DISTINCT FROM nullif(current_setting('weave.tenant_id',true),'')::uuid
          OR p IS DISTINCT FROM nullif(current_setting('weave.project_id',true),'')::uuid
          OR e IS DISTINCT FROM nullif(current_setting('weave.environment_id',true),'')::uuid
          OR e IS NULL OR NOT public.weave_operation_fenced(t,p) THEN
          RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Scoped operation required'; END IF;
        SELECT a.test,a.version_id INTO flagged,synthetic FROM public.activation_revisions a
          WHERE a.tenant_id=t AND a.project_id=p AND a.environment_id=e AND a.id=target;
        IF NOT FOUND THEN RETURN false; END IF;
        IF NOT flagged THEN
          RAISE EXCEPTION USING ERRCODE='WQ004',MESSAGE='Only a test activation can be purged'; END IF;
        SELECT d.retired_at INTO retired FROM public.test_activation_details d
          WHERE d.tenant_id=t AND d.project_id=p AND d.environment_id=e AND d.activation_id=target;
        IF retired IS NULL THEN RETURN false; END IF;
        IF EXISTS(SELECT 1 FROM public.runs WHERE tenant_id=t AND project_id=p AND environment_id=e
            AND activation_id=target)
          OR EXISTS(SELECT 1 FROM public.run_calls WHERE tenant_id=t AND project_id=p AND environment_id=e
            AND activation_id=target)
          OR EXISTS(SELECT 1 FROM public.run_facts WHERE tenant_id=t AND project_id=p AND environment_id=e
            AND activation_id=target) THEN
          RETURN false; END IF;
        SELECT v.test INTO versioned FROM public.definition_versions v
          WHERE v.tenant_id=t AND v.project_id=p AND v.id=synthetic;
        DELETE FROM public.test_activation_details WHERE tenant_id=t AND project_id=p AND environment_id=e
          AND activation_id=target;
        DELETE FROM public.activation_connections WHERE tenant_id=t AND project_id=p AND environment_id=e
          AND activation_id=target;
        DELETE FROM public.activation_connector_releases WHERE tenant_id=t AND project_id=p AND environment_id=e
          AND activation_id=target;
        DELETE FROM public.activation_revisions WHERE tenant_id=t AND project_id=p AND environment_id=e
          AND id=target;
        -- The synthetic version belongs to this activation alone; a published version is never deleted here.
        IF versioned AND NOT EXISTS(SELECT 1 FROM public.activation_revisions WHERE tenant_id=t AND project_id=p
            AND version_id=synthetic) THEN
          DELETE FROM public.definition_sources WHERE tenant_id=t AND project_id=p AND version_id=synthetic;
          DELETE FROM public.definition_versions WHERE tenant_id=t AND project_id=p AND id=synthetic;
        END IF;
        RETURN true;
      END $$""")
    for table in (
        "test_activation_details",
        "activation_connections",
        "activation_connector_releases",
        "activation_revisions",
        "definition_sources",
        "definition_versions",
    ):
        op.execute(f"GRANT SELECT,DELETE ON {table} TO weave_retention_owner")
    for table in ("runs", "run_calls", "run_facts"):
        op.execute(f"GRANT SELECT ON {table} TO weave_retention_owner")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_retention_owner")
    op.execute(f"ALTER FUNCTION {PURGE} OWNER TO weave_retention_owner")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_retention_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {PURGE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {PURGE} TO weave_app")


def row_accounting() -> None:
    """The test flags cost nothing while false, so versions and activations charged before this migration stay exact."""
    anchor = "IF item IS NULL OR item='{}'::jsonb THEN RETURN item; END IF;"
    patch_function(
        "public.weave_charged_row(text,jsonb)",
        anchor,
        anchor + "\n        IF relation IN ('definition_versions','activation_revisions') AND "
        "item->'test'='false'::jsonb THEN item:=item-'test'; END IF;",
        applied="'activation_revisions') AND",
    )


def upgrade() -> None:
    test_flags()
    details()
    policies()
    purge()
    row_accounting()
    accepted = ",".join("'" + role + "'" for role in ROLES)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    op.execute("ANALYZE test_activation_details")
    op.execute("ANALYZE environment_test_policies")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
