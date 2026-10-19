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

"""Validated legacy fixtures and independent logical-usage inventory."""

import json
import runpy
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest
from firefly_weave.contracts.runtime import RunView, StartRunRequest
from firefly_weave.runtime.models import RunState, RuntimeEvent, Transition

FACT_TABLES = ("run_facts", "step_facts", "task_facts", "incident_facts", "worker_facts")
ZERO = UUID(int=0)
AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def identifier(value: int) -> UUID:
    return UUID(int=value)


async def seed_prior_head(connection: AsyncConnection, *, count: int = 10001) -> dict[str, UUID]:
    """Insert validated source models; no runtime service sees the previous schema."""
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))[
        "worker_runtime_fixture"
    ].__wrapped__()
    ids = {
        name: identifier(index)
        for index, name in enumerate(
            ("tenant", "project", "environment", "other_environment", "principal", "version", "activation"), 1
        )
    }
    ids.update(
        first_run=identifier(1001),
        last_run=identifier(1000 + count),
        incident=identifier(101),
        estimated_incident=identifier(102),
        retry=identifier(1003),
        active_run=identifier(1002),
        no_events=identifier(1004),
        malformed=identifier(1005),
        unsupported=identifier(1006),
        incoherent=identifier(1007),
        explicit=identifier(1008),
    )
    scope = Scope(tenant_id=ids["tenant"], project_id=ids["project"], environment_id=ids["environment"])
    artifact = fixture["artifact"]
    activation = Activation(
        id=ids["activation"],
        revision=1,
        name="worker-flow",
        request=ActivationRequest(
            version_id=ids["version"],
            artifact_digest=artifact.digest,
            scope=scope,
        ),
    )
    await connection.execute(text("INSERT INTO tenants VALUES(:tenant,'facts')"), ids)
    await connection.execute(text("INSERT INTO projects VALUES(:project,:tenant,'facts')"), ids)
    await connection.execute(
        text(
            "INSERT INTO environments VALUES(:environment,:tenant,:project,'first'),"
            "(:other_environment,:tenant,:project,'second')"
        ),
        ids,
    )
    await connection.execute(text("INSERT INTO principals(id,kind) VALUES(:principal,'application')"), ids)
    await connection.execute(
        text(
            "INSERT INTO definition_versions VALUES(:version,:tenant,:project,'Workflow',"
            "'worker-flow','1.0.0',:digest,:definition_digest,cast(:document AS jsonb),cast(:artifact AS jsonb))"
        ),
        {
            **ids,
            "digest": artifact.digest,
            "definition_digest": artifact.source_hash,
            "document": fixture["source"],
            "artifact": artifact.to_bytes().decode(),
        },
    )
    await connection.execute(
        text(
            "INSERT INTO activation_revisions VALUES(:activation,:tenant,:project,:environment,"
            "'worker-flow',1,:version,cast(:payload AS jsonb))"
        ),
        {**ids, "payload": activation.model_dump_json()},
    )
    run_rows, event_rows = [], []
    for index in range(1, count + 1):
        run = identifier(1000 + index)
        state = RunState(
            status="waiting" if index == 2 else "succeeded",
            accepted_sequence=1,
            admission_policy="classified-v1",
            input=3,
        )
        request = StartRunRequest(
            activation_id=activation.id, input=3, business_key='España 雪 " \\', correlation_key="case-" + str(index)
        )
        view = RunView(
            id=run,
            activation=activation,
            artifact_digest=artifact.digest,
            state=state,
            business_key=request.business_key,
            correlation_key=request.correlation_key,
        )
        run_rows.append(
            {
                **ids,
                "id": run,
                "artifact": artifact.to_bytes().decode(),
                "payload": activation.model_dump_json(),
                "request": request.model_dump_json(),
                "state": state.model_dump_json(),
            }
        )
        if index != 4:
            event = RuntimeEvent(
                id=identifier(100000 + index),
                type="started",
                sequence=1,
                timestamp=AT,
                data={"origin": "test"} if index == 8 else {"input": 3},
            )
            event_rows.append(
                {
                    **ids,
                    "run": run,
                    "id": event.id,
                    "sequence": 1,
                    "type": event.type,
                    "data": json.dumps(event.data),
                    "at": event.timestamp,
                    "transition": Transition(state=state).model_dump_json(),
                    "response": view.model_dump_json(),
                }
            )
    for offset in range(0, len(run_rows), 1000):
        await connection.execute(
            text(
                "INSERT INTO runs VALUES(:id,:tenant,:project,:environment,:activation,:principal,"
                "cast(:artifact AS jsonb),cast(:payload AS jsonb),cast(:request AS jsonb),cast(:state AS jsonb))"
            ),
            run_rows[offset : offset + 1000],
        )
    for offset in range(0, len(event_rows), 1000):
        await connection.execute(
            text(
                "INSERT INTO run_events VALUES(:tenant,:project,:environment,:run,:id,:sequence,"
                ":type,cast(:data AS jsonb),:at,'fixture',cast(:transition AS jsonb),cast(:response AS jsonb))"
            ),
            event_rows[offset : offset + 1000],
        )
    await connection.execute(
        text("INSERT INTO run_retry_links VALUES(:tenant,:project,:environment,:retry,:first_run)"), ids
    )
    event = RuntimeEvent(
        id=identifier(200001),
        type="incident_opened",
        sequence=2,
        timestamp=AT + timedelta(seconds=5),
        data={"node_id": "work[3]", "generation": 2, "code": "WV-TASK-AMBIGUOUS"},
    )
    state = RunState(
        status="suspended",
        accepted_sequence=2,
        admission_policy="classified-v1",
        input=3,
        incidents={"work[3]:2": {"node_id": "work[3]", "generation": 2, "code": "WV-TASK-AMBIGUOUS"}},
    )
    await connection.execute(
        text("UPDATE runs SET state=cast(:state AS jsonb) WHERE id=:active_run"),
        {**ids, "state": state.model_dump_json()},
    )
    await connection.execute(
        text(
            "INSERT INTO run_events VALUES(:tenant,:project,:environment,:active_run,:id,2,"
            "'incident_opened',cast(:data AS jsonb),:at,'fixture',cast(:transition AS jsonb),cast(:response AS jsonb))"
        ),
        {
            **ids,
            "id": event.id,
            "data": json.dumps(event.data),
            "at": event.timestamp,
            "transition": Transition(state=state).model_dump_json(),
            "response": RunView(
                id=ids["active_run"], activation=activation, artifact_digest=artifact.digest, state=state
            ).model_dump_json(),
        },
    )
    await connection.execute(
        text(
            "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,"
            "node_id,generation,origin_code,code,status,revision) VALUES(:incident,:tenant,:project,:environment,"
            ":active_run,:incident_key,'work[3]',2,'WV-TASK-AMBIGUOUS','WV-TASK-AMBIGUOUS','active',1),"
            "(:estimated_incident,:tenant,:project,:environment,:first_run,'@legacy',NULL,NULL,"
            "'WV-TASK-AMBIGUOUS','WV-TASK-AMBIGUOUS','closed',1)"
        ),
        {**ids, "incident_key": "work[3]:2"},
    )
    # Corruption trials begin with validated fixture models and change only the evidence under test.
    await connection.execute(text("UPDATE runs SET artifact='{}' WHERE id=:malformed"), ids)
    await connection.execute(
        text(
            "UPDATE runs SET artifact=jsonb_set(artifact,'{executable,irVersion}',"
            "'\"weave/ir-future\"') WHERE id=:unsupported"
        ),
        ids,
    )
    await connection.execute(text("UPDATE run_events SET transition='{}' WHERE run_id=:incoherent"), ids)
    return ids


async def recompute_usage(connection: AsyncConnection, tenant: UUID, project: UUID) -> dict[tuple[UUID, str], int]:
    """Inventory charged tables and retained control provenance independently of counters."""
    tables = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE t.tgname='operation_usage' "
                    "AND n.nspname='public' "
                    "ORDER BY c.relname"
                )
            )
        )
        .scalars()
        .all()
    )
    totals = defaultdict(int)
    params = {"tenant": tenant, "project": project}
    for table in tables:
        projection = f"public.weave_{table}_account(r)" if table in ("runs", "run_events") else "to_jsonb(r)"
        rows = (
            await connection.execute(
                text(
                    f"SELECT item,public.weave_row_metrics('{table}',item) AS metrics FROM "
                    f"(SELECT {projection} AS item FROM {table} r) q WHERE "
                    "coalesce(item->>'tenant_id',"
                    "item->'event'->'scope'->>'tenant_id')=cast(cast(:tenant AS uuid) AS text) AND "
                    "coalesce(item->>'project_id',"
                    "item->'event'->'scope'->>'project_id')=cast(cast(:project AS uuid) AS text)"
                ),
                params,
            )
        ).mappings()
        for row in rows:
            for metric, amount in row["metrics"].items():
                dimension = (
                    ZERO
                    if metric in ("ordinary_bytes", "runs_retained", "debug_rows")
                    else UUID(row["item"].get("environment_id", str(ZERO)))
                )
                if metric == "outbox_pending" and await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM operation_control_deliveries WHERE tenant_id=:tenant "
                        "AND project_id=:project AND id=:id)"
                    ),
                    {**params, "id": row["item"]["id"]},
                ):
                    metric, dimension = "control_outbox_pending", ZERO
                totals[dimension, metric] += int(amount)
    allocation = (
        (
            await connection.execute(
                text(
                    "SELECT coalesce(sum(control_bytes),0) AS spent,count(*) AS rows "
                    "FROM operation_allocations WHERE tenant_id=:tenant AND project_id=:project"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    totals[ZERO, "ordinary_bytes"] -= int(allocation["spent"])
    totals[ZERO, "control_bytes"] += int(allocation["spent"]) + 512 * allocation["rows"]
    reservation = (
        (
            await connection.execute(
                text(
                    "SELECT coalesce(sum(amount),0) AS amount,"
                    "coalesce(sum(outbox_slots),0) AS slots,count(*) AS rows FROM operation_reservations "
                    "WHERE tenant_id=:tenant AND project_id=:project"
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    totals[ZERO, "ordinary_bytes"] += 512 * reservation["rows"]
    totals[ZERO, "control_reserved"] += int(reservation["amount"])
    totals[ZERO, "control_outbox_reserved"] += int(reservation["slots"])
    return {key: value for key, value in totals.items() if value}


async def assert_usage(connection: AsyncConnection, ids: dict[str, UUID]) -> None:
    expected = await recompute_usage(connection, ids["tenant"], ids["project"])
    actual = (
        await connection.execute(
            text(
                "SELECT environment_id,metric,value FROM operation_usage "
                "WHERE tenant_id=:tenant AND project_id=:project AND value<>0"
            ),
            ids,
        )
    ).all()
    assert {(environment, metric): value for environment, metric, value in actual} == expected


async def seed_worker_source(connection, ids):
    """Validated registration rows, including for databases without fact tables."""
    from firefly_weave.contracts.workers import WorkerInstance, WorkerRelease

    fixture = runpy.run_path(str(Path(__file__).resolve().parents[1] / "conftest.py"))[
        "worker_runtime_fixture"
    ].__wrapped__()
    contract = next(
        value.definition.value for key, value in fixture["catalog"].resources.items() if key[0] == "TaskCapability"
    )
    release = WorkerRelease(id=identifier(301), image_digest="sha256:" + "a" * 64, capabilities=[contract])
    worker = WorkerInstance(
        id=identifier(302), release_id=release.id, principal_id=ids["principal"], task_types=["echo@1.2.0"], capacity=1
    )
    ids.update(worker=worker.id, release=release.id, task=identifier(400000))
    await connection.execute(
        text(
            "INSERT INTO worker_releases VALUES(:release,:tenant,:project,:environment,:digest,cast(:payload AS jsonb))"
        ),
        {**ids, "digest": release.image_digest, "payload": release.model_dump_json()},
    )
    await connection.execute(
        text(
            "INSERT INTO worker_instances(id,tenant_id,project_id,environment_id,release_id,"
            "principal_id,payload) VALUES(:worker,:tenant,:project,:environment,:release,:principal,"
            "cast(:payload AS jsonb))"
        ),
        {**ids, "payload": worker.model_dump_json()},
    )


async def seed_fact_children(connection, ids, *, steps=1, tasks=1):
    """Explicit source and projection inserts; runtime writers are tested separately."""
    from firefly_weave.runtime.models import TaskIntent

    await seed_worker_source(connection, ids)
    await connection.execute(
        text(
            "INSERT INTO worker_facts(tenant_id,project_id,environment_id,worker_id) "
            "VALUES(:tenant,:project,:environment,:worker)"
        ),
        ids,
    )
    for index in range(steps):
        node = "work" + str(index)
        await connection.execute(
            text("INSERT INTO step_instances VALUES(:tenant,:project,:environment,:active_run,:node,'waiting','null')"),
            {**ids, "node": node},
        )
        await connection.execute(
            text(
                "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,"
                "kind,status,scheduled_at) VALUES(:tenant,:project,:environment,:active_run,:node,"
                "'action','running',:at)"
            ),
            {**ids, "node": node, "at": AT},
        )
    for index in range(tasks):
        task = TaskIntent(
            node_id="work" + str(index),
            action_digest="a" * 64,
            action_reference="echo-action@2.0.0",
            input=3,
            connection_slot=None,
            task_type="echo",
            task_version="1.2.0",
            deadline=AT + timedelta(hours=1),
        )
        params = {
            **ids,
            "id": identifier(400000 + index),
            "node": task.node_id,
            "payload": task.model_dump_json(),
            "key": "task-" + str(index),
            "at": AT,
        }
        await connection.execute(
            text(
                "INSERT INTO task_intents VALUES(:id,:tenant,:project,:environment,:active_run,"
                ":node,cast(:payload AS jsonb),:release,:key,'ready')"
            ),
            params,
        )
        await connection.execute(
            text(
                "INSERT INTO task_facts(tenant_id,project_id,environment_id,task_id,run_id,"
                "node_id,task_type,status,created_at,ready_since) VALUES(:tenant,:project,:environment,:id,:active_run,"
                ":node,'echo@1.2.0','ready',:at,:at)"
            ),
            params,
        )


async def scope_as_app(connection, ids, *, environment=None, tenant=None, project=None):
    await connection.execute(text("SET LOCAL ROLE weave_app"))
    for field, value in (
        ("tenant", tenant or ids["tenant"]),
        ("project", project or ids["project"]),
        ("environment", ids["environment"] if environment is None else environment),
    ):
        await connection.execute(
            text("SELECT set_config(:name,:value,true)"), {"name": "weave." + field + "_id", "value": str(value)}
        )
    await connection.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:' "
            "|| cast(cast(:tenant AS uuid) AS text) || ':' || cast(cast(:project AS uuid) AS text),0))"
        ),
        ids,
    )


async def seed_ingress_receipts(connection, ids):
    """Validated start and signal receipts for every durable ingress family."""
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.broker import AcceptedBrokerReceipt, BrokerTrigger, SourceBinding
    from firefly_weave.contracts.connectors import ConnectionRevision
    from firefly_weave.contracts.email import EmailReceipt, EmailSourceRequest
    from firefly_weave.contracts.providers import ProviderEvent, ProviderReceipt, ProviderSource
    from firefly_weave.contracts.schedules import ScheduleView
    from firefly_weave.triggers.models import Trigger, TriggerReceipt

    source = json.dumps(json.loads(Path("tests/fixtures/e2-provider/connector.json").read_text())["manifest"])
    compiled = compile_source(
        source, format="json", catalog=CatalogSnapshot.from_definitions([], adapters=["e2-inbox-fixture"])
    )
    assert compiled.ok
    document = json.loads(source)
    connector = identifier(501)
    await connection.execute(
        text(
            "INSERT INTO definition_versions VALUES(:id,:tenant,:project,'Connector',:name,"
            ":version,:digest,:definition_digest,cast(:document AS jsonb),cast(:artifact AS jsonb))"
        ),
        {
            **ids,
            "id": connector,
            "name": document["metadata"]["name"],
            "version": document["metadata"]["version"],
            "digest": compiled.artifact.digest,
            "definition_digest": compiled.artifact.source_hash,
            "document": source,
            "artifact": compiled.artifact.to_bytes().decode(),
        },
    )
    connection_model = ConnectionRevision(
        id=identifier(502),
        revision=1,
        name="inbox",
        connector_version_id=connector,
        connector="inbox@1.0.0",
        connector_digest=compiled.artifact.digest,
        adapter="inbox",
    )
    await connection.execute(
        text(
            "INSERT INTO connection_revisions VALUES(:id,:tenant,:project,:environment,'inbox',1,"
            ":connector,cast(:payload AS jsonb))"
        ),
        {
            **ids,
            "id": connection_model.id,
            "connector": connector,
            "payload": connection_model.model_dump_json(by_alias=True),
        },
    )
    for signal in (False, True):
        base = 600 if not signal else 700
        common = (
            {"kind": "signal", "run_id": identifier(1014), "signal": "continue"}
            if signal
            else {"kind": "run", "activation_id": ids["activation"]}
        )
        webhook = Trigger(
            id=identifier(base + 1), principal_id=ids["principal"], name="webhook", secret_ref="signing", **common
        )
        await connection.execute(
            text(
                "INSERT INTO trigger_routes(id,tenant_id,project_id,environment_id,principal_id,"
                "activation_id,run_id,secret_ref,tolerance_seconds,max_body_bytes,payload) VALUES(:id,:tenant,:project,"
                ":environment,:principal,:activation_id,:run_id,'signing',300,1048576,cast(:payload AS jsonb))"
            ),
            {
                **ids,
                "id": webhook.id,
                "activation_id": webhook.activation_id,
                "run_id": webhook.run_id,
                "payload": webhook.model_dump_json(),
            },
        )
        for index in [14] if signal else [9, 15, 8, 3]:
            receipt = TriggerReceipt(
                id=identifier(base + 20 + index),
                trigger_id=webhook.id,
                event_id=str(index),
                run_id=identifier(1000 + index),
                signal_id=identifier(900) if signal else None,
            )
            await connection.execute(
                text(
                    "INSERT INTO trigger_receipts VALUES(:id,:tenant,:project,:environment,:trigger,"
                    ":event,'fixture',:run,cast(:payload AS jsonb))"
                ),
                {
                    **ids,
                    "id": receipt.id,
                    "trigger": receipt.trigger_id,
                    "event": receipt.event_id,
                    "run": receipt.run_id,
                    "payload": receipt.model_dump_json(),
                },
            )
        for family, offset in (("broker", 2), ("provider", 3)):
            binding = SourceBinding(
                id=identifier(base + offset + 50),
                connection_revision_id=connection_model.id,
                source_kind="kafka-trigger" if family == "broker" else "provider-source",
                source_id=identifier(base + offset),
                principal_id=ids["principal"],
                source_fingerprint="fixture",
            )
            await connection.execute(
                text(
                    "INSERT INTO connection_source_bindings(id,tenant_id,project_id,environment_id,"
                    "connection_revision_id,source_kind,source_id,principal_id,source_fingerprint) "
                    "VALUES(:id,:tenant,:project,"
                    ":environment,:connection,:kind,:source,:principal,'fixture')"
                ),
                {
                    **ids,
                    "id": binding.id,
                    "connection": connection_model.id,
                    "kind": binding.source_kind,
                    "source": binding.source_id,
                },
            )
            if family == "broker":
                route = BrokerTrigger(
                    id=binding.source_id,
                    binding_id=binding.id,
                    principal_id=ids["principal"],
                    name="broker",
                    connection_revision_id=connection_model.id,
                    cluster_id="cluster",
                    topic="events",
                    dead_letter_policy="receipt",
                    **common,
                )
                await connection.execute(
                    text(
                        "INSERT INTO broker_routes(id,tenant_id,project_id,environment_id,binding_id,"
                        "principal_id,activation_id,run_id,payload) VALUES(:id,:tenant,:project,"
                        ":environment,:binding,:principal,"
                        ":activation_id,:run_id,cast(:payload AS jsonb))"
                    ),
                    {
                        **ids,
                        "id": route.id,
                        "binding": binding.id,
                        "activation_id": route.activation_id,
                        "run_id": route.run_id,
                        "payload": route.model_dump_json(),
                    },
                )
                for index in [14] if signal else [11, 15]:
                    receipt = AcceptedBrokerReceipt(
                        id=identifier(base + 100 + index),
                        trigger_id=route.id,
                        event_id=identifier(base + 200 + index),
                        run_id=identifier(1000 + index),
                        signal_id=identifier(900) if signal else None,
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO broker_events VALUES(:id,:tenant,:project,:environment,"
                            ":trigger,'cluster',:event,'fixture',:run,cast(:payload AS jsonb))"
                        ),
                        {
                            **ids,
                            "id": receipt.id,
                            "trigger": route.id,
                            "event": receipt.event_id,
                            "run": receipt.run_id,
                            "payload": receipt.model_dump_json(),
                        },
                    )
            else:
                provider = ProviderSource(
                    id=binding.source_id,
                    scope=Scope(tenant_id=ids["tenant"], project_id=ids["project"], environment_id=ids["environment"]),
                    binding_id=binding.id,
                    principal_id=ids["principal"],
                    name="provider",
                    provider="inbox",
                    package="e2-inbox-fixture",
                    package_version="1.0.0",
                    adapter_version="1.0.0",
                    schema_digest="a" * 64,
                    connection_revision_id=connection_model.id,
                    policy={},
                    **common,
                )
                await connection.execute(
                    text(
                        "INSERT INTO provider_sources(id,tenant_id,project_id,environment_id,"
                        "binding_id,principal_id,activation_id,run_id,payload) VALUES(:id,:tenant,"
                        ":project,:environment,"
                        ":binding,:principal,:activation_id,:run_id,cast(:payload AS jsonb))"
                    ),
                    {
                        **ids,
                        "id": provider.id,
                        "binding": binding.id,
                        "activation_id": provider.activation_id,
                        "run_id": provider.run_id,
                        "payload": provider.model_dump_json(),
                    },
                )
                for index in [14] if signal else [13, 17]:
                    event = ProviderEvent(event_id=str(index), kind="message", payload={})
                    receipt = ProviderReceipt(
                        id=identifier(base + 300 + index),
                        source_id=provider.id,
                        provider="inbox",
                        event_id=event.event_id,
                        kind=event.kind,
                        fingerprint=event.fingerprint,
                        received_at=AT,
                        state="pending" if index == 17 else "dispatched",
                        run_id=identifier(1000 + index),
                        signal_id=identifier(900) if signal else None,
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO provider_receipts VALUES(:id,:tenant,:project,:environment,"
                            ":source,:event,'message',:fingerprint,cast(:payload AS jsonb),'{}',"
                            "cast(:receipt AS jsonb),:state,:at)"
                        ),
                        {
                            **ids,
                            "id": receipt.id,
                            "source": provider.id,
                            "event": event.event_id,
                            "fingerprint": event.fingerprint,
                            "payload": event.model_dump_json(),
                            "receipt": receipt.model_dump_json(),
                            "state": receipt.state,
                            "at": AT,
                        },
                    )
        email = EmailSourceRequest(
            connection_revision_id=connection_model.id, activation_id=None if signal else ids["activation"]
        )
        email_id = identifier(base + 4)
        await connection.execute(
            text(
                "INSERT INTO email_sources(id,tenant_id,project_id,environment_id,principal_id,"
                "connection_revision_id,activation_id,mailbox,policy) VALUES(:id,:tenant,:project,"
                ":environment,:principal,"
                ":connection,:activation_id,'INBOX',cast(:policy AS jsonb))"
            ),
            {
                **ids,
                "id": email_id,
                "connection": connection_model.id,
                "activation_id": email.activation_id,
                "policy": email.model_dump_json(),
            },
        )
        receipt = EmailReceipt(
            id=identifier(base + 400),
            source_id=email_id,
            transport_key="mail",
            message_id=None,
            state="dispatched",
            run_id=identifier(1014 if signal else 1012),
            signal_id=identifier(900) if signal else None,
        )
        await connection.execute(
            text(
                "INSERT INTO email_receipts(id,tenant_id,project_id,environment_id,source_id,"
                "transport_key,state,run_id,signal_id) VALUES(:id,:tenant,:project,:environment,"
                ":source,'mail','dispatched',"
                ":run,:signal)"
            ),
            {**ids, "id": receipt.id, "source": email_id, "run": receipt.run_id, "signal": receipt.signal_id},
        )
    schedule = ScheduleView(
        id=identifier(550),
        cron="0 * * * *",
        activation_id=ids["activation"],
        revision=1,
        principal_id=ids["principal"],
        status="disabled",
        next_due_at=AT,
    )
    await connection.execute(
        text("INSERT INTO schedules VALUES(:id,:tenant,:project,:environment,1,'disabled',:at,NULL)"),
        {**ids, "id": schedule.id, "at": AT},
    )
    await connection.execute(
        text(
            "INSERT INTO schedule_revisions VALUES(:tenant,:project,:environment,:id,1,:principal,"
            ":activation,cast(:payload AS jsonb),:at)"
        ),
        {**ids, "id": schedule.id, "payload": schedule.model_dump_json(), "at": AT},
    )
    await connection.execute(
        text(
            "INSERT INTO schedule_occurrences(tenant_id,project_id,environment_id,schedule_id,"
            "revision,instant,through,observed_at,kind,run_id) VALUES(:tenant,:project,:environment,"
            ":id,1,:at,:at,:at,'started',:run)"
        ),
        {**ids, "id": schedule.id, "at": AT, "run": identifier(1010)},
    )


# A deterministic operational dataset in one environment, seeded through the owner connection with quota
# accounting enabled. Source rows are copies of two admitted runs (one cancelled, one with a ready task); the
# metadata facts vary by position so every filter, order and tie has rows to find.
DATASET_BATCH = 500
_RUN = "md5(:seed||'/run/'||i)::uuid"
_TERMINAL_STATUS = "('succeeded','failed','cancelled','timed_out')"
_STATUS = (
    "CASE WHEN i<=:ready THEN 'running' WHEN i%100<85 THEN 'succeeded' WHEN i%100<93 THEN 'failed' "
    "WHEN i%100<96 THEN 'running' WHEN i%100<98 THEN 'cancelled' WHEN i%100=98 THEN 'waiting' ELSE 'timed_out' END"
)
_ORIGIN = (
    "CASE WHEN i%50=0 THEN 'test' WHEN i%1000=999 THEN 'call' WHEN i%100<50 THEN 'manual' "
    "WHEN i%100<70 THEN 'webhook' WHEN i%100<90 THEN 'schedule' WHEN i%100<95 THEN 'broker' "
    "WHEN i%100<98 THEN 'retry' WHEN i%100=98 THEN 'email' ELSE 'provider' END"
)
# Three runs share each start second (ties); one in 997 has no known history.
_STARTED = "CASE WHEN i%997=0 THEN NULL ELSE cast(:anchor AS timestamptz)-((:runs-i)/3)*interval '1 second' END"
_UPDATED = (
    "CASE WHEN i%997=0 THEN NULL ELSE cast(:anchor AS timestamptz)-((:runs-i)/3)*interval '1 second'"
    "+(i%7)*interval '1 second' END"
)


async def seed_operations_dataset(
    case,
    *,
    runs: int,
    incidents: int,
    workers: int,
    ready_tasks: int,
    observations: int = 0,
    seed: str = "weave-operations-1",
    steps_per_run: bool = False,
) -> dict:
    """Seed `runs` runs (and their facts), `incidents` incidents, `workers` workers and `ready_tasks` ready tasks.

    The operation policy of this fixture-owned database is raised for the dataset and recorded in the result.
    """
    import time

    from firefly_weave.access.audit import AuditContext

    assert runs >= 20 * incidents and runs >= ready_tasks
    started = time.monotonic()
    done = await case.start(seed + "/done")
    async with case.tx() as tx:
        await case.runtime.cancel(
            tx, done.id, "dataset template", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    active = await case.start(seed + "/active")
    # Each active run holds outbox capacity, which a project bounds; ready tasks share about 13 to a run.
    active_runs = max(1, -(-ready_tasks // 13))
    params = {
        "seed": seed,
        "runs": runs,
        "ready": active_runs,
        "tasks": ready_tasks,
        "incidents": incidents,
        "done": done.id,
        "active": active.id,
        "tenant": case.scope.tenant_id,
        "project": case.scope.project_id,
        "environment": case.scope.environment_id,
    }
    limits = {
        "runs_retained": 2 * runs + 1000,
        "runs_active": 2 * active_runs + 1000,
        "tasks_active": 2 * ready_tasks + 1000,
        "ordinary_bytes": 2**62,
        "control_bytes": 2**62,
    }
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE operation_policy SET limits=limits||cast(:limits AS jsonb)"), {"limits": json.dumps(limits)}
        )
        params["anchor"] = await session.scalar(text("SELECT date_trunc('second',clock_timestamp())"))
        task = (
            (await session.execute(text("SELECT id,node_id FROM task_intents WHERE run_id=:active"), params))
            .mappings()
            .one()
        )
        params["task"] = task["id"]
        params["kind"] = await session.scalar(
            text("SELECT node_kinds->>:node FROM run_facts WHERE run_id=:active"), {**params, "node": task["node_id"]}
        )
        params["node"] = task["node_id"]
    for low in range(1, runs + 1, DATASET_BATCH):
        high = min(runs, low + DATASET_BATCH - 1)
        batch = {**params, "low": low, "high": high}
        async with case.owner.begin() as session:
            for template, lower, upper in (
                ("active", low, min(high, active_runs)),
                ("done", max(low, active_runs + 1), high),
            ):
                if lower > upper:
                    continue
                await session.execute(
                    text(
                        "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
                        f"jsonb_build_object('id',{_RUN}))).* FROM (SELECT * FROM runs WHERE id=:template) r "
                        "CROSS JOIN generate_series(cast(:lower AS bigint),cast(:upper AS bigint)) i"
                    ),
                    {**batch, "template": params[template], "lower": lower, "upper": upper},
                )
                await session.execute(
                    text(
                        "INSERT INTO run_facts SELECT (jsonb_populate_record(NULL::run_facts,to_jsonb(f)||"
                        "jsonb_build_object("
                        f"'run_id',{_RUN},"
                        "'workflow_name','workflow-'||lpad((i%20)::text,2,'0'),"
                        "'workflow_version','1.'||(i%5)||'.0',"
                        "'activation_id',md5(:seed||'/activation/'||(i%40))::uuid,"
                        "'activation_name','activation-'||(i%40),"
                        f"'status',{_STATUS},"
                        f"'origin',{_ORIGIN},"
                        "'test',i%50=0,"
                        "'caller_run_id',CASE WHEN i%1000=999 AND i%50<>0 THEN md5(:seed||'/run/1')::uuid END,"
                        "'caller_node_id',CASE WHEN i%1000=999 AND i%50<>0 THEN 'work' END,"
                        "'caller_instance_key',CASE WHEN i%1000=999 AND i%50<>0 THEN '' END,"
                        "'retried_from_run_id',CASE WHEN i%50<>0 AND i%1000<>999 AND i%100 BETWEEN 95 AND 97 "
                        "THEN md5(:seed||'/run/'||(i-1))::uuid END,"
                        "'business_key','order-ñ-雪-'||i,"
                        "'correlation_key','case-'||(i/10),"
                        f"'started_at',{_STARTED},"
                        f"'updated_at',{_UPDATED},"
                        f"'ended_at',CASE WHEN {_STATUS} IN {_TERMINAL_STATUS} THEN {_UPDATED} END,"
                        "'failed_node_id',CASE WHEN "
                        f"{_STATUS}='failed' THEN :node END,"
                        "'failed_error_code',CASE WHEN "
                        f"{_STATUS}='failed' THEN 'WV-TASK-FAILED' END,"
                        "'active_incidents',CASE WHEN i%20=0 AND i/20<=:incidents AND (i/20)%5=1 THEN 1 ELSE 0 END,"
                        "'handled_errors',CASE WHEN i%100=13 THEN 1+i%3 ELSE 0 END,"
                        "'classification_state',CASE WHEN i%997=0 THEN 'unavailable' WHEN i%1999=0 "
                        "THEN 'unsupported' ELSE 'available' END"
                        "))).* FROM (SELECT * FROM run_facts WHERE run_id=:template) f "
                        "CROSS JOIN generate_series(cast(:lower AS bigint),cast(:upper AS bigint)) i"
                    ),
                    {**batch, "template": params[template], "lower": lower, "upper": upper},
                )
            if steps_per_run:
                await session.execute(
                    text(
                        "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,kind,status,"
                        "scheduled_at,started_at,ended_at,attempts) SELECT :tenant,:project,:environment,"
                        f"{_RUN},:node,:kind,'succeeded',{_STARTED},{_STARTED},{_UPDATED},1 "
                        "FROM generate_series(cast(:low AS bigint),cast(:high AS bigint)) i WHERE i>:ready"
                    ),
                    batch,
                )
            await session.execute(
                text(
                    "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived) "
                    f"SELECT :tenant,:project,:environment,{_RUN},true "
                    "FROM generate_series(cast(:low AS bigint),cast(:high AS bigint)) i WHERE i%100=37"
                ),
                batch,
            )
    # The steps run: 200 instances of one step, four sharing each scheduled time.
    steps_run = runs // 2 if runs // 2 > active_runs else runs
    async with case.owner.begin() as session:
        params["steps_run"] = await session.scalar(
            text("SELECT md5(:seed||'/run/'||cast(:index AS bigint))::uuid"), {**params, "index": steps_run}
        )
        await session.execute(
            text(
                "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,kind,status,"
                "scheduled_at,started_at,ended_at,attempts) SELECT :tenant,:project,:environment,:steps_run,:node,"
                ":node||'['||k||']',:kind,'succeeded',cast(:anchor AS timestamptz)+(k/4)*interval '1 second',"
                "cast(:anchor AS timestamptz)+(k/4)*interval '1 second',"
                "cast(:anchor AS timestamptz)+(k/4+1)*interval '1 second',1 FROM generate_series(0,199) k"
            ),
            params,
        )
    # One incident for every twentieth run; one in five is active (never on a test run), the rest closed.
    for low in range(1, incidents + 1, DATASET_BATCH):
        batch = {**params, "low": low, "high": min(incidents, low + DATASET_BATCH - 1)}
        async with case.owner.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,"
                    "generation,origin_code,code,status,revision) SELECT md5(:seed||'/incident/'||k)::uuid,"
                    ":tenant,:project,:environment,md5(:seed||'/run/'||(20*k))::uuid,:node||chr(58)||'1',:node,1,"
                    "'WV-TASK-FAILED',CASE WHEN k%3=0 THEN 'WV-TASK-AMBIGUOUS' ELSE 'WV-TASK-FAILED' END,"
                    "CASE WHEN k%5=1 THEN 'active' ELSE 'closed' END,1 "
                    "FROM generate_series(cast(:low AS bigint),cast(:high AS bigint)) k"
                ),
                batch,
            )
            await session.execute(
                text(
                    "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,opened_at,"
                    "updated_at,workflow_name,workflow_version,test) SELECT f.tenant_id,f.project_id,"
                    "f.environment_id,md5(:seed||'/incident/'||k)::uuid,:node,f.started_at+interval '5 seconds',"
                    "f.started_at+interval '5 seconds',f.workflow_name,f.workflow_version,f.test "
                    "FROM generate_series(cast(:low AS bigint),cast(:high AS bigint)) k JOIN run_facts f "
                    "ON f.run_id=md5(:seed||'/run/'||(20*k))::uuid"
                ),
                batch,
            )
    # Ready tasks spread over the active runs, oldest first.
    for low in range(1, ready_tasks + 1, DATASET_BATCH):
        batch = {**params, "low": low, "high": min(ready_tasks, low + DATASET_BATCH - 1)}
        async with case.owner.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO task_intents SELECT (jsonb_populate_record(NULL::task_intents,to_jsonb(t)||"
                    "jsonb_build_object('id',md5(:seed||'/task/'||i)::uuid,"
                    "'run_id',md5(:seed||'/run/'||((i-1)%:ready+1))::uuid,'node_id',:node||'['||((i-1)/:ready)||']',"
                    "'operation_key',md5(:seed||'/operation/'||i),'status','ready',"
                    "'enqueued_at',cast(:anchor AS timestamptz)-(:tasks-i)*interval '1 second'))).* "
                    "FROM (SELECT * FROM task_intents WHERE id=:task) t "
                    "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                ),
                batch,
            )
            await session.execute(
                text(
                    "INSERT INTO task_facts SELECT (jsonb_populate_record(NULL::task_facts,to_jsonb(t)||"
                    "jsonb_build_object('task_id',md5(:seed||'/task/'||i)::uuid,"
                    "'run_id',md5(:seed||'/run/'||((i-1)%:ready+1))::uuid,'instance_key',:node||'['||((i-1)/:ready)||']',"
                    "'created_at',cast(:anchor AS timestamptz)-(:tasks-i)*interval '1 second',"
                    "'ready_since',cast(:anchor AS timestamptz)-(:tasks-i)*interval '1 second'))).* "
                    "FROM (SELECT * FROM task_facts WHERE task_id=:task) t "
                    "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                ),
                batch,
            )
    # Workers: most seen recently, some offline for hours, some never seen since registration.
    from test_worker_fleet_facts import add_deployment, add_observation, add_target, add_workers

    now = params["anchor"]
    recent = workers * 8 // 10
    offline = workers // 10
    await add_workers(
        case,
        recent,
        seen=[now - timedelta(seconds=index % 50) for index in range(recent)],
        host=None,
    )
    await add_workers(case, offline, seen=now - timedelta(hours=2))
    await add_workers(case, workers - recent - offline, seen=None, registered=None)
    if observations:
        target = await add_target(case, "bench")
        await add_deployment(case, target)
        # About 250 KB each, near the 256 KiB a stored observation may hold: 15 components of 100 members.
        members = {
            f"component-{component:02}": [f"bench-host-{component:02}-{member:03}-" + "x" * 32 for member in range(100)]
            for component in range(15)
        }
        first = await add_observation(case, target, members, at=now - timedelta(days=1))
        async with case.owner.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO deployment_observations SELECT (jsonb_populate_record(NULL::deployment_observations,"
                    "to_jsonb(o)||jsonb_build_object('id',md5(:seed||'/observation/'||k)::uuid))).* "
                    "FROM (SELECT * FROM deployment_observations WHERE id=:first) o "
                    "CROSS JOIN generate_series(2,cast(:count AS bigint)) k"
                ),
                {**params, "first": first, "count": observations},
            )
            params["observation_bytes"] = await session.scalar(
                text("SELECT octet_length(payload::text) FROM deployment_observations WHERE id=:first"),
                {"first": first},
            )
        await add_observation(case, target, {"engine": ["bench-current"]}, at=now)
    async with case.owner.begin() as session:
        for table in (
            "runs",
            "run_facts",
            "step_facts",
            "run_archives",
            "incidents",
            "incident_facts",
            "task_intents",
            "task_facts",
            "worker_instances",
            "worker_facts",
            "deployment_observations",
        ):
            await session.execute(text(f"ANALYZE {table}"))
        sizes = {
            row["name"]: row["bytes"]
            for row in (
                await session.execute(
                    text(
                        "SELECT relname AS name,pg_total_relation_size(oid) AS bytes FROM pg_class WHERE relname "
                        "IN ('runs','run_facts','step_facts','incidents','incident_facts','task_intents','task_facts',"
                        "'worker_instances','worker_facts','deployment_observations')"
                    )
                )
            ).mappings()
        }
        database_bytes = await session.scalar(text("SELECT pg_database_size(current_database())"))
    return {
        **params,
        "limits": limits,
        "seconds": round(time.monotonic() - started, 1),
        "table_bytes": sizes,
        "database_bytes": database_bytes,
        "workers": {"recent": recent, "offline": offline, "never_seen": workers - recent - offline},
        "observations": observations,
    }


async def recompute_usage_in_sql(
    connection: AsyncConnection, tenant: UUID, project: UUID
) -> dict[tuple[UUID, str], int]:
    """`recompute_usage` summed in PostgreSQL, for inventories too large to read row by row.

    It covers the same charged tables and control provenance; outbox rows owned by control deliveries are absent
    from these datasets, which the inventory checks instead of reclassifying them.
    """
    tables = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE t.tgname='operation_usage' "
                    "AND n.nspname='public' ORDER BY c.relname"
                )
            )
        )
        .scalars()
        .all()
    )
    assert not await connection.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM operation_control_deliveries WHERE tenant_id=:tenant AND project_id=:project)"
        ),
        {"tenant": tenant, "project": project},
    )
    totals: defaultdict[tuple[UUID, str], int] = defaultdict(int)
    for table in tables:
        projection = f"public.weave_{table}_account(r)" if table in ("runs", "run_events") else "to_jsonb(r)"
        rows = await connection.execute(
            text(
                "SELECT CASE WHEN m.key IN ('ordinary_bytes','runs_retained','debug_rows') THEN NULL "
                "ELSE q.item->>'environment_id' END AS environment,m.key AS metric,sum(m.value::numeric) AS amount "
                f"FROM (SELECT {projection} AS item FROM {table} r) q "
                f"CROSS JOIN LATERAL jsonb_each_text(public.weave_row_metrics('{table}',q.item)) m WHERE "
                "coalesce(q.item->>'tenant_id',q.item->'event'->'scope'->>'tenant_id')="
                "cast(cast(:tenant AS uuid) AS text) "
                "AND coalesce(q.item->>'project_id',q.item->'event'->'scope'->>'project_id')="
                "cast(cast(:project AS uuid) AS text) GROUP BY 1,2"
            ),
            {"tenant": tenant, "project": project},
        )
        for environment, metric, amount in rows:
            totals[UUID(environment) if environment else ZERO, metric] += int(amount)
    allocation = (
        await connection.execute(
            text(
                "SELECT coalesce(sum(control_bytes),0),count(*) FROM operation_allocations "
                "WHERE tenant_id=:tenant AND project_id=:project"
            ),
            {"tenant": tenant, "project": project},
        )
    ).one()
    totals[ZERO, "ordinary_bytes"] -= int(allocation[0])
    totals[ZERO, "control_bytes"] += int(allocation[0]) + 512 * allocation[1]
    reservation = (
        await connection.execute(
            text(
                "SELECT coalesce(sum(amount),0),coalesce(sum(outbox_slots),0),count(*) FROM operation_reservations "
                "WHERE tenant_id=:tenant AND project_id=:project"
            ),
            {"tenant": tenant, "project": project},
        )
    ).one()
    totals[ZERO, "ordinary_bytes"] += 512 * reservation[2]
    totals[ZERO, "control_reserved"] += int(reservation[0])
    totals[ZERO, "control_outbox_reserved"] += int(reservation[1])
    return {key: value for key, value in totals.items() if value}
