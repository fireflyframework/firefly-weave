<!--
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Read and write PostgreSQL from a workflow

Use this guide when a workflow needs to read or change rows in a business
PostgreSQL database, for example to look up a customer before an approval. The
built-in `weave-postgresql@1.0.0` connector runs one reviewed SQL statement per
step, inside its own transaction in **your** database. Weave's own runtime
database is a separate service and is never used for these calls.

- **Who it is for:** integration developers who write the reviewed statement, the
  database owner who grants access, and the operator who prepares the platform.
- **What you need:** a deployed platform with a native executor (the
  [local platform](../guides/local-platform.md) runs only the built-in HTTP
  connector), a reachable PostgreSQL server with TLS, and the rights to publish,
  create connections, and activate. [Connect the CLI to a platform](../guides/connect-to-api.md)
  first.
- **How long:** about 30 minutes for the first lookup once the database owner has
  prepared the table and login.

![External PostgreSQL transaction and operation-ledger boundary](../diagrams/integrations-sql-transaction.svg)

**How to read this diagram:** Read down from the reviewed operation and connection to the external transaction and its result. Step 4 applies only to idempotent-command; ordinary commands do not gain that protection. A lost COMMIT acknowledgment remains unknown even after cleanup.

[Open diagram at full size](../diagrams/integrations-sql-transaction.svg)

## Choose the action

The connector has three actions. Each one has its own reserved capability
`weave-connector-postgresql-<action>@1.0.0` and its own descriptor binding;
releases and credential grants must authorize that exact capability and
connection revision.

| Action | Statement | Side effect | Connection `role` |
| --- | --- | --- | --- |
| `read` | `SELECT` | `read_only` | `read`: a dedicated login with no write grants, default read-only; runs in `BEGIN READ ONLY` |
| `command` | `INSERT`, `UPDATE`, or `DELETE` | `non_idempotent` | `command`: a dedicated command login |
| `idempotent-command` | `INSERT`, `UPDATE`, or `DELETE` | `idempotency_key` | `command`, plus the [operation ledger](#idempotency-authority-and-ambiguous-outcomes) |

The connection's `role` selects the connector's access profile; it is not a
PostgreSQL role name. A `read` Action needs a `read` connection, and the command
actions need a `command` connection; any other pairing fails with `SQL_CONFIG`
before connecting.

## First lookup: from connection to rows

This walkthrough publishes a bounded, read-only customer lookup. Its success
criterion is an expected row result from the approved database; compiling the
Action alone does not query that database. The
[publication sequence](authoring.md#from-package-to-an-executable-workflow)
explains each ID you will collect, and [native worker admission](../guides/workers.md)
explains executor releases.

1. **Agree on the database facts.** Ask the database owner for a reviewed
    `business.customers` table, a dedicated read login, the server's canonical IP
    address and port, the database name, and the trusted TLS CA. Check the
    [table restrictions](#tables-the-connector-accepts) before granting access.

2. **Prepare the platform (operator).** Put the password behind an
    operator-scoped secret handle, here `customer-db-password`. Publish the
    installed Connector: `weave connector descriptor weave-postgresql --output
    json` prints the platform's installed descriptor (in a CLI from the current
    source), and its `source` field is the exact publication source for `weave
    definitions publish --collection connectors`, wrapped as `{"format": "json",
    "source": ...}`. Register the executor release
    with the descriptor's capabilities and bindings, and allow the private
    network if the address is private (see
    [Connection and destination policy](#connection-and-destination-policy)).

3. **Create the connection.** Use the returned Connector version ID in this
    complete request:

    ```json
    {
      "name": "customer-db",
      "connector_version_id": "00000000-0000-4000-8000-000000000001",
      "config": {
        "dialect": "postgresql", "host": "10.20.0.15", "port": 5432,
        "database": "customers", "user": "weave_reader", "role": "read", "tls": "verify-full"
      },
      "secretRef": {"password": "customer-db-password"},
      "allowed_destinations": ["postgresql://10.20.0.15:5432"]
    }
    ```

    ```sh
    # Create the connection revision from the reviewed request file.
    weave connections create --request customer-db.json --output json
    ```

    Expected: a connection revision whose `id` is the **connection revision ID**.
    The UUID and address above are placeholders; the certificate must cover the
    actual IP. Then grant the release the connection's credentials with `weave
    workers grant` and the capability `weave-connector-postgresql-read@1.0.0`.
    `weave connections test ID` connects with the password and runs `SELECT 1`; it
    proves reachability and authentication, not this query's privileges or a
    supported table shape.

4. **Publish the Action and a workflow.** Publish
    [`postgresql.action.yaml`](../../examples/definitions/postgresql.action.yaml)
    (`sql.lookup-customer@1.0.0`) with `weave definitions publish --collection
    actions`, wrapping the file as `{"format": "yaml", "source": ...}`. A workflow
    calls it through a connection slot; this one compiles against that Action:

    ```json
    {
      "apiVersion": "weave/v1alpha1", "kind": "Workflow",
      "metadata": {"name": "customer-lookup", "version": "1.0.0"},
      "spec": {
        "connections": {"customers": {"connector": "weave-postgresql@1.0.0"}},
        "inputSchema": {"type": "object", "properties": {"customerId": {"type": "string", "maxLength": 128}},
                        "required": ["customerId"], "additionalProperties": false},
        "outputSchema": {},
        "steps": [{"id": "lookup", "kind": "action", "uses": "sql.lookup-customer@1.0.0", "connection": "customers",
                   "with": {"object": {"parameters": {"object": {
                     "customerId": {"ref": "/input/customerId"}, "rowLimit": {"literal": 10}}}}}}],
        "output": {"ref": "/steps/lookup/output"}
      }
    }
    ```

5. **Activate and run.** Activate the workflow with `connection_revision_ids`
    mapping `customers` to the connection revision ID and `connector_release_ids`
    mapping the Connector version ID to the admitted release, then start a run
    with `{"customerId": "customer-123"}`. The step's Action input is then:

    ```json
    {"parameters":{"customerId":"customer-123","rowLimit":10}}
    ```

    Expected: a matching row produces
    `{"rowCount":1,"rows":[{"customer_id":"customer-123","status":"active"}]}`, and
    no match produces `{"rowCount":0,"rows":[]}`. Actual row values come from your
    table.

If the step fails, read the fixed SQL code in the run incident and use
[Troubleshoot](#troubleshoot). For `SQL_COMMIT_UNKNOWN`, reconcile the external
operation before retrying; the lookup is read-only, but commands have different
recovery rules below.

## How a call runs

An invocation receives its statement and parameter schema from the published,
pinned Action. Its input contains only `parameters`; values never become
identifiers. Each invocation owns one database connection and one explicit
external transaction, independent of the platform's own transactions. The native
PyFly service uses constructor injection; it keeps resource limits, never an
actor, lease, secret, or connection.

PostgreSQL is the only implemented driver. There is no portable-SQL claim and no
Oracle, MySQL, or SQL Server adapter.

## Supported grammar

The PostgreSQL profile accepts one fully consumed statement. It tokenizes at most
16 KiB and 1,024 tokens, then emits SQL from parsed operations, identifiers, and
positional value binds. Tokens are ASCII, identifiers are unquoted and folded to
lowercase, and every relation is explicitly `schema.table`.

```sql
SELECT customer_id, status FROM business.customers
WHERE customer_id = :customerId LIMIT :rowLimit
INSERT INTO business.customers (customer_id, status) VALUES (:customerId, :status)
UPDATE business.customers SET status = :status WHERE customer_id = :customerId
DELETE FROM business.customers WHERE customer_id = :customerId
```

- `SELECT` permits optional equality predicates joined by `AND` and an optional
  bound `LIMIT`.
- `UPDATE` and `DELETE` require `WHERE`. Commands permit explicit `RETURNING`
  columns.
- Lists and predicates are limited to 64 entries. Repeated named values share a
  positional bind; the Action's `parameters` schemas and input names must match
  exactly.

**Rejected syntax** includes literals, comments, semicolons, quoted identifiers,
`SELECT *`, functions, casts, CTEs, joins, subqueries, `OR`, DDL, `COPY`, `CALL`,
session or role changes, and transaction control. This intentionally small
grammar is not a general SQL parser. Unsupported queries must be redesigned or
await another reviewed profile.

## Tables the connector accepts

- Only ordinary, non-owned tables without inheritance, row-level security, or
  rules.
- Columns must use the reviewed built-in scalar types; domains, arrays, JSON,
  custom types, and generated columns are unsupported.
- Commands additionally reject user triggers, check, foreign-key, and exclusion
  constraints, and expression or partial indexes. `INSERT` rejects tables with
  defaults or identity columns (`ALWAYS` and `BY DEFAULT`). Primary and unique
  keys and `NOT NULL` are allowed.
- The connection's login may not be a superuser or have `CREATEDB`,
  `CREATEROLE`, replication, or `BYPASSRLS`. A `read` login must have no table or
  column write grants, and its default transactions must be read-only.

The connector takes a table lock before metadata checks. Operators must own and
review schema changes and maintain least privilege; this is not protection
against a malicious database administrator. Parsing alone cannot prove arbitrary
database side effects absent. Extending this profile to triggers, sequences, or
functions requires explicit treatment of effects that rollback cannot reverse.

## Connection and destination policy

The connection config has exactly `dialect`, `host`, `port`, `database`, `user`,
`role`, and `tls`. `secretRef` contains only `password`. `allowed_destinations`
contains exact `postgresql://IP:port` origins, with IPv6 brackets where needed.

**Only canonical IP literals are supported in this release.** DNS names, URLs and
DSNs, paths, sockets, host lists, and DSN query options are rejected. The driver
receives an explicit host, port, database, user, password, and TLS setting, and
`/dev/null` as its password file; external connections never fall back to the
platform database or an environment DSN.

| Operator variable | Meaning | Default |
| --- | --- | --- |
| `WEAVE_POSTGRES_CA_FILE` | CA bundle used to verify the server certificate | System trust |
| `WEAVE_POSTGRES_PRIVATE_NETWORKS` | JSON CIDR array of private ranges the connector may reach | `[]` |
| `WEAVE_POSTGRES_PLAINTEXT_NETWORKS` | JSON CIDR array where `tls: disable` is allowed, for an explicitly permitted local deployment only | `[]` |
| `WEAVE_POSTGRES_MAX_CONNECTIONS` | External invocations that may own a connection at once, per process (1–64) | `8` |

- **TLS.** `tls: verify-full` is the default; both the certificate chain and the
  IP SAN are verified. Connection authors cannot select certificates or disable
  verification.
- **Addresses.** Link-local and metadata, multicast, unspecified, and reserved
  destinations stay denied under the shared address policy. HTTP destination
  validation remains HTTP-specific and unchanged.
- **Capacity.** Semaphore waits consume the attempt deadline. Connections are
  never pooled or shared by URL, role, revision, or credential version. The
  connect timeout is 5 seconds, bounded by the remaining attempt budget. A
  separate 2-second cleanup budget attempts rollback and close, then terminates
  the socket. These are process-local limits, not cluster-wide capacity
  admission.

## Bounds and mappings

The Action config sets `statementTimeoutMs` (1–30,000, default 10,000), `maxRows`
(1–10,000, default 100), `maxBytes` (32–1,048,576, default 1 MiB), and
`mappings`. The outer attempt deadline bounds acquisition, metadata, statement,
fetch, and commit together. A transaction-local PostgreSQL statement timeout
applies to each command; zero never disables it.

The driver fetches one row at a time, checks the row cap and the exact serialized
UTF-8 JSON envelope, and validates the Action output before `COMMIT`. It fails on
overflow; `RETURNING` overflow rolls back the command. No partial success is
returned. `rowCount` means returned rows when `RETURNING` or `SELECT` produces
columns; otherwise it is the affected count from the validated command tag. It
does not count every indirect effect, and streaming does not cap affected rows or
server work.

| Database value | Required mapping | JSON value |
| --- | --- | --- |
| numeric/decimal | `decimal` | Exact finite decimal string |
| date | `date` | ISO date string |
| timestamp/timestamptz | `datetime` | ISO string; preserves available offset |
| time/timetz | `time` | ISO string |
| bytea | `base64` | Standard base64 string |
| UUID | `uuid` | UUID string |

Null stays null. Primitive text, boolean, integers, and finite floats need no
mapping. Mapping or type mismatches, non-finite values, and unsupported values
fail. Binary, temporal, and decimal parameter coercion is not implicit: task
values must match the driver-inferred bind type; this profile primarily supports
JSON scalar binds.

**Memory limit:** the driver can decode a single large field before Python can
reject it. The 1 MiB bound controls retained and accepted output, not arbitrary
peak process memory. Approved database projections, field constraints, and
process isolation are needed when database data is untrusted or unbounded.

## Idempotency, authority, and ambiguous outcomes

**The operation ledger.** `idempotent-command` needs this ledger in the external
database. Provision it with a separate schema owner; the connector never creates
schemas or tables. Grant the command login `USAGE` on the schema and `SELECT`,
`INSERT`, and `UPDATE` on this table only. Keep it an ordinary table without
triggers, custom types, defaults, or rewrite policies.

```sql
CREATE SCHEMA weave_connector;
CREATE TABLE weave_connector.operations (
  scope text NOT NULL,
  operation_key text NOT NULL,
  request_hash text NOT NULL,
  result jsonb,
  PRIMARY KEY (scope, operation_key)
);
```

- The scope is the immutable, globally unique connection revision ID, which the
  platform binds to the tenant, project, and environment.
- The fingerprint covers the pinned Action identity, the reviewed config, and the
  full input. The unique operation record, the business change, and the bounded
  result commit together.
- Concurrent duplicates wait on that unique record and recover the saved result.
  Reusing a key for a different request fails with `SQL_IDEMPOTENCY_CONFLICT`.
- A new revision is a new idempotency namespace. Do not discard or change ledger
  records while deliveries may recur. No library or application retry silently
  re-executes a business command.

**Authority checks.** Before connecting, before the business statement, and
immediately before `COMMIT`, the connector requires the active invocation's
`ActionContext.authorize` callback. Native execution rechecks the live lease, the
exact saved pins, the current principal, the release capability, and the
unrevoked connection grant without secret-provider I/O. The callback fails closed
when absent and expires when the invocation exits. A denial before `COMMIT` rolls
back. Credentials are resolved once per invocation.

There is an unavoidable cross-database check-to-`COMMIT` race: a revocation
committed after the final authorization check cannot atomically cancel the
external commit. No distributed transaction or instantaneous revocation guarantee
is claimed.

**Ambiguous commits.** Any timeout, cancellation, or transport failure after
entering `COMMIT` without an acknowledgment yields `SQL_COMMIT_UNKNOWN` with
outcome `unknown`; a later rollback or socket cleanup never proves that the
commit did not happen. Unsafe command recovery opens `WV-TASK-AMBIGUOUS`;
operators reconcile before replay. Idempotent commands can safely recover the
ledger on an authorized redelivery; this connector does not retry them
automatically. Earlier cancellation propagates after cleanup. Stable failures
carry codes only, never driver text, SQL, bind values, or credentials.

## Troubleshoot

| What you see | Why | What to do |
| --- | --- | --- |
| `SQL_CONFIG` | The Action config is invalid, the statement does not match the action, or the connection `role` does not match the action | Pair `read` with a `read` connection and commands with a `command` connection; check the statement against the grammar |
| `SQL_INPUT` | The input is not exactly `{"parameters": {...}}` with every statement parameter, or a value fails its schema | Compare the step's input with the Action's `parameters` schemas |
| `SQL_TLS` | `tls: disable` was used for an address outside `WEAVE_POSTGRES_PLAINTEXT_NETWORKS` | Use `verify-full`, or have the operator permit that local address |
| `SQL_AUTH` | The stored password is empty or too long | Store a corrected value behind the same handle |
| `SQL_ROLE` | The login has elevated attributes, a `read` login can write, or the database denied the statement | Ask the database owner to apply least-privilege grants |
| `SQL_RELATION` or `SQL_TYPE` | The table or one of its columns is outside the [accepted shapes](#tables-the-connector-accepts) | Use a reviewed table or projection that fits the profile |
| `SQL_MAPPING` | A returned column needs a mapping, or a value does not match its mapping | Add the required mapping from the table above |
| `SQL_RESULT_LIMIT` or `SQL_OUTPUT` | The rows or bytes exceed `maxRows` or `maxBytes`, or the result fails the output schema | Narrow the query, raise the limits within bounds, or fix the output schema |
| `SQL_IDEMPOTENCY_CONFLICT` | An operation key was reused for a different request | Investigate the earlier operation before sending a new one |
| `SQL_AUTHORITY` | An authority check before connecting, before the statement, or before `COMMIT` failed: the lease, pins, release capability, or connection grant is missing or changed | Check the release's `weave workers grant` for this connection revision and capability, and the executor's `worker` role |
| `TIMEOUT` | The attempt deadline, the statement timeout, or a lock wait ran out | Check `statementTimeoutMs`, the Action's timeout, and locks on the table |
| `SQL_STATUS` | The database returned an unexpected command tag; the transaction was rolled back | Check the table against the [accepted shapes](#tables-the-connector-accepts) before running the command again |
| `SQL_FAILED` | Another database or transport failure; the code is the only detail | Check the database logs and reachability; `weave connections test ID` checks login and `SELECT 1` |
| `SQL_COMMIT_UNKNOWN` | The commit acknowledgment was lost | Reconcile in the database, then resolve the incident; see [incident operations](../reference/incident-operations.md) |

## Next steps

- Put the lookup in a process with a decision or an approval:
  [Author workflows](../guides/workflow-authoring.md) and
  [Human tasks and approvals](../guides/human-tasks.md).
- Understand releases, grants, and activation pins:
  [From package to an executable workflow](authoring.md#from-package-to-an-executable-workflow).
- Call an HTTP API in the same workflow: [Call a REST API without code](http-without-code.md).
