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

"""Scoped operational projections, bounded retained-history backfill and accounted control capacity."""

import json
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from pydantic import TypeAdapter

from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.definitions import ResourceName
from firefly_weave.contracts.instance_keys import InstanceKeyText, instance_view
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.runtime.admission import POLICY, admission
from firefly_weave.runtime.models import RunState

revision = "0031_operations_facts"
down_revision = "0030_worker_presence"

SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
SCOPE_KEY = "tenant_id,project_id,environment_id"
SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
EXACT = " AND ".join(
    f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid"
    for name in ("tenant_id", "project_id", "environment_id")
)
TABLE_KEYS = {
    "run_facts": ("run_id",),
    "step_facts": ("run_id", "node_id", "instance_key"),
    "task_facts": ("task_id",),
    "incident_facts": ("incident_id",),
    "worker_facts": ("worker_id",),
}
STATUSES = "'queued','running','waiting','suspended','succeeded','failed','cancelled','timed_out'"
TERMINAL = "'succeeded','failed','cancelled','timed_out'"
ORIGINS = "'manual','webhook','schedule','broker','email','provider','retry','call','test'"
BATCH_SIZE = 10000
FETCH_BYTES = 128 * 1024 * 1024


def secure(table: str, key: tuple[str, ...]) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT SELECT,INSERT,UPDATE ON {table} TO weave_app")
    op.execute(f"GRANT SELECT,DELETE ON {table} TO weave_retention_owner")
    op.execute(f"CREATE POLICY operations_inventory ON {table} TO weave_retention_owner USING(true)")
    arguments = ",".join("'" + name + "'" for name in key)
    op.execute(
        f"CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION weave_usage_charge({arguments})"
    )


def tables() -> None:
    run_parent = f"FOREIGN KEY({SCOPE_KEY},run_id) REFERENCES runs({SCOPE_KEY},id) ON DELETE CASCADE"
    op.execute(f"""CREATE TABLE run_facts(
        {SCOPE_COLUMNS},run_id uuid PRIMARY KEY,definition_version_id uuid NOT NULL,
        workflow_name text NOT NULL,workflow_version text NOT NULL,activation_id uuid NOT NULL,
        activation_name text NOT NULL,activation_revision integer NOT NULL CHECK(activation_revision>0),
        status text,paused boolean NOT NULL DEFAULT false,
        test boolean NOT NULL DEFAULT false,origin text CHECK(origin IN ({ORIGINS})),
        caller_run_id uuid,caller_node_id text,caller_instance_key text,retried_from_run_id uuid,
        business_key text,correlation_key text,started_at timestamptz,updated_at timestamptz,ended_at timestamptz,
        failed_node_id text,failed_instance_key text,failed_error_code text,
        active_incidents bigint NOT NULL DEFAULT 0 CHECK(active_incidents>=0),
        handled_errors bigint NOT NULL DEFAULT 0 CHECK(handled_errors>=0),
        classification_state text NOT NULL CHECK(classification_state IN ('available','unavailable','unsupported')),
        classification_policy text NOT NULL,pinned_ir_version text,
        CHECK((status IS NULL AND classification_state='unavailable') OR
            (status IS NOT NULL AND status IN ({STATUSES}))),
        pinned_features jsonb NOT NULL DEFAULT '[]',node_kinds jsonb NOT NULL DEFAULT '{{}}',
        last_event_sequence bigint NOT NULL DEFAULT 0,facts_started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK(CASE WHEN jsonb_typeof(pinned_features)='array' THEN
            jsonb_array_length(pinned_features)<=256 AND octet_length(pinned_features::text)<=16384 AND
            NOT jsonb_path_exists(pinned_features,'$[*] ? (@.type() != "string")') ELSE false END),
        CHECK(CASE WHEN jsonb_typeof(node_kinds)='object' THEN octet_length(node_kinds::text)<=1048576 AND
            jsonb_array_length(jsonb_path_query_array(node_kinds,'$.keyvalue()'))<=10000 AND
            NOT jsonb_path_exists(node_kinds,'$.keyvalue().value ? (@.type() != "string")') ELSE false END),
        {run_parent})""")
    op.execute(f"""CREATE TABLE step_facts(
        {SCOPE_COLUMNS},run_id uuid NOT NULL,node_id text NOT NULL,instance_key text NOT NULL DEFAULT '',
        kind text NOT NULL,status text NOT NULL,scheduled_at timestamptz,started_at timestamptz,ended_at timestamptz,
        attempts bigint NOT NULL DEFAULT 0 CHECK(attempts>=0),worker_id uuid,error_code text,
        handled text CHECK(handled IN ('continue','errorOutput')),child_run_id uuid,
        PRIMARY KEY(run_id,node_id,instance_key),CHECK(handled IS NULL OR status='failed'),{run_parent})""")
    op.execute(f"""CREATE TABLE task_facts(
        {SCOPE_COLUMNS},task_id uuid PRIMARY KEY,run_id uuid NOT NULL,node_id text NOT NULL,
        instance_key text NOT NULL DEFAULT '',task_type text NOT NULL,status text NOT NULL,
        created_at timestamptz NOT NULL,ready_since timestamptz,first_claimed_at timestamptz,
        FOREIGN KEY({SCOPE_KEY},task_id) REFERENCES task_intents({SCOPE_KEY},id) ON DELETE CASCADE,{run_parent})""")
    op.execute(f"""CREATE TABLE incident_facts(
        {SCOPE_COLUMNS},incident_id uuid PRIMARY KEY,node_id text,instance_key text NOT NULL DEFAULT '',
        opened_at timestamptz,updated_at timestamptz,workflow_name text NOT NULL,workflow_version text NOT NULL,
        test boolean NOT NULL DEFAULT false,opened_at_estimated boolean NOT NULL DEFAULT false,
        FOREIGN KEY({SCOPE_KEY},incident_id) REFERENCES incidents({SCOPE_KEY},id) ON DELETE CASCADE)""")
    op.execute(f"""CREATE TABLE worker_facts(
        {SCOPE_COLUMNS},worker_id uuid PRIMARY KEY,registered_at timestamptz,identity jsonb NOT NULL DEFAULT '{{}}',
        drained_at timestamptz,CHECK(octet_length(identity::text)<=4096),
        FOREIGN KEY({SCOPE_KEY},worker_id) REFERENCES worker_instances({SCOPE_KEY},id) ON DELETE CASCADE)""")
    op.execute(
        "CREATE TABLE operations_environment_cursors(tenant_id uuid PRIMARY KEY REFERENCES "
        "tenants(id),environment_id uuid)"
    )
    tenant = "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid"
    op.execute("ALTER TABLE operations_environment_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE operations_environment_cursors FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_scope ON operations_environment_cursors TO weave_app USING "
        f"({tenant}) WITH CHECK ({tenant})"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON operations_environment_cursors TO weave_app")
    op.execute(f"""CREATE TABLE operations_fact_cursors({SCOPE_COLUMNS},after_run_id uuid,
        PRIMARY KEY({SCOPE_KEY}),FOREIGN KEY({SCOPE_KEY}) REFERENCES environments(tenant_id,project_id,id)
        ON DELETE CASCADE)""")
    op.execute("ALTER TABLE operations_fact_cursors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE operations_fact_cursors FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY exact_scope ON operations_fact_cursors TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON operations_fact_cursors TO weave_app")


def indexes() -> None:
    for name, columns, condition in (
        ("started", "started_at DESC,run_id DESC", ""),
        ("workflow", "workflow_name,started_at DESC,run_id DESC", ""),
        ("status", "status,started_at DESC,run_id DESC", ""),
        ("activation", "activation_id,started_at DESC,run_id DESC", ""),
        ("updated", "updated_at DESC,run_id DESC", ""),
        ("business_key", "business_key", ""),
        ("correlation_key", "correlation_key", ""),
        ("caller", "caller_run_id", "WHERE caller_run_id IS NOT NULL"),
        ("retry", "retried_from_run_id", "WHERE retried_from_run_id IS NOT NULL"),
        ("ended", "ended_at", "WHERE ended_at IS NOT NULL"),
        ("active_test", "", "WHERE test AND ended_at IS NULL"),
    ):
        op.execute(
            f"CREATE INDEX run_facts_{name} ON run_facts({SCOPE_KEY}{',' + columns if columns else ''}) {condition}"
        )
    op.execute(f"CREATE INDEX step_facts_ended ON step_facts({SCOPE_KEY},ended_at)")
    op.execute(
        f"CREATE INDEX task_facts_ready ON task_facts({SCOPE_KEY},task_type,ready_since) WHERE ready_since IS NOT NULL"
    )
    op.execute(f"CREATE INDEX incident_facts_node_opened ON incident_facts({SCOPE_KEY},node_id,opened_at,incident_id)")
    op.execute(f"CREATE INDEX run_deadlines_operations_due ON run_deadlines({SCOPE_KEY},deadline) WHERE NOT consumed")
    op.execute(
        f"CREATE INDEX event_deliveries_operations_due ON event_deliveries({SCOPE_KEY},next_at) "
        f"WHERE status IN ('pending','retry')"
    )
    op.execute(f"CREATE INDEX incidents_operations_active ON incidents({SCOPE_KEY},status) WHERE status='active'")


def patch_function(signature: str, anchor: str, replacement: str) -> None:
    connection = op.get_bind()
    body = connection.scalar(
        sa.text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
    )
    if not isinstance(body, str) or body.count(anchor) != 1:
        raise RuntimeError(f"Unexpected definition of {signature}; reconcile the control-accounting migration")
    connection.exec_driver_sql(body.replace(anchor, replacement))


def control_accounting() -> None:
    anchor = "IF relation_name='runs' THEN RETURN (item->>'id')::uuid; END IF;"
    patch_function(
        "public.weave_control_target(text,jsonb)",
        anchor,
        """
      IF relation_name IN ('run_facts','step_facts','task_facts') THEN
        RETURN (item->>'run_id')::uuid;
      END IF;
      IF relation_name='incident_facts' THEN
        SELECT run_id INTO identifier FROM public.incidents WHERE id=(item->>'incident_id')::uuid;
        RETURN identifier;
      END IF;
      """
        + anchor,
    )
    anchor = "+(item->>'_weave_activation_bytes')::numeric+16777216+64*2048+65536+576*affected;"
    predicate = "tenant_id=tenant AND project_id=project AND environment_id=(item->>'environment_id')::uuid"
    patch_function(
        "public.weave_reserve_refresh(text,uuid,boolean)",
        anchor,
        anchor[:-1]
        + f"""
            +2048+2048*((SELECT count(*) FROM public.step_facts WHERE {predicate} AND run_id=identifier)
              +(SELECT count(*) FROM public.task_facts WHERE {predicate} AND run_id=identifier)
              +(SELECT count(*) FROM public.incident_facts f JOIN public.incidents i ON
                i.tenant_id=f.tenant_id AND i.project_id=f.project_id AND i.environment_id=f.environment_id
                AND i.id=f.incident_id WHERE f.tenant_id=tenant AND f.project_id=project
                AND f.environment_id=(item->>'environment_id')::uuid AND i.run_id=identifier));""",
    )
    anchor = "ELSIF relation_name IN ('task_intents','task_leases','incidents','run_deadlines') THEN"
    patch_function(
        "public.weave_reservation_touch(text,jsonb)",
        anchor,
        "ELSIF relation_name IN ('task_intents','task_leases','incidents','run_deadlines',"
        "'run_facts','step_facts','task_facts','incident_facts') THEN",
    )


def node_kinds(artifact: Any) -> dict[str, str]:
    """Freeze author-step eligibility for the retained projection independently of live writers."""
    result = {}
    for node in artifact.executable["graph"]["nodes"]:
        key = node["id"]
        if isinstance(key, str) and key.startswith("@"):
            continue
        TypeAdapter(InstanceKeyText).validate_python(key)
        view = instance_view(key)
        TypeAdapter(ResourceName).validate_python(view.node_id)
        result[view.node_id] = node["kind"]
    if len(result) > 10000 or len(json.dumps(result, ensure_ascii=False).encode()) > 1048576:
        raise ValueError("Node-kind projection exceeds its bound")
    return result


def classification(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "classification_state": "unavailable",
        "classification_policy": POLICY,
        "pinned_ir_version": None,
        "pinned_features": "[]",
        "node_kinds": "{}",
    }
    try:
        RunState.model_validate_json(json.dumps(row["state"]))
        Activation.model_validate_json(json.dumps(row["activation"]))
        StartRunRequest.model_validate_json(json.dumps(row["request"]))
        decision = admission(row)
        executable = row["artifact"].get("executable", {})
        version, features = executable.get("irVersion"), executable.get("features", [])
        if not isinstance(version, str) or len(version) > 256 or not isinstance(features, list):
            return result
        if len(features) > 256 or any(type(feature) is not str for feature in features):
            return result
        encoded = json.dumps(features, ensure_ascii=False)
        if len(encoded.encode()) > 16384:
            return result
        result.update(classification_state=decision, pinned_ir_version=version, pinned_features=encoded)
        if decision == "available":
            result["node_kinds"] = json.dumps(node_kinds(import_artifact(row["artifact"])), ensure_ascii=False)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result.update(classification_state="unavailable", node_kinds="{}")
    return result


EVENTS = f"""WITH ordered AS (
    SELECT e.*,lag(created_at) OVER(PARTITION BY run_id ORDER BY sequence) AS previous_at,
      max(sequence) OVER(PARTITION BY run_id) AS final_sequence,
      public.weave_run_events_bytes(e)<={FETCH_BYTES} AS bounded
    FROM run_events e WHERE {SCOPE} AND run_id=ANY(:ids)
) SELECT run_id,min(created_at) AS started_at,max(created_at) AS updated_at,
    max(created_at) FILTER(WHERE transition->'state'->>'status' IN ({TERMINAL})) AS ended_at,
    min(sequence) AS first_sequence,max(sequence) AS last_event_sequence,count(*) AS event_count,
    max(transition->'state'->>'status') FILTER(WHERE sequence=final_sequence) AS last_status,
    bool_and(coalesce(bounded AND jsonb_typeof(data)='object' AND jsonb_typeof(transition->'state')='object'
      AND transition->'state'->>'status' IN ({STATUSES})
      AND transition->'state'->>'accepted_sequence'=sequence::text
      AND (previous_at IS NULL OR created_at>=previous_at),false)) AS coherent,
    min(created_at) FILTER(WHERE sequence=1 AND type='started') AS first_started,
    min(data->>'origin') FILTER(WHERE sequence=1 AND type='started' AND data->>'origin' IN ({ORIGINS})) AS
    explicit_origin
    FROM ordered GROUP BY run_id"""


def receipt_origins(connection, params):
    query = []
    for receipt, source, foreign, extra, origin in (
        ("trigger_receipts", "trigger_routes", "trigger_id", "", "webhook"),
        ("broker_events", "broker_routes", "trigger_id", "", "broker"),
        ("email_receipts", "email_sources", "source_id", "AND r.state='dispatched' AND r.signal_id IS NULL", "email"),
    ):
        query.append(
            f"SELECT r.run_id,'{origin}' AS origin FROM {receipt} r JOIN {source} s "
            "ON s.tenant_id=r.tenant_id AND s.project_id=r.project_id AND s.environment_id=r.environment_id "
            f"AND s.id=r.{foreign} WHERE r.tenant_id=:tenant AND r.project_id=:project "
            "AND r.environment_id=:environment AND r.run_id=ANY(:ids) AND s.activation_id IS NOT NULL " + extra
        )
    query.append(
        "SELECT run_id,'schedule' AS origin FROM schedule_occurrences WHERE "
        + SCOPE
        + " AND run_id=ANY(:ids) AND kind='started'"
    )
    query.append(
        "SELECT cast(r.receipt->>'run_id' AS uuid),'provider' AS origin FROM provider_receipts r "
        "JOIN provider_sources s ON s.tenant_id=r.tenant_id AND s.project_id=r.project_id "
        "AND s.environment_id=r.environment_id AND s.id=r.source_id "
        "WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment "
        "AND r.receipt->>'run_id'=ANY(:text_ids) AND r.state='dispatched' "
        "AND s.activation_id IS NOT NULL AND r.receipt->>'signal_id' IS NULL"
    )
    result: dict[UUID, set[str]] = {}
    for row in connection.execute(
        sa.text(" UNION ALL ".join(query)), {**params, "text_ids": [str(v) for v in params["ids"]]}
    ).mappings():
        result.setdefault(row["run_id"], set()).add(row["origin"])
    return result


def backfill_incidents(connection, params, events) -> None:
    scoped = "i.tenant_id=:tenant AND i.project_id=:project AND i.environment_id=:environment"
    rows = connection.execute(
        sa.text(f"""WITH evidence AS (
            SELECT run_id,type,data->>'node_id' AS node_id,coalesce(data->>'generation','0') AS generation,
                data->>'code' AS code,CASE WHEN type='incident_resolved' THEN data->>'incident_key'
                ELSE (data->>'node_id')||':'||coalesce(data->>'generation','0') END AS incident_key,
                min(created_at) AS first_at,max(created_at) AS last_at
            FROM run_events e WHERE {SCOPE} AND run_id=ANY(:ids)
                AND type IN ('incident_opened','incident_resolved')
                AND public.weave_run_events_bytes(e)<={FETCH_BYTES}
            GROUP BY run_id,type,data->>'node_id',coalesce(data->>'generation','0'),data->>'code',
                CASE WHEN type='incident_resolved' THEN data->>'incident_key'
                ELSE (data->>'node_id')||':'||coalesce(data->>'generation','0') END
        ) SELECT i.*,f.workflow_name,f.workflow_version,f.test,
        min(e.first_at) FILTER(WHERE e.type='incident_opened') AS exact_opened,
        max(e.last_at) AS last_transition FROM incidents i JOIN run_facts f
        ON f.tenant_id=i.tenant_id AND f.project_id=i.project_id
          AND f.environment_id=i.environment_id AND f.run_id=i.run_id
        LEFT JOIN evidence e ON e.run_id=i.run_id AND e.node_id=i.node_id
          AND e.generation=coalesce(i.generation,0)::text AND e.incident_key=i.incident_key
          AND (e.type='incident_resolved' OR e.code=i.origin_code)
        WHERE {scoped} AND i.run_id=ANY(:ids)
        GROUP BY i.id,f.workflow_name,f.workflow_version,f.test"""),
        params,
    ).mappings()
    for row in rows:
        node, instance = None, ""
        try:
            if row["node_id"] is not None and not row["node_id"].startswith("@"):
                TypeAdapter(InstanceKeyText).validate_python(row["node_id"])
                view = instance_view(row["node_id"])
                TypeAdapter(ResourceName).validate_python(view.node_id)
                node, instance = view.node_id, view.instance_key
        except (ValueError, TypeError):
            pass
        event = events.get(row["run_id"], {})
        opened = row["exact_opened"] or event.get("updated_at")
        times = [value for value in (opened, row["last_transition"], row["resolved_at"]) if value is not None]
        connection.execute(
            sa.text(
                "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,"
                "node_id,instance_key,opened_at,updated_at,workflow_name,workflow_version,test,opened_at_estimated) "
                "VALUES(:tenant,:project,:environment,:incident,:node,:instance,:opened,:updated,"
                ":name,:version,:test,:estimated)"
            ),
            {
                **params,
                "incident": row["id"],
                "node": node,
                "instance": instance,
                "opened": opened,
                "updated": max(times) if times else None,
                "name": row["workflow_name"],
                "version": row["workflow_version"],
                "test": row["test"],
                "estimated": row["exact_opened"] is None,
            },
        )


def backfill_scope(connection, params) -> None:
    after = None
    while True:
        ids = list(
            connection.execute(
                sa.text(
                    f"SELECT id FROM runs WHERE {SCOPE} "
                    "AND (cast(:after AS uuid) IS NULL OR id>:after) ORDER BY id LIMIT 10000"
                ),
                {**params, "after": after},
            ).scalars()
        )
        if not ids:
            break
        batch = {**params, "ids": ids}
        events = {row["run_id"]: dict(row) for row in connection.execute(sa.text(EVENTS), batch).mappings()}
        origins = receipt_origins(connection, batch)
        identities = connection.execute(
            sa.text("""SELECT r.id,a.id AS activation_id,a.version_id AS definition_version_id,
            a.name AS activation_name,a.revision AS activation_revision,v.name AS workflow_name,v.version AS
            workflow_version,
            CASE WHEN jsonb_typeof(r.state->'status')='string' THEN r.state->>'status' END AS status,
            r.state->>'manual_paused' AS paused,
            CASE WHEN jsonb_typeof(r.request->'business_key')='string' AND
                char_length(r.request->>'business_key')<=200 THEN r.request->>'business_key' END AS business_key,
            CASE WHEN jsonb_typeof(r.request->'correlation_key')='string' AND
                char_length(r.request->>'correlation_key')<=200 THEN r.request->>'correlation_key'
                END AS correlation_key,
            l.parent_run_id,public.weave_runs_bytes(r) AS bytes,
            (SELECT count(*) FROM incidents i WHERE i.tenant_id=r.tenant_id AND i.project_id=r.project_id
              AND i.environment_id=r.environment_id AND i.run_id=r.id AND i.status='active') AS active_incidents
            FROM runs r JOIN activation_revisions a ON a.tenant_id=r.tenant_id AND a.project_id=r.project_id
              AND a.environment_id=r.environment_id AND a.id=r.activation_id
            JOIN definition_versions v ON v.tenant_id=a.tenant_id AND v.project_id=a.project_id AND v.id=a.version_id
            LEFT JOIN run_retry_links l ON l.tenant_id=r.tenant_id AND l.project_id=r.project_id
              AND l.environment_id=r.environment_id AND l.run_id=r.id
            WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment AND r.id=ANY(:ids)
            ORDER BY r.id"""),
            batch,
        ).mappings()
        facts = []
        for identity in identities:
            item = dict(identity)
            event = events.get(item["id"], {})
            cached = classification({})
            state = None
            if item["bytes"] <= FETCH_BYTES:
                row = (
                    connection.execute(
                        sa.text(f"SELECT artifact,state,activation,request FROM runs WHERE {SCOPE} AND id=:id"),
                        {**params, "id": item["id"]},
                    )
                    .mappings()
                    .one()
                )
                cached = classification(dict(row))
                state = row["state"]
            if not (
                event.get("coherent")
                and event["first_sequence"] == 1
                and event["last_event_sequence"] == event["event_count"]
                and event["first_started"] == event["started_at"]
                and isinstance(state, dict)
                and state.get("accepted_sequence") == event["last_event_sequence"]
                and item["status"] == event["last_status"]
                and (
                    item["status"] not in ("succeeded", "failed", "cancelled", "timed_out")
                    or event["ended_at"] is not None
                )
            ):
                cached.update(classification_state="unavailable", node_kinds="{}")
            if item["status"] not in (
                "queued",
                "running",
                "waiting",
                "suspended",
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
            ):
                item["status"] = None
                cached.update(classification_state="unavailable", node_kinds="{}")
                event = {**event, "started_at": None, "updated_at": None, "ended_at": None}
            matching = origins.get(item["id"], set())
            origin = "retry" if item["parent_run_id"] else event.get("explicit_origin")
            if origin is None and len(matching) == 1:
                origin = next(iter(matching))
            # A call without its scoped parent identity is not supported historical evidence.
            if origin == "call":
                origin = None
            facts.append(
                {
                    **params,
                    **item,
                    **cached,
                    "origin": origin,
                    "test": origin == "test",
                    "paused": item["paused"] == "true",
                    "started_at": event.get("started_at"),
                    "updated_at": event.get("updated_at"),
                    "ended_at": event.get("ended_at")
                    if item["status"] in ("succeeded", "failed", "cancelled", "timed_out")
                    else None,
                    "last_event_sequence": event.get("last_event_sequence", 0),
                }
            )
        connection.execute(
            sa.text("""INSERT INTO run_facts(tenant_id,project_id,environment_id,run_id,definition_version_id,
            workflow_name,workflow_version,activation_id,activation_name,activation_revision,status,paused,test,origin,
            retried_from_run_id,business_key,correlation_key,started_at,updated_at,ended_at,active_incidents,
            classification_state,classification_policy,pinned_ir_version,pinned_features,node_kinds,last_event_sequence)
            VALUES(:tenant,:project,:environment,:id,:definition_version_id,:workflow_name,:workflow_version,
            :activation_id,:activation_name,:activation_revision,:status,:paused,:test,:origin,:parent_run_id,
            :business_key,:correlation_key,:started_at,:updated_at,:ended_at,:active_incidents,:classification_state,
            :classification_policy,:pinned_ir_version,cast(:pinned_features AS jsonb),cast(:node_kinds AS
            jsonb),:last_event_sequence)"""),
            facts,
        )
        backfill_incidents(connection, batch, events)
        after = ids[-1]
    connection.execute(
        sa.text(
            f"INSERT INTO worker_facts(tenant_id,project_id,environment_id,worker_id) "
            f"SELECT tenant_id,project_id,environment_id,id FROM worker_instances WHERE {SCOPE}"
        ),
        params,
    )


def scoped_work(connection, *, refresh: bool = False) -> None:
    for tenant in connection.execute(sa.text("SELECT weave_tenant_ids()")).scalars():
        connection.execute(sa.text("SELECT set_config('weave.tenant_id',:value,true)"), {"value": str(tenant)})
        for project in connection.execute(
            sa.text("SELECT id FROM projects WHERE tenant_id=:tenant ORDER BY id"), {"tenant": tenant}
        ).scalars():
            connection.execute(sa.text("SELECT set_config('weave.project_id',:value,true)"), {"value": str(project)})
            for environment in connection.execute(
                sa.text("SELECT id FROM environments WHERE tenant_id=:tenant AND project_id=:project ORDER BY id"),
                {"tenant": tenant, "project": project},
            ).scalars():
                connection.execute(
                    sa.text("SELECT set_config('weave.environment_id',:value,true)"), {"value": str(environment)}
                )
                params = {"tenant": tenant, "project": project, "environment": environment}
                if not refresh:
                    backfill_scope(connection, params)
                    continue
                after = None
                while True:
                    ids = list(
                        connection.execute(
                            sa.text(
                                f"SELECT id FROM runs WHERE {SCOPE} AND state->>'status' NOT IN ({TERMINAL}) "
                                "AND (cast(:after AS uuid) IS NULL OR id>:after) ORDER BY id LIMIT 10000"
                            ),
                            {**params, "after": after},
                        ).scalars()
                    )
                    if not ids:
                        break
                    connection.execute(
                        sa.text("SELECT weave_reserve_refresh('run',id,false) FROM unnest(cast(:ids AS uuid[])) id"),
                        {"ids": ids},
                    )
                    after = ids[-1]


def upgrade() -> None:
    connection = op.get_bind()
    if not connection.scalar(sa.text("SELECT has_function_privilege(current_user,'weave_tenant_ids()','EXECUTE')")):
        raise RuntimeError("Migration requires explicit EXECUTE on weave_tenant_ids() for tenant-scoped backfill")
    if connection.scalar(sa.text("SELECT to_regclass('public.run_call_links')")) is not None or connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND "
            "table_name='step_instances' AND column_name='handled')"
        )
    ):
        raise RuntimeError("Reconcile call and handled-failure history with the operational backfill before migrating")
    previous = {
        name: connection.scalar(sa.text("SELECT current_setting(:name,true)"), {"name": "weave." + name})
        for name in ("tenant_id", "project_id", "environment_id")
    }
    try:
        with connection.begin_nested():
            tables()
            indexes()
            control_accounting()
            scoped_work(connection)
            for table, key in TABLE_KEYS.items():
                op.execute(f"""INSERT INTO operation_usage(tenant_id,project_id,environment_id,metric,value)
                    SELECT tenant_id,project_id,'00000000-0000-0000-0000-000000000000'::uuid,'ordinary_bytes',
                      sum((public.weave_row_metrics('{table}',to_jsonb(f))->>'ordinary_bytes')::bigint)
                    FROM {table} f GROUP BY tenant_id,project_id
                    ON CONFLICT(tenant_id,project_id,environment_id,metric)
                    DO UPDATE SET value=operation_usage.value+excluded.value""")
                secure(table, key)
            scoped_work(connection, refresh=True)
            op.execute("UPDATE weave_schema_version SET version='0031_operations_facts'")
    finally:
        for name, value in previous.items():
            connection.execute(
                sa.text("SELECT set_config(:name,:value,true)"), {"name": "weave." + name, "value": value or ""}
            )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
