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

"""Real isolated PostgreSQL acceptance; owned databases and roles are retained."""

import asyncio
import importlib.util
import os
import secrets
import struct
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorInvocation, ResolvedSecret

pytestmark = pytest.mark.integration


@pytest.fixture
async def external_database(release_backends):
    value = os.environ.get("WEAVE_TEST_DATABASE_URL")
    if not value:
        pytest.fail("Real PostgreSQL required: set WEAVE_TEST_DATABASE_URL for colima-weave-tests", pytrace=False)
    url = release_backends["postgres_endpoint"](value, legacy_ports=(55433,))
    kwargs = dict(
        host="127.0.0.1",
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
        ssl=False,
        timeout=5,
    )
    try:
        admin = await asyncpg.connect(**kwargs)
        assert (
            await admin.fetchval("SELECT identity FROM weave_test_backend_guard")
            == "firefly-weave-b1-local-integration"
        )
    except Exception:
        pytest.fail("Task-owned PostgreSQL unavailable or marker missing", pytrace=False)
    suffix = uuid4().hex[:20]
    name = "weave_d1_" + suffix
    password = secrets.token_hex(24)
    roles = {kind: f"d1_{kind}_{suffix}" for kind in ("read", "write")}
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        for kind, role in roles.items():
            await admin.execute(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOINHERIT NOBYPASSRLS"
            )
            if kind == "read":
                await admin.execute(f"ALTER ROLE {role} SET default_transaction_read_only=on")
    finally:
        await admin.close()
    db = await asyncpg.connect(**(kwargs | {"database": name}))
    await db.execute(
        "CREATE SCHEMA business; CREATE TABLE business.customers(customer_id text PRIMARY "
        "KEY,status text,amount numeric,created date,data bytea)"
    )
    await db.execute("INSERT INTO business.customers(customer_id,status) VALUES('one','active'),('two','active')")
    await db.execute(
        "CREATE SCHEMA weave_connector; CREATE TABLE weave_connector.operations(scope text NOT "
        "NULL,operation_key text NOT NULL,request_hash text NOT NULL,result jsonb,PRIMARY "
        "KEY(scope,operation_key))"
    )
    for kind, role in roles.items():
        await db.execute(
            f"GRANT CONNECT ON DATABASE {name} TO {role}; GRANT USAGE ON SCHEMA business TO "
            f"{role}; GRANT SELECT ON business.customers TO {role}"
        )
        if kind == "write":
            await db.execute(
                f"GRANT INSERT,UPDATE,DELETE ON business.customers TO {role}; GRANT USAGE ON "
                f"SCHEMA weave_connector TO {role}; GRANT SELECT,INSERT,UPDATE ON "
                f"weave_connector.operations TO {role}"
            )
    try:
        yield {"db": db, "name": name, "password": password, "roles": roles, "kwargs": kwargs | {"database": name}}
    finally:
        await db.close()


@pytest.fixture
def postgres_connector():
    assert importlib.util.find_spec("firefly_weave.connectors.postgresql") is not None, "PostgreSQL adapter absent"
    from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy

    return PostgresConnector(PostgresPolicy(private_networks=("127.0.0.0/8",), plaintext_networks=("127.0.0.0/8",)))


@pytest.fixture
def sql_action_context(external_database):
    async def credentials(slot):
        assert slot == "password"
        return ResolvedSecret(value=external_database["password"], provider_version="1")

    async def authorize():
        return None

    def make(
        statement="SELECT customer_id, status FROM business.customers WHERE customer_id = :customerId",
        *,
        action="read",
        parameters=None,
        mappings=None,
        max_rows=100,
        max_bytes=1048576,
        timeout=1000,
    ):
        revision = ConnectionRevision(
            id=uuid4(),
            revision=1,
            name="postgres",
            connector_version_id=uuid4(),
            connector="weave-postgresql@1.0.0",
            connector_digest="a" * 64,
            adapter="weave-postgresql",
            config={
                "dialect": "postgresql",
                "host": "127.0.0.1",
                "port": external_database["kwargs"]["port"],
                "database": external_database["name"],
                "user": external_database["roles"]["read" if action == "read" else "write"],
                "role": "read" if action == "read" else "command",
                "tls": "disable",
            },
            secretRef={"password": "external-password"},
            allowed_destinations=(f"postgresql://127.0.0.1:{external_database['kwargs']['port']}",),
        )
        config = {
            "statement": statement,
            "parameters": parameters if parameters is not None else {"customerId": {"type": "string"}},
            "mappings": mappings or {},
            "maxRows": max_rows,
            "maxBytes": max_bytes,
            "statementTimeoutMs": timeout,
        }
        invocation = ConnectorInvocation(revision, config, action, {}, {}, 1048576, 1048576)
        return ActionContext(
            "op-" + uuid4().hex, datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize=authorize
        )

    return make


async def test_sql_input_is_a_value_not_an_identifier(postgres_connector, sql_action_context, external_database):
    hostile = "x'; DROP TABLE customers; --"
    result = await postgres_connector.execute({"parameters": {"customerId": hostile}}, sql_action_context())
    assert result["rows"] == []
    assert await external_database["db"].fetchval("SELECT to_regclass('business.customers')")


async def test_read_role_rejects_mutation(external_database):
    conn = await asyncpg.connect(
        **(
            external_database["kwargs"]
            | {"user": external_database["roles"]["read"], "password": external_database["password"]}
        )
    )
    try:
        with pytest.raises((asyncpg.ReadOnlySQLTransactionError, asyncpg.InsufficientPrivilegeError)):
            await conn.execute("DELETE FROM business.customers")
    finally:
        await conn.close()


async def test_bounds_and_returning_overflow_roll_back(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    for action, statement in [
        ("read", "SELECT customer_id FROM business.customers"),
        ("command", "UPDATE business.customers SET status = :status WHERE status = :old RETURNING customer_id"),
    ]:
        parameters = {} if action == "read" else {"status": {"type": "string"}, "old": {"type": "string"}}
        context = sql_action_context(statement, action=action, parameters=parameters, max_rows=1)
        with pytest.raises(ConnectorFailure, match="SQL_RESULT_LIMIT"):
            await postgres_connector.execute(
                {"parameters": {} if action == "read" else {"status": "changed", "old": "active"}}, context
            )
    assert await external_database["db"].fetchval("SELECT count(*) FROM business.customers WHERE status='active'") == 2


async def test_timeout_and_cancellation_roll_back(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    db = external_database["db"]
    transaction = db.transaction()
    await transaction.start()
    await db.execute("LOCK TABLE business.customers IN ACCESS EXCLUSIVE MODE")
    context = sql_action_context(timeout=100)
    try:
        with pytest.raises(ConnectorFailure, match="TIMEOUT"):
            await postgres_connector.execute({"parameters": {"customerId": "one"}}, context)
        task = asyncio.create_task(
            postgres_connector.execute(
                {"parameters": {"customerId": "one"}},
                replace(
                    context,
                    invocation=replace(
                        context.invocation, config=context.invocation.config | {"statementTimeoutMs": 5000}
                    ),
                ),
            )
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await transaction.rollback()
    assert await db.fetchval("SELECT count(*) FROM business.customers") == 2


async def test_precommit_revocation_rolls_back(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context(
        "UPDATE business.customers SET status = :status WHERE customer_id = :customerId RETURNING status",
        action="command",
        parameters={"status": {"type": "string"}, "customerId": {"type": "string"}},
    )
    checks = 0

    async def authorize():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise PermissionError("revoked")

    with pytest.raises(ConnectorFailure, match="SQL_AUTHORITY"):
        await postgres_connector.execute(
            {"parameters": {"status": "revoked", "customerId": "one"}}, replace(context, authorize=authorize)
        )
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "active"
    )


async def test_duplicate_command_has_one_external_effect(postgres_connector, sql_action_context, external_database):
    context = sql_action_context(
        "INSERT INTO business.customers (customer_id, status) VALUES (:customerId, :status) RETURNING customer_id",
        action="idempotent-command",
        parameters={"customerId": {"type": "string"}, "status": {"type": "string"}},
    )
    input = {"parameters": {"customerId": "new", "status": "active"}}
    first, second = await asyncio.gather(
        postgres_connector.execute(input, context), postgres_connector.execute(input, context)
    )
    assert first == second == {"rowCount": 1, "rows": [{"customer_id": "new"}]}
    assert await external_database["db"].fetchval("SELECT count(*) FROM weave_connector.operations") == 1


@pytest.mark.parametrize(
    "columns,mappings,expected",
    [
        ("amount", {"amount": "decimal"}, "1234567890.123456789"),
        ("created", {"created": "date"}, "2026-09-30"),
        ("data", {"data": "base64"}, "AP8="),
    ],
)
async def test_explicit_non_json_mapping(
    postgres_connector, sql_action_context, external_database, columns, mappings, expected
):
    await external_database["db"].execute(
        "UPDATE business.customers SET "
        "amount=1234567890.123456789,created='2026-09-30',data='\\x00ff' WHERE customer_id='one'"
    )
    context = sql_action_context(
        f"SELECT {columns} FROM business.customers WHERE customer_id = :customerId", mappings=mappings
    )
    assert (await postgres_connector.execute({"parameters": {"customerId": "one"}}, context))["rows"] == [
        {columns: expected}
    ]


async def test_mapping_byte_bounds_and_redacted_errors(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    db = external_database["db"]
    await db.execute("UPDATE business.customers SET amount=3.14,status=$1 WHERE customer_id='one'", '"\\\n' * 100000)
    with pytest.raises(ConnectorFailure, match="SQL_MAPPING"):
        await postgres_connector.execute(
            {"parameters": {"customerId": "one"}},
            sql_action_context("SELECT amount FROM business.customers WHERE customer_id = :customerId"),
        )
    with pytest.raises(ConnectorFailure, match="SQL_RESULT_LIMIT"):
        await postgres_connector.execute({"parameters": {"customerId": "one"}}, sql_action_context(max_bytes=100))

    async def bad_password(slot):
        return ResolvedSecret(value="credential-canary-not-a-password")

    with pytest.raises(ConnectorFailure) as error:
        await postgres_connector.execute(
            {"parameters": {"customerId": "one"}}, replace(sql_action_context(), credentials=bad_password)
        )
    assert error.value.args == ("SQL_FAILED",)
    assert "credential-canary" not in str(error.value)
    assert external_database["name"] not in str(error.value)


async def test_role_metadata_and_missing_authority_fail_closed(
    postgres_connector, sql_action_context, external_database
):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context()
    with pytest.raises(ConnectorFailure, match="SQL_AUTHORITY"):
        await postgres_connector.execute({"parameters": {"customerId": "one"}}, replace(context, authorize=None))
    write_role = context.invocation.connection.model_copy(
        update={"config": context.invocation.connection.config | {"user": external_database["roles"]["write"]}}
    )
    with pytest.raises(ConnectorFailure, match="SQL_ROLE"):
        await postgres_connector.execute(
            {"parameters": {"customerId": "one"}},
            replace(context, invocation=replace(context.invocation, connection=write_role)),
        )
    await external_database["db"].execute("CREATE VIEW business.unsafe AS SELECT customer_id FROM business.customers")
    await external_database["db"].execute(f"GRANT SELECT ON business.unsafe TO {external_database['roles']['read']}")
    with pytest.raises(ConnectorFailure, match="SQL_RELATION"):
        await postgres_connector.execute(
            {"parameters": {}}, sql_action_context("SELECT customer_id FROM business.unsafe", parameters={})
        )

    await external_database["db"].execute("ALTER TABLE business.customers ENABLE ROW LEVEL SECURITY")
    with pytest.raises(ConnectorFailure, match="SQL_RELATION"):
        await postgres_connector.execute({"parameters": {"customerId": "one"}}, context)


async def test_idempotency_identity_conflict_and_command_row_count(
    postgres_connector, sql_action_context, external_database
):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context(
        "UPDATE business.customers SET status = :status WHERE customer_id = :customerId",
        action="idempotent-command",
        parameters={"status": {"type": "string"}, "customerId": {"type": "string"}},
    )
    assert await postgres_connector.execute({"parameters": {"status": "updated", "customerId": "one"}}, context) == {
        "rowCount": 1,
        "rows": [],
    }
    with pytest.raises(ConnectorFailure, match="SQL_IDEMPOTENCY_CONFLICT"):
        await postgres_connector.execute({"parameters": {"status": "different", "customerId": "one"}}, context)
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "updated"
    )


@asynccontextmanager
async def commit_ack_loss_proxy(*, backend_port=55433, hold=False):
    """Forward actual PG protocol, cut only after backend COMMIT command completion."""
    evidence = {"commit_ack_dropped": 0, "committed": asyncio.Event()}
    handlers = set()

    async def handle(client_reader, client_writer):
        handlers.add(asyncio.current_task())
        backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", backend_port)
        committing = False

        async def upstream():
            nonlocal committing
            length = await client_reader.readexactly(4)
            startup = await client_reader.readexactly(struct.unpack("!I", length)[0] - 4)
            backend_writer.write(length + startup)
            await backend_writer.drain()
            while True:
                kind = await client_reader.readexactly(1)
                length = await client_reader.readexactly(4)
                data = await client_reader.readexactly(struct.unpack("!I", length)[0] - 4)
                if kind == b"Q" and data == b"COMMIT;\x00":
                    committing = True
                backend_writer.write(kind + length + data)
                await backend_writer.drain()

        async def downstream():
            while True:
                kind = await backend_reader.readexactly(1)
                length = await backend_reader.readexactly(4)
                data = await backend_reader.readexactly(struct.unpack("!I", length)[0] - 4)
                if committing and kind == b"C" and data == b"COMMIT\x00" and evidence["commit_ack_dropped"] == 0:
                    evidence["commit_ack_dropped"] += 1
                    evidence["committed"].set()
                    if hold:
                        await asyncio.Event().wait()
                    return
                client_writer.write(kind + length + data)
                await client_writer.drain()

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    task.exception()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            client_writer.close()
            backend_writer.close()
            await asyncio.gather(client_writer.wait_closed(), backend_writer.wait_closed(), return_exceptions=True)
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], evidence
    finally:
        server.close()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()


async def test_real_network_loss_after_commit_is_unknown(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context(
        "UPDATE business.customers SET status = :status WHERE customer_id = :customerId",
        action="command",
        parameters={"status": {"type": "string"}, "customerId": {"type": "string"}},
    )
    async with commit_ack_loss_proxy(backend_port=external_database["kwargs"]["port"]) as (port, evidence):
        revision = context.invocation.connection.model_copy(
            update={
                "config": context.invocation.connection.config | {"port": port},
                "allowed_destinations": (f"postgresql://127.0.0.1:{port}",),
            }
        )
        with pytest.raises(ConnectorFailure) as error:
            await postgres_connector.execute(
                {"parameters": {"status": "committed", "customerId": "one"}},
                replace(context, invocation=replace(context.invocation, connection=revision)),
            )
        assert (error.value.code, error.value.outcome) == ("SQL_COMMIT_UNKNOWN", "unknown")
        assert evidence["commit_ack_dropped"] == 1
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "committed"
    )


async def test_cancel_after_write_rolls_back_and_releases_capacity(sql_action_context, external_database):
    from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy

    connector = PostgresConnector(
        PostgresPolicy(private_networks=("127.0.0.0/8",), plaintext_networks=("127.0.0.0/8",), max_connections=1)
    )
    context = sql_action_context(
        "UPDATE business.customers SET status = :status WHERE customer_id = :customerId RETURNING status",
        action="command",
        parameters={"customerId": {"type": "string"}, "status": {"type": "string"}},
    )
    ready, forever = asyncio.Event(), asyncio.Event()
    count = 0

    async def authorize():
        nonlocal count
        count += 1
        if count == 3:
            ready.set()
            await forever.wait()

    task = asyncio.create_task(
        connector.execute(
            {"parameters": {"customerId": "one", "status": "cancelled"}}, replace(context, authorize=authorize)
        )
    )
    await asyncio.wait_for(ready.wait(), 3)
    from firefly_weave.contracts.connectors import ConnectorFailure

    with pytest.raises(ConnectorFailure, match="TIMEOUT"):
        await connector.execute(
            {"parameters": {"customerId": "one"}},
            replace(sql_action_context(), attempt_deadline=datetime.now(UTC) + timedelta(seconds=0.1)),
        )
    assert (
        await external_database["db"].fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND "
            "application_name='firefly-weave-sql'"
        )
        == 1
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "active"
    )
    assert (await connector.execute({"parameters": {"customerId": "one"}}, sql_action_context()))["rowCount"] == 1


async def test_socket_revoked_mid_attempt_never_retries(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        if calls == 2:
            killed = await external_database["db"].fetchval(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
                "datname=current_database() AND application_name='firefly-weave-sql'"
            )
            assert killed
            await asyncio.sleep(0.05)

    with pytest.raises(ConnectorFailure) as error:
        await postgres_connector.execute(
            {"parameters": {"customerId": "one"}}, replace(sql_action_context(), authorize=authorize)
        )
    assert error.value.outcome == "failed"
    assert calls == 2
    assert await external_database["db"].fetchval("SELECT count(*) FROM business.customers") == 2


async def test_commands_reject_implicit_default_effects(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    db = external_database["db"]
    role = external_database["roles"]["write"]
    await db.execute("CREATE TABLE business.with_default(customer_id text, status text DEFAULT 'implicit')")
    await db.execute(f"GRANT SELECT,INSERT ON business.with_default TO {role}")
    context = sql_action_context(
        "INSERT INTO business.with_default (customer_id) VALUES (:customerId)", action="command"
    )
    with pytest.raises(ConnectorFailure, match="SQL_RELATION"):
        await postgres_connector.execute({"parameters": {"customerId": "one"}}, context)
    assert await db.fetchval("SELECT count(*) FROM business.with_default") == 0


async def test_cancellation_during_real_commit_is_unknown(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context(
        "UPDATE business.customers SET status = :status WHERE customer_id = :customerId",
        action="command",
        parameters={"status": {"type": "string"}, "customerId": {"type": "string"}},
    )
    async with commit_ack_loss_proxy(backend_port=external_database["kwargs"]["port"], hold=True) as (port, evidence):
        revision = context.invocation.connection.model_copy(
            update={
                "config": context.invocation.connection.config | {"port": port},
                "allowed_destinations": (f"postgresql://127.0.0.1:{port}",),
            }
        )
        task = asyncio.create_task(
            postgres_connector.execute(
                {"parameters": {"status": "committed", "customerId": "one"}},
                replace(context, invocation=replace(context.invocation, connection=revision)),
            )
        )
        await asyncio.wait_for(evidence["committed"].wait(), 3)
        task.cancel()
        with pytest.raises(ConnectorFailure) as error:
            await asyncio.wait_for(task, 5)
        assert (error.value.code, error.value.outcome) == ("SQL_COMMIT_UNKNOWN", "unknown")
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "committed"
    )


async def test_idempotent_commit_loss_recovers_saved_result(postgres_connector, sql_action_context, external_database):
    from firefly_weave.contracts.connectors import ConnectorFailure

    context = sql_action_context(
        "INSERT INTO business.customers (customer_id, status, amount, created, data) "
        "VALUES (:id, :status, :amount, :created, :data) RETURNING customer_id",
        action="idempotent-command",
        parameters={
            "id": {"type": "string"},
            "status": {"type": "string"},
            "amount": {"type": "null"},
            "created": {"type": "null"},
            "data": {"type": "null"},
        },
    )
    input = {"parameters": {"id": "recover", "status": "committed", "amount": None, "created": None, "data": None}}
    async with commit_ack_loss_proxy(backend_port=external_database["kwargs"]["port"]) as (port, evidence):
        revision = context.invocation.connection.model_copy(
            update={
                "config": context.invocation.connection.config | {"port": port},
                "allowed_destinations": (f"postgresql://127.0.0.1:{port}",),
            }
        )
        context = replace(context, invocation=replace(context.invocation, connection=revision))
        with pytest.raises(ConnectorFailure, match="SQL_COMMIT_UNKNOWN"):
            await postgres_connector.execute(input, context)
        assert await postgres_connector.execute(input, context) == {"rowCount": 1, "rows": [{"customer_id": "recover"}]}
        assert evidence["commit_ack_dropped"] == 1
    assert (
        await external_database["db"].fetchval("SELECT count(*) FROM business.customers WHERE customer_id='recover'")
        == 1
    )


@pytest.mark.parametrize("generation,identity_kind", [("ALWAYS", "a"), ("BY DEFAULT", "d")])
async def test_insert_identity_is_rejected_without_sequence_advance(
    postgres_connector, sql_action_context, external_database, generation, identity_kind
):
    from firefly_weave.contracts.connectors import ConnectorFailure

    db = external_database["db"]
    role = external_database["roles"]["write"]
    await db.execute(f"CREATE TABLE business.identity_orders(id bigint GENERATED {generation} AS IDENTITY, label text)")
    await db.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON business.identity_orders TO {role}")
    await db.execute(f"GRANT SELECT ON business.identity_orders TO {external_database['roles']['read']}")
    await db.execute(f"GRANT USAGE ON SEQUENCE business.identity_orders_id_seq TO {role}")
    metadata = await db.fetchrow(
        "SELECT attidentity::text AS identity,atthasdef FROM pg_catalog.pg_attribute "
        "WHERE attrelid='business.identity_orders'::regclass AND attname='id'"
    )
    assert dict(metadata) == {"identity": identity_kind, "atthasdef": False}
    before = dict(await db.fetchrow("SELECT last_value,is_called FROM business.identity_orders_id_seq"))
    checks = 0

    async def authorize():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise PermissionError("Precommit revocation must not leave implicit sequence effects")

    context = sql_action_context(
        "INSERT INTO business.identity_orders (label) VALUES (:label) RETURNING id",
        action="command",
        parameters={"label": {"type": "string"}},
    )
    with pytest.raises(ConnectorFailure) as error:
        await postgres_connector.execute({"parameters": {"label": "owned"}}, replace(context, authorize=authorize))
    after = dict(await db.fetchrow("SELECT last_value,is_called FROM business.identity_orders_id_seq"))
    assert after == before, "An omitted identity advanced its sequence despite transaction rollback"
    assert (error.value.code, error.value.outcome) == ("SQL_RELATION", "failed")
    assert checks == 1, "Identity targets must be rejected before authorizing the business statement"
    assert await db.fetchval("SELECT count(*) FROM business.identity_orders") == 0

    await db.execute("INSERT INTO business.identity_orders(id,label) OVERRIDING SYSTEM VALUE VALUES(7,'seed')")
    read = sql_action_context(
        "SELECT id, label FROM business.identity_orders WHERE id = :id", parameters={"id": {"type": "integer"}}
    )
    assert await postgres_connector.execute({"parameters": {"id": 7}}, read) == {
        "rowCount": 1,
        "rows": [{"id": 7, "label": "seed"}],
    }
    update = sql_action_context(
        "UPDATE business.identity_orders SET label = :label WHERE id = :id RETURNING id",
        action="command",
        parameters={"label": {"type": "string"}, "id": {"type": "integer"}},
    )
    assert await postgres_connector.execute({"parameters": {"id": 7, "label": "updated"}}, update) == {
        "rowCount": 1,
        "rows": [{"id": 7}],
    }
    delete = sql_action_context(
        "DELETE FROM business.identity_orders WHERE id = :id", action="command", parameters={"id": {"type": "integer"}}
    )
    assert await postgres_connector.execute({"parameters": {"id": 7}}, delete) == {"rowCount": 1, "rows": []}
    assert dict(await db.fetchrow("SELECT last_value,is_called FROM business.identity_orders_id_seq")) == before
