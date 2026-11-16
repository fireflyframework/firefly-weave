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

"""Alerts on operations signals: rules and their evaluation state, alerts and their history, email channels, outside
recipients' opt-ins, each person's own alert address, notifications, per-environment settings, installed presets,
the evaluator's schedule and tenant cursor, and the alert manager role.

Every environment table forces row-level security and goes with its environment. None counts toward usage: rules,
channels and opt-ins are capped by the services, and alerts, events and notifications are pruned by retention. Alert
history is append-only for the app, an opt-in can only be revoked, and a rule has at most one firing alert. ``runs``,
the facts tables and ``weave_purge_run`` are unchanged.
"""

from alembic import op

revision = "0042_operations_alerts"
down_revision = "0040_editor_step_tests"

KEY = "tenant_id,project_id,environment_id"
SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
ENVIRONMENT = f"FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))
TENANT = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
SEVERITIES = "'info','warning','critical'"
COMPARATORS = "'gt','gte','lt','lte'"
RESOLVED_REASONS = "'threshold','no_data','rule_changed','rule_disabled','rule_deleted'"
NOTIFICATION_KINDS = "'firing','resolved','repeat','test','approval','approval_moved'"
SIGNAL = "CHECK(signal ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$' AND char_length(signal)<=100)"
THRESHOLD = "CHECK(threshold>=0 AND threshold<=1e9)"
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
    "alert_manager",
)
RECIPIENTS = "weave_alert_recipients_valid(jsonb)"
TENANTS = "weave_alert_tenants(integer)"


def secure(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT {grants} ON {table} TO weave_app")


def secure_tenant(table: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_scope ON {table} TO weave_app USING ({TENANT}) WITH CHECK ({TENANT})")
    op.execute(f"GRANT {grants} ON {table} TO weave_app")


def rules() -> None:
    """Rules, each with one evaluation state row that goes with it.

    A rule Weave installed from a default preset has no updater until a person changes it; every other rule has one.
    """
    op.execute(f"""CREATE TABLE alert_rules(
        {SCOPE_COLUMNS},id uuid NOT NULL,name text NOT NULL CHECK(char_length(name) BETWEEN 1 AND 120),
        enabled boolean NOT NULL,signal text NOT NULL {SIGNAL},
        selector jsonb NOT NULL DEFAULT '{{}}' CHECK(jsonb_typeof(selector)='object'
            AND octet_length(selector::text)<=1024),
        comparator text NOT NULL CHECK(comparator IN ({COMPARATORS})),threshold double precision NOT NULL {THRESHOLD},
        window_seconds integer CHECK(window_seconds IN (60,300,900,3600)),
        for_seconds integer NOT NULL CHECK(for_seconds BETWEEN 0 AND 3600 AND for_seconds%30=0),
        min_samples integer NOT NULL DEFAULT 1 CHECK(min_samples BETWEEN 1 AND 10000),
        severity text NOT NULL CHECK(severity IN ({SEVERITIES})),
        channel_ids uuid[] NOT NULL DEFAULT '{{}}' CHECK(cardinality(channel_ids)<=5
            AND array_position(channel_ids,NULL) IS NULL),
        renotify_seconds integer NOT NULL DEFAULT 0 CHECK(renotify_seconds=0 OR renotify_seconds BETWEEN 900 AND 86400),
        preset_id text CHECK(preset_id ~ '^[a-z0-9][a-z0-9-]{{0,63}}$'),
        revision integer NOT NULL CHECK(revision>0),created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
        updated_by uuid REFERENCES principals(id),CHECK(updated_by IS NOT NULL OR preset_id IS NOT NULL),
        PRIMARY KEY({KEY},id),{ENVIRONMENT})""")
    # Unauthorized is stored by the evaluator when the rule's target grant is gone; unsupported is decided on read.
    op.execute(f"""CREATE TABLE alert_rule_state(
        {SCOPE_COLUMNS},rule_id uuid NOT NULL,
        state text NOT NULL CHECK(state IN ('ok','pending','firing','unauthorized')),since timestamptz NOT NULL,
        pending_since timestamptz,clear_since timestamptz,last_value double precision,evaluated_at timestamptz,
        error_code text CHECK(error_code ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{{0,99}}$'),error_at timestamptz,
        alert_id uuid,revision integer NOT NULL CHECK(revision>0),
        CHECK(state<>'pending' OR pending_since IS NOT NULL),CHECK(state<>'firing' OR alert_id IS NOT NULL),
        CHECK((error_code IS NULL)=(error_at IS NULL)),
        PRIMARY KEY({KEY},rule_id),FOREIGN KEY({KEY},rule_id) REFERENCES alert_rules({KEY},id) ON DELETE CASCADE)""")
    secure("alert_rules", "SELECT,INSERT,UPDATE,DELETE")
    secure("alert_rule_state", "SELECT,INSERT,UPDATE,DELETE")


def history() -> None:
    """Alerts outlive a deleted rule, so they copy what they need from it; their events are append-only."""
    op.execute(f"""CREATE TABLE alerts(
        {SCOPE_COLUMNS},id uuid NOT NULL,rule_id uuid NOT NULL,
        rule_name text NOT NULL CHECK(char_length(rule_name) BETWEEN 1 AND 120),
        rule_revision integer NOT NULL CHECK(rule_revision>0),signal text NOT NULL {SIGNAL},
        severity text NOT NULL CHECK(severity IN ({SEVERITIES})),
        comparator text NOT NULL CHECK(comparator IN ({COMPARATORS})),threshold double precision NOT NULL {THRESHOLD},
        unit text NOT NULL CHECK(unit IN ('count','ratio','seconds')),
        state text NOT NULL CHECK(state IN ('firing','resolved')),started_at timestamptz NOT NULL,
        resolved_at timestamptz,resolved_reason text CHECK(resolved_reason IN ({RESOLVED_REASONS})),
        value_at_start double precision NOT NULL,last_value double precision,
        acknowledged_at timestamptz,acknowledged_by uuid REFERENCES principals(id),last_notified_at timestamptz,
        repeat_count integer NOT NULL DEFAULT 0 CHECK(repeat_count>=0),
        CHECK((state='resolved')=(resolved_at IS NOT NULL)),CHECK((resolved_at IS NULL)=(resolved_reason IS NULL)),
        CHECK((acknowledged_at IS NULL)=(acknowledged_by IS NULL)),
        PRIMARY KEY({KEY},id),{ENVIRONMENT})""")
    # A rule fires at most once at a time; a concurrent second firing insert fails instead of doubling the alert.
    op.execute(f"CREATE UNIQUE INDEX alerts_one_firing ON alerts({KEY},rule_id) WHERE state='firing'")
    op.execute(f"CREATE INDEX alerts_started ON alerts({KEY},started_at DESC,id DESC)")
    op.execute(f"CREATE INDEX alerts_resolved ON alerts({KEY},resolved_at) WHERE state='resolved'")
    op.execute(f"""CREATE TABLE alert_events(
        {SCOPE_COLUMNS},id uuid NOT NULL,alert_id uuid,rule_id uuid NOT NULL,
        rule_revision integer NOT NULL CHECK(rule_revision>0),at timestamptz NOT NULL,
        kind text NOT NULL CHECK(kind IN ('firing','resolved','acknowledged')),value double precision,
        reason text CHECK(reason IN ({RESOLVED_REASONS})),
        PRIMARY KEY({KEY},id),{ENVIRONMENT})""")
    op.execute(f"CREATE INDEX alert_events_at ON alert_events({KEY},at DESC,id DESC)")
    op.execute(f"CREATE INDEX alert_events_rule ON alert_events({KEY},rule_id,at DESC,id DESC)")
    secure("alerts", "SELECT,INSERT,UPDATE,DELETE")
    secure("alert_events", "SELECT,INSERT,DELETE")


def recipients() -> None:
    """Email channels, outside recipients' opt-ins, each person's own address and the tenant's address hash key."""
    op.execute(f"""CREATE FUNCTION {RECIPIENTS.replace("(jsonb)", "(recipients jsonb)")} RETURNS boolean
      LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
      DECLARE item jsonb; field text; entry jsonb;
      BEGIN
        IF jsonb_typeof(recipients)<>'array' THEN RETURN false; END IF;
        IF jsonb_array_length(recipients) NOT BETWEEN 1 AND 10 THEN RETURN false; END IF;
        FOR item IN SELECT element FROM jsonb_array_elements(recipients) AS elements(element) LOOP
          IF jsonb_typeof(item)<>'object' THEN RETURN false; END IF;
          IF (SELECT count(*) FROM jsonb_object_keys(item))<>1 THEN RETURN false; END IF;
          SELECT pair.key,pair.value INTO field,entry FROM jsonb_each(item) AS pair;
          IF jsonb_typeof(entry)<>'string' THEN RETURN false; END IF;
          IF field='member' THEN
            IF (entry#>>'{{}}') !~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$' THEN
              RETURN false;
            END IF;
          ELSIF field='address' THEN
            IF char_length(entry#>>'{{}}') NOT BETWEEN 3 AND 254 OR strpos(entry#>>'{{}}','@')<2 THEN
              RETURN false;
            END IF;
          ELSE
            RETURN false;
          END IF;
        END LOOP;
        RETURN true;
      END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {RECIPIENTS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {RECIPIENTS} TO weave_app")
    address = "CHECK(char_length(address) BETWEEN 3 AND 254 AND strpos(address,'@')>1)"
    op.execute(f"""CREATE TABLE alert_channels(
        {SCOPE_COLUMNS},id uuid NOT NULL,kind text NOT NULL CHECK(kind='email'),
        name text NOT NULL CHECK(char_length(name) BETWEEN 1 AND 80),connection_revision_id uuid NOT NULL,
        recipients jsonb NOT NULL CHECK({RECIPIENTS.replace("(jsonb)", "(recipients)")}),
        owner_id uuid NOT NULL REFERENCES principals(id),revision integer NOT NULL CHECK(revision>0),
        created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
        PRIMARY KEY({KEY},id),{ENVIRONMENT},
        FOREIGN KEY({KEY},connection_revision_id) REFERENCES connection_revisions({KEY},id))""")
    op.execute(f"""CREATE TABLE alert_recipient_consents(
        {SCOPE_COLUMNS},id uuid NOT NULL,address text NOT NULL {address},
        domain text NOT NULL CHECK(char_length(domain) BETWEEN 1 AND 253),
        method text NOT NULL CHECK(method IN ('administrator_attested','recipient_confirmed')),
        statement text NOT NULL CHECK(char_length(statement) BETWEEN 1 AND 200),
        created_by uuid NOT NULL REFERENCES principals(id),created_at timestamptz NOT NULL,
        revoked_at timestamptz,revoked_by uuid REFERENCES principals(id),
        CHECK((revoked_at IS NULL)=(revoked_by IS NULL)),
        PRIMARY KEY({KEY},id),{ENVIRONMENT})""")
    op.execute(
        f"CREATE UNIQUE INDEX alert_recipient_consents_active ON alert_recipient_consents({KEY},address) "
        "WHERE revoked_at IS NULL"
    )
    secure("alert_channels", "SELECT,INSERT,UPDATE,DELETE")
    # An opt-in is a record of how someone agreed: the app may only revoke it.
    secure("alert_recipient_consents", "SELECT,INSERT")
    op.execute("GRANT UPDATE(revoked_at,revoked_by) ON alert_recipient_consents TO weave_app")
    op.execute(f"""CREATE TABLE alert_member_addresses(
        tenant_id uuid NOT NULL REFERENCES tenants(id),principal_id uuid NOT NULL REFERENCES principals(id),
        address text NOT NULL {address},updated_at timestamptz NOT NULL,PRIMARY KEY(tenant_id,principal_id))""")
    secure_tenant("alert_member_addresses", "SELECT,INSERT,UPDATE,DELETE")
    # The key that hashes addresses in audit records is created once per tenant and never changes.
    op.execute("""CREATE TABLE alert_hash_keys(
        tenant_id uuid PRIMARY KEY REFERENCES tenants(id),key bytea NOT NULL CHECK(octet_length(key)=32),
        created_at timestamptz NOT NULL)""")
    secure_tenant("alert_hash_keys", "SELECT,INSERT")


def delivery() -> None:
    """Notifications, settings, installed presets and the evaluator's per-environment schedule."""
    op.execute(f"""CREATE TABLE alert_notifications(
        {SCOPE_COLUMNS},id uuid NOT NULL,alert_id uuid,channel_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ({NOTIFICATION_KINDS})),sequence integer NOT NULL CHECK(sequence>=0),
        recipients_count integer NOT NULL CHECK(recipients_count BETWEEN 0 AND 10),
        state text NOT NULL CHECK(state IN ('pending','sent','failed','skipped_limit')),
        code text CHECK(code ~ '^[a-z][a-z_]{{0,63}}$'),attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
        created_at timestamptz NOT NULL,attempted_at timestamptz,change_request_id uuid,
        change_request_revision integer CHECK(change_request_revision>0),
        CHECK((kind IN ('firing','resolved','repeat'))=(alert_id IS NOT NULL)),
        CHECK((kind='approval')=(change_request_id IS NOT NULL)),
        CHECK((change_request_id IS NULL)=(change_request_revision IS NULL)),
        PRIMARY KEY({KEY},id),{ENVIRONMENT})""")
    # A retried evaluation or scan writes each notification once.
    op.execute(
        f"CREATE UNIQUE INDEX alert_notifications_alert ON alert_notifications({KEY},alert_id,channel_id,kind,"
        "sequence) WHERE alert_id IS NOT NULL"
    )
    op.execute(
        f"CREATE UNIQUE INDEX alert_notifications_approval ON alert_notifications({KEY},change_request_id,"
        "change_request_revision,channel_id) WHERE kind='approval'"
    )
    op.execute(
        f"CREATE INDEX alert_notifications_pending ON alert_notifications({KEY},created_at,id) WHERE state='pending'"
    )
    op.execute(f"""CREATE TABLE alert_settings(
        {SCOPE_COLUMNS},recipient_domains text[] NOT NULL DEFAULT '{{}}' CHECK(cardinality(recipient_domains)<=20
            AND array_position(recipient_domains,NULL) IS NULL),
        approval_channel_ids uuid[] NOT NULL DEFAULT '{{}}' CHECK(cardinality(approval_channel_ids)<=5
            AND array_position(approval_channel_ids,NULL) IS NULL),
        revision integer NOT NULL CHECK(revision>0),updated_at timestamptz NOT NULL,
        updated_by uuid NOT NULL REFERENCES principals(id),PRIMARY KEY({KEY}),{ENVIRONMENT})""")
    # A default preset is installed once per environment; the record stays when its rule is deleted, so it never
    # comes back.
    op.execute(f"""CREATE TABLE alert_preset_installs(
        {SCOPE_COLUMNS},preset_id text NOT NULL CHECK(preset_id ~ '^[a-z0-9][a-z0-9-]{{0,63}}$'),
        version integer NOT NULL CHECK(version>0),rule_id uuid NOT NULL,installed_at timestamptz NOT NULL,
        PRIMARY KEY({KEY},preset_id),{ENVIRONMENT})""")
    op.execute(f"""CREATE TABLE alert_environments(
        {SCOPE_COLUMNS},next_at timestamptz NOT NULL,approval_after_at timestamptz,approval_after_id uuid,
        CHECK((approval_after_at IS NULL)=(approval_after_id IS NULL)),PRIMARY KEY({KEY}),{ENVIRONMENT})""")
    op.execute("CREATE INDEX alert_environments_due ON alert_environments(tenant_id,next_at)")
    secure("alert_notifications", "SELECT,INSERT,UPDATE,DELETE")
    secure("alert_settings", "SELECT,INSERT,UPDATE,DELETE")
    secure("alert_preset_installs", "SELECT,INSERT")
    # The evaluator claims due environments with only the tenant set, then works inside each one; a row is created
    # and removed only from inside its own environment. A claim may change the schedule and the approval scan
    # position, never which environment a row belongs to.
    op.execute("ALTER TABLE alert_environments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE alert_environments FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_read ON alert_environments FOR SELECT TO weave_app USING ({TENANT})")
    op.execute(
        f"CREATE POLICY tenant_claim ON alert_environments FOR UPDATE TO weave_app USING ({TENANT}) "
        f"WITH CHECK ({TENANT})"
    )
    op.execute(f"CREATE POLICY exact_insert ON alert_environments FOR INSERT TO weave_app WITH CHECK ({EXACT})")
    op.execute(f"CREATE POLICY exact_delete ON alert_environments FOR DELETE TO weave_app USING ({EXACT})")
    op.execute("GRANT SELECT,INSERT,DELETE ON alert_environments TO weave_app")
    op.execute("GRANT UPDATE(next_at,approval_after_at,approval_after_id) ON alert_environments TO weave_app")


def tenant_page() -> None:
    """The alert evaluator pages tenants with its own cursor, so it never moves the operations or recovery cursors."""
    op.execute("CREATE TABLE alert_tenant_cursor(singleton boolean PRIMARY KEY CHECK(singleton),tenant_id uuid)")
    op.execute("INSERT INTO alert_tenant_cursor VALUES(true,NULL)")
    op.execute("GRANT SELECT,UPDATE ON alert_tenant_cursor TO weave_catalog_reader")
    op.execute(f"""CREATE FUNCTION {TENANTS.replace("(integer)", "(page_size integer)")} RETURNS SETOF uuid
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE prior uuid; candidate uuid; visited uuid[] := ARRAY[]::uuid[];
      BEGIN
        IF page_size IS NULL OR page_size<1 OR page_size>16 THEN
          RAISE EXCEPTION 'Bounded catalog page required';
        END IF;
        SELECT tenant_id INTO prior FROM public.alert_tenant_cursor WHERE singleton FOR UPDATE;
        FOR candidate IN SELECT id FROM public.tenants WHERE prior IS NULL OR id>prior ORDER BY id LIMIT page_size LOOP
          visited := array_append(visited,candidate); RETURN NEXT candidate;
        END LOOP;
        IF array_length(visited,1) IS NULL OR array_length(visited,1)<page_size THEN
          FOR candidate IN SELECT id FROM public.tenants WHERE id<=prior ORDER BY id
            LIMIT page_size-coalesce(array_length(visited,1),0) LOOP
            visited := array_append(visited,candidate); RETURN NEXT candidate;
          END LOOP;
        END IF;
        IF array_length(visited,1)>0 THEN
          UPDATE public.alert_tenant_cursor SET tenant_id=visited[array_length(visited,1)] WHERE singleton;
        END IF;
      END $$""")
    op.execute("GRANT CREATE ON SCHEMA public TO weave_catalog_reader")
    op.execute(f"ALTER FUNCTION {TENANTS} OWNER TO weave_catalog_reader")
    op.execute("REVOKE CREATE ON SCHEMA public FROM weave_catalog_reader")
    op.execute(f"REVOKE ALL ON FUNCTION {TENANTS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {TENANTS} TO weave_scheduler")


def upgrade() -> None:
    rules()
    history()
    recipients()
    delivery()
    tenant_page()
    accepted = ",".join("'" + role + "'" for role in ROLES)
    op.execute("ALTER TABLE role_bindings DROP CONSTRAINT role_bindings_role_check")
    op.execute(f"ALTER TABLE role_bindings ADD CONSTRAINT role_bindings_role_check CHECK(role IN ({accepted}))")
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    for table in (
        "alert_rules",
        "alert_rule_state",
        "alerts",
        "alert_events",
        "alert_channels",
        "alert_recipient_consents",
        "alert_member_addresses",
        "alert_hash_keys",
        "alert_notifications",
        "alert_settings",
        "alert_preset_installs",
        "alert_environments",
    ):
        op.execute(f"ANALYZE {table}")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
