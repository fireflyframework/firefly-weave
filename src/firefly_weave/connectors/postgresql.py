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

"""PostgreSQL driver for the reviewed SQL family; one external transaction per invocation."""

import asyncio
import hashlib
import ipaddress
import json
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import asyncpg  # type: ignore[import-untyped]
from pydantic import Field
from pyfly.container import service

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.egress import EgressPolicy
from firefly_weave.connectors.sql import SqlConfig, Statement, encode_result, mapped_value, parse_statement
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionRequest,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonObject, JsonValue


@dataclass(frozen=True)
class PostgresPolicy:
    private_networks: tuple[str, ...] = ()
    plaintext_networks: tuple[str, ...] = ()
    ca_file: str | None = None
    max_connections: int = 8
    cleanup_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_connections <= 64 or not 0 < self.cleanup_seconds <= 5:
            raise ValueError("Invalid PostgreSQL resource limit")
        for network in (*self.private_networks, *self.plaintext_networks):
            ipaddress.ip_network(network)


class PostgresConnectionConfig(ContractModel):
    dialect: Literal["postgresql"]
    host: str
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=63, pattern=r"^[a-zA-Z_][a-zA-Z_0-9]*$")
    user: str = Field(min_length=1, max_length=63, pattern=r"^[a-zA-Z_][a-zA-Z_0-9]*$")
    role: Literal["read", "command"]
    tls: Literal["verify-full", "disable"] = "verify-full"


def destination(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "postgresql"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or "\\" in value
        or any(ord(c) <= 32 for c in value)
    ):
        raise ValueError("Invalid PostgreSQL destination")
    address = ipaddress.ip_address(parsed.hostname or "")
    if address.version == 6 and getattr(address, "scope_id", None):
        raise ValueError("Scoped addresses are unavailable")
    if not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid PostgreSQL port")
    return str(address), parsed.port


def validate_postgres_connection(request: ConnectionRequest) -> None:
    from firefly_weave.connections.models import unavailable

    try:
        config = PostgresConnectionConfig.model_validate(request.config)
        ip = str(ipaddress.ip_address(config.host))
        if ip != config.host or (ip, config.port) not in {destination(v) for v in request.allowed_destinations}:
            raise ValueError
        if set(request.secret_refs) != {"password"}:
            raise ValueError
    except (ValueError, KeyError):
        raise unavailable() from None


async def _authorize(context: ActionContext) -> None:
    try:
        if context.authorize is None:
            raise ValueError
        await context.authorize()
    except Exception:
        raise ConnectorFailure("SQL_AUTHORITY", "failed") from None


# Only built-in scalar types have reviewed decoding, comparison and output semantics.
_TYPES = {
    "bool",
    "int2",
    "int4",
    "int8",
    "text",
    "varchar",
    "bpchar",
    "float4",
    "float8",
    "numeric",
    "date",
    "timestamp",
    "timestamptz",
    "time",
    "timetz",
    "bytea",
    "uuid",
}


@service
class PostgresConnector:
    def __init__(self, policy: PostgresPolicy) -> None:
        self.policy = policy
        self._capacity = asyncio.Semaphore(policy.max_connections)

    async def _connect(self, revision: ConnectionRequest, password: str) -> Any:
        validate_postgres_connection(revision)
        config = PostgresConnectionConfig.model_validate(revision.config)
        host = f"[{config.host}]" if ":" in config.host else config.host
        # Reuse address/CIDR rejection, while keeping protocol origins explicitly separate.
        url = f"https://{host}:{config.port}"
        EgressPolicy((url,), self.policy.private_networks).validate(url, (config.host,))
        if config.tls == "disable":
            if not any(
                ipaddress.ip_address(config.host) in ipaddress.ip_network(n) for n in self.policy.plaintext_networks
            ):
                raise ConnectorFailure("SQL_TLS", "not_started")
            tls: ssl.SSLContext | bool = False
        else:
            tls = ssl.create_default_context(cafile=self.policy.ca_file)
        if not password or len(password) > 8192:
            raise ConnectorFailure("SQL_AUTH", "not_started")
        return await asyncpg.connect(
            host=config.host,
            port=config.port,
            database=config.database,
            user=config.user,
            password=password,
            ssl=tls,
            timeout=5,
            passfile="/dev/null",
            command_timeout=30,
            server_settings={"search_path": "pg_catalog", "application_name": "firefly-weave-sql"},
        )

    async def _relation(self, connection: Any, plan: Statement, readonly: bool) -> None:
        # Keep ordinary schema changes out of the check/use window. Identifiers originate
        # exclusively from the consumed grammar, never invocation values.
        mode = "ACCESS SHARE" if readonly else "ROW EXCLUSIVE"
        await connection.execute(f'LOCK TABLE "{plan.schema}"."{plan.table}" IN {mode} MODE')
        role = await connection.fetchrow(
            "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM "
            "pg_catalog.pg_roles WHERE rolname=current_user"
        )
        if role is None or any(role.values()):
            raise ConnectorFailure("SQL_ROLE", "failed")
        relation = await connection.fetchrow(
            "SELECT c.oid,c.relkind::text AS relkind,c.relrowsecurity,c.relhasrules,c.relhassubclass,c.relowner "
            "= (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) AS owned "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=$1 AND c.relname=$2",
            plan.schema,
            plan.table,
        )
        if (
            relation is None
            or relation["relkind"] != "r"
            or relation["relrowsecurity"]
            or relation["relhasrules"]
            or relation["relhassubclass"]
            or relation["owned"]
        ):
            raise ConnectorFailure("SQL_RELATION", "failed")
        columns = await connection.fetch(
            "SELECT a.attname,t.typname,n.nspname,a.atthasdef,a.attidentity::text AS attidentity, "
            "a.attgenerated::text AS attgenerated "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_type t ON t.oid=a.atttypid JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
            "WHERE a.attrelid=$1 AND a.attnum>0 AND NOT a.attisdropped",
            relation["oid"],
        )
        known = {r["attname"]: r for r in columns}
        if any(c not in known for c in plan.columns) or any(
            c["typname"] not in _TYPES or c["nspname"] != "pg_catalog" or c["attgenerated"] for c in columns
        ):
            raise ConnectorFailure("SQL_TYPE", "failed")
        if plan.operation == "INSERT" and any(c["atthasdef"] or c["attidentity"] for c in columns):
            raise ConnectorFailure("SQL_RELATION", "failed")
        if readonly:
            privileges = await connection.fetchval(
                "SELECT has_table_privilege(current_user,$1::oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
                relation["oid"],
            )
            # Column grants are also write authority, even if no table-level grant exists.
            column_write = await connection.fetchval(
                "SELECT has_any_column_privilege(current_user,$1::oid,'INSERT,UPDATE,REFERENCES')", relation["oid"]
            )
            default_readonly = await connection.fetchval("SHOW default_transaction_read_only")
            if privileges or column_write or default_readonly != "on":
                raise ConnectorFailure("SQL_ROLE", "failed")
        else:
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_trigger WHERE tgrelid=$1 AND NOT tgisinternal) "
                "OR EXISTS(SELECT 1 FROM pg_catalog.pg_constraint WHERE (conrelid=$1 OR confrelid=$1) "
                "AND contype NOT IN ('p','u','n')) "
                "OR EXISTS(SELECT 1 FROM pg_catalog.pg_index WHERE indrelid=$1 "
                "AND (indexprs IS NOT NULL OR indpred IS NOT NULL))",
                relation["oid"],
            ):
                raise ConnectorFailure("SQL_RELATION", "failed")

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        connection = transaction = None
        state = "not_started"
        invocation = context.invocation
        try:
            config = SqlConfig.model_validate(invocation.config)
            plan = parse_statement(config.statement)
            bound = PostgresConnectionConfig.model_validate(invocation.connection.config)
            readonly = invocation.action == "read"
            if (
                invocation.connection.adapter != "weave-postgresql"
                or invocation.action not in {"read", "command", "idempotent-command"}
                or readonly != (plan.operation == "SELECT")
                or bound.role != ("read" if readonly else "command")
                or set(config.parameters) != set(plan.parameters)
            ):
                raise ConnectorFailure("SQL_CONFIG", "not_started")
            parameters = input.get("parameters")
            if (
                set(input) != {"parameters"}
                or not isinstance(parameters, dict)
                or set(parameters) != set(plan.parameters)
                or len(encode_result(input)) > min(1048576, invocation.max_request_bytes)
                or validate_payload(invocation.input_schema, input, invocation.schema_bundle)
                or any(
                    validate_payload(schema, parameters[name], invocation.schema_bundle)
                    for name, schema in config.parameters.items()
                )
            ):
                raise ConnectorFailure("SQL_INPUT", "not_started")
            values = [parameters[name] for name in plan.parameters]
        except ConnectorFailure:
            raise
        except Exception:
            raise ConnectorFailure("SQL_CONFIG", "not_started") from None
        budget = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
        if budget <= 0:
            raise ConnectorFailure("TIMEOUT", "not_started")
        acquired = False
        try:
            async with asyncio.timeout(budget):
                await self._capacity.acquire()
                acquired = True
                await _authorize(context)
                secret = await context.credentials("password")
                connection = await self._connect(invocation.connection, secret.value)
                del secret
                transaction = connection.transaction(readonly=readonly)
                await transaction.start()
                state = "active"
                await connection.execute(
                    "SELECT set_config('statement_timeout',$1,true)", str(config.statement_timeout_ms)
                )
                await self._relation(connection, plan, readonly)
                await _authorize(context)
                result = None
                scope = str(invocation.connection.id)
                fingerprint = hashlib.sha256(
                    encode_result(
                        {
                            "config": invocation.config,
                            "input": input,
                            "action": invocation.target.action_digest if invocation.target else invocation.action,
                        }
                    )
                ).hexdigest()
                if invocation.action == "idempotent-command":
                    # Unique insert waits for a concurrent transaction; no business statement is replayed.
                    inserted = await connection.fetchval(
                        "INSERT INTO weave_connector.operations(scope,operation_key,request_hash) VALUES($1,$2,$3) "
                        "ON CONFLICT(scope,operation_key) DO NOTHING RETURNING request_hash",
                        scope,
                        context.operation_key,
                        fingerprint,
                    )
                    if inserted is None:
                        prior = await connection.fetchrow(
                            "SELECT request_hash,result FROM weave_connector.operations WHERE "
                            "scope=$1 AND operation_key=$2",
                            scope,
                            context.operation_key,
                        )
                        if prior is None or prior["request_hash"] != fingerprint or prior["result"] is None:
                            raise ConnectorFailure("SQL_IDEMPOTENCY_CONFLICT", "failed")
                        result = json.loads(prior["result"])
                if result is None:
                    statement = await connection.prepare(plan.sql)
                    attributes = statement.get_attributes()
                    labels = [a.name for a in attributes]
                    if len(set(labels)) != len(labels) or set(config.mappings) - set(labels):
                        raise ConnectorFailure("SQL_MAPPING", "failed")
                    rows = []
                    if attributes:
                        cursor = await statement.cursor(*values)
                        while True:
                            batch = await cursor.fetch(1)
                            if not batch:
                                break
                            row = {name: mapped_value(batch[0][name], config.mappings.get(name)) for name in labels}
                            rows.append(row)
                            if len(rows) > config.max_rows or len(
                                encode_result({"rowCount": len(rows), "rows": rows})
                            ) > min(config.max_bytes, invocation.max_response_bytes):
                                raise ConnectorFailure("SQL_RESULT_LIMIT", "failed")
                        count = len(rows)
                    else:
                        status = await connection.execute(plan.sql, *values)
                        prefix = "INSERT 0 " if plan.operation == "INSERT" else plan.operation + " "
                        if not status.startswith(prefix) or not status[len(prefix) :].isdigit():
                            raise ConnectorFailure("SQL_STATUS", "failed")
                        count = int(status[len(prefix) :])
                    result = {"rowCount": count, "rows": rows}
                    if invocation.action == "idempotent-command":
                        await connection.execute(
                            "UPDATE weave_connector.operations SET result=$3::jsonb WHERE "
                            "scope=$1 AND operation_key=$2",
                            scope,
                            context.operation_key,
                            encode_result(result).decode(),
                        )
                if len(encode_result(result)) > min(
                    config.max_bytes, invocation.max_response_bytes
                ) or validate_payload(invocation.output_schema, result, invocation.schema_bundle):
                    raise ConnectorFailure("SQL_OUTPUT", "failed")
                await _authorize(context)
                state = "committing"
                await transaction.commit()
                state = "committed"
                return cast(JsonValue, result)
        except asyncio.CancelledError:
            if state == "committing":
                raise ConnectorFailure("SQL_COMMIT_UNKNOWN", "unknown") from None
            raise
        except Exception as error:
            if state == "committing":
                raise ConnectorFailure("SQL_COMMIT_UNKNOWN", "unknown") from None
            if isinstance(error, ConnectorFailure):
                raise
            if isinstance(error, (TimeoutError, asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError)):
                raise ConnectorFailure("TIMEOUT", "not_started" if state == "not_started" else "failed") from None
            if isinstance(error, (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError)):
                raise ConnectorFailure("SQL_ROLE", "failed") from None
            if isinstance(error, (TypeError, ValueError)):
                raise ConnectorFailure("SQL_MAPPING", "failed") from None
            raise ConnectorFailure("SQL_FAILED", "not_started" if state == "not_started" else "failed") from None
        finally:
            try:
                if connection is not None:
                    # An unacknowledged COMMIT is never relabeled by cleanup. Termination closes
                    # any uncommitted transaction; cleanup failure does not replace the primary error.
                    async def cleanup() -> None:
                        try:
                            async with asyncio.timeout(self.policy.cleanup_seconds):
                                if state == "active" and transaction is not None:
                                    await transaction.rollback()
                                await connection.close(timeout=self.policy.cleanup_seconds)
                        except BaseException:
                            connection.terminate()

                    closing = asyncio.create_task(cleanup())
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        await closing
                        raise
            finally:
                if acquired:
                    self._capacity.release()

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        database = None
        try:
            async with asyncio.timeout(5):
                async with self._capacity:
                    secret = connection.credentials("password")
                    database = await self._connect(connection.revision, secret.value)
                    await database.fetchval("SELECT 1")
            return ConnectionTestResult(ok=True)
        except Exception:
            return ConnectionTestResult(ok=False, code="failed")
        finally:
            if database is not None:
                database.terminate()
