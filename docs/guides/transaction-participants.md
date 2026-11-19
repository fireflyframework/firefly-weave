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

# Make a system safe to undo

This guide is for developers whose system a workflow
[transaction](transactions.md) calls: a worker you write with the SDK, a
PostgreSQL database behind the built-in connector, or any other service. It
explains what Weave sends your system, what your system must do with it, how to
do that with the SDK's fence or the PostgreSQL connector, and how to check it
before production with `weave transactions conformance`.

## What Weave sends

Every request of a step inside a transaction carries two keys:

- The **operation key**, `"{run ID}:{instance key}"`, is the same every time
  Weave sends that request again. A step's own request uses the step's key
  (`R:hold`); its confirm, cancel, and undo use their own (`R:hold#confirm`,
  `R:hold#cancel`, `R:add#undo`).
- The lease's `transaction.branch_key` (a connector gets
  `ActionContext.transaction.branch_key`) is the operation key of the step
  the request belongs to, so all requests of one step name the same branch.

`transaction.phase` says which request it is: `try` for a step that reserves,
`forward` for a step an action undoes, then `confirm`, `cancel`, or
`compensate`. A cancel or undo also says what Weave knows about the step:
`forward_outcome` is `done`, or `unknown` when the step may or may not have
applied.

Weave delivers each request at least once. It sends one direction per step (a
confirm or a cancel, never both), and it waits for every step still running to
settle before it undoes anything. The network can still reorder requests: a
retry of the step can arrive after its cancel, and a cancel can arrive for a
step your system never received.

## What your system must do

| Situation | What your system must do |
| --- | --- |
| The same request arrives twice | Change nothing the second time, and answer as the first time |
| A cancel or undo arrives for a step that never arrived | Succeed, and remember the step's key so the step is refused if it arrives later |
| The step arrives after its cancel or undo | Refuse it and change nothing |
| A step hangs past its time limit | Nothing; Weave treats its outcome as unknown and undoes it, so the undo must cope with a step that did or didn't apply |
| A confirm arrives after the reservation expired | Fail with a code the action lists in `definiteFailures`, or report `effect: none`; the transaction is then in doubt for a person |

The record of a cancelled step that never arrived is a **tombstone**. It is
what refuses the late step.

## With an SDK worker

The fence, `firefly_weave.sdk.participants.Fence`, records each request in your
own PostgreSQL database, in the same transaction as your change. It speaks
asyncpg's connection API and adds no dependency.

1. Have the database owner create the fence's table. `Fence().ddl()` returns
   this text; `Fence("app.fence")` names another `schema.table`:

    ```sql
    CREATE SCHEMA IF NOT EXISTS "weave_fence";
    CREATE TABLE IF NOT EXISTS "weave_fence"."operations" (
      operation_key text PRIMARY KEY,
      branch_key text NOT NULL,
      phase text NOT NULL CHECK (phase IN ('try', 'forward', 'confirm', 'cancel', 'compensate')),
      status text NOT NULL CHECK (status IN ('tried', 'confirmed', 'cancelled', 'tombstone')),
      result jsonb
    );
    ```

    Grant the worker's login `USAGE` on the schema and `SELECT`, `INSERT`, and
    `UPDATE` on the table.

2. Open one database transaction per request, and make the change and the
   fence's row in it:

    ```python
    @asynccontextmanager
    async def transaction() -> AsyncIterator[asyncpg.Connection]:
        """One database transaction for one request: the change and the fence's row commit together."""
        connection = await asyncpg.connect(os.environ["PARTICIPANT_DATABASE_URL"])
        try:
            async with connection.transaction():
                yield connection
        finally:
            await connection.close()
    ```

3. In each handler, call `begin` before the change and `finish` with the
   answer:

    ```python
    async def hold(lease: TaskLease) -> JsonValue:
        async with transaction() as connection:
            entry = await fence.begin(connection, lease.operation_key, lease.transaction)
            if entry.replay:
                return entry.result
            await connection.execute(
                "INSERT INTO holds (hold_id, reference, quantity, status) VALUES ($1, $2, $3, 'held')",
                lease.operation_key,
                lease.input["reference"],
                lease.input["quantity"],
            )
            return await fence.finish(connection, entry, {"holdId": lease.operation_key})
    ```

    ```python
    async def release_hold(lease: TaskLease) -> JsonValue:
        async with transaction() as connection:
            entry = await fence.begin(connection, lease.operation_key, lease.transaction)
            if entry.replay:
                return entry.result
            # Without the hold there is nothing to release; the fence has recorded that it must never be made.
            if entry.applied:
                await connection.execute("DELETE FROM holds WHERE hold_id = $1", lease.input["holdOperation"])
            return await fence.finish(connection, entry, {})
    ```

    The whole worker, with a confirm, an addition and its removal, is in
    [`examples/transaction_participant.py`](https://github.com/fireflyframework/firefly-weave/blob/main/examples/transaction_participant.py).

What `begin` returns:

- `replay` is true when an earlier request with this key finished: return
  `result`, which is that request's answer, and change nothing.
- `applied`, for a confirm, cancel, or undo, says whether the step it belongs to
  was applied. A cancel or undo with `applied` false has nothing to undo; the
  fence has already left the tombstone.

`begin` refuses a request that must change nothing by raising
`ConnectorFailure(code, "failed", effect="none")`:

| Code | When |
| --- | --- |
| `TXN_ALREADY_UNDONE` | The step arrived after its cancel or undo |
| `TXN_NOT_APPLIED` | A confirm arrived for a step that was never applied |
| `TXN_OTHER_DIRECTION` | A cancel or undo arrived for a confirmed step, or a confirm for a cancelled one |

Because the refusal raises inside your transaction, your change and the fence's
row roll back together. `begin` refuses a connection that isn't inside a
transaction (`RuntimeError`), and a concurrent request with the same key waits
for the first one's transaction.

For Weave to trust `effect: none`, declare it on the worker's claims:
`WorkerTransport(..., worker=ClaimWorker(release=..., transaction_aware=True))`.
Without the declaration, a refused step counts as maybe applied and is undone
again, which the fence also answers safely. See
[Worker protocol](../reference/worker-protocol.md).

Keep the fence's rows at least as long as the transaction's undo time limit
(`undoTimeoutSeconds`) plus the time a person may take to retry a stuck undo; 30
days is a safe default. A row removed too early lets a late request apply.

## With the PostgreSQL connector

The built-in PostgreSQL connector needs nothing more than its operation table
(see [Idempotency, authority, and ambiguous outcomes](../connectors/postgresql.md#idempotency-authority-and-ambiguous-outcomes)):

- Use `idempotent-command` for the step and for its confirm, cancel, or undo,
  and keep them on the same connection slot, which is the default.
- A first cancel or undo of a step that has no row yet records the step's key
  as a tombstone in the operation table. A step that arrives later fails
  `SQL_ALREADY_UNDONE` before its statement runs.
- A `command` step never reads the operation table, so it is not protected; nor
  is a step whose cancel runs on another connection slot. The check below shows
  both.

## Other systems

For an HTTP API or any other service, give the undo the step's key and let the
service refuse a late step by it: pass `{"ref": "/participant/operationKey"}` in
the cancel's or undo's `with`, as
[`move-between-systems.workflow.yaml`](https://github.com/fireflyframework/firefly-weave/blob/main/examples/language/move-between-systems.workflow.yaml)
does. When the step itself can't send its key, use a reference both requests
carry, such as a business reference from the run's input.

## Check it

`weave transactions conformance` sends a step's own requests, built as Weave
builds them, in every order Weave can deliver them, and reports each check. It
runs on your computer against your handlers or your database, so it fits your
own CI. It applies real changes: run it against a test system, never
production.

```sh
weave transactions conformance move.workflow.yaml --step hold \
  --catalog catalog.lock.json --input input.json \
  --handlers transaction_participant:handlers --vary /moveId
```

| Flag | What it names |
| --- | --- |
| `WORKFLOW` | The workflow file in YAML or JSON |
| `--step ID` | A step inside a `transaction` whose confirm and cancel, or undo, run actions |
| `--catalog LOCK` | The catalog lock with the step's actions; `weave remote catalog --output json` writes the platform's |
| `--input FILE` | The run's input |
| `--steps FILE` | Outputs of earlier steps the step reads, by step ID: `{"lookup": {"count": 3}}` |
| `--vary POINTER` | An input string that gets `-` and 12 hexadecimal characters appended in every check, so the checks don't share business references |
| `--handlers MODULE:ATTRIBUTE` | Worker handlers by capability, as `Worker(handlers=...)` takes them, or a function (sync or async) that returns them |
| `--connection SLOT=FILE` | A connection request for a PostgreSQL step's slot, in the shape `weave connections create --request` takes |
| `--secret SLOT.NAME=VARIABLE` | Read the connection's secret `NAME` from the environment variable `VARIABLE` |
| `--private-network`, `--plaintext-network`, `--ca-file` | The PostgreSQL connector's network policy |
| `--output text\|json` | One line per check, or the report as JSON |

The checks of a step that reserves are `try-then-confirm`, `try-then-cancel`,
`cancel-without-try`, `try-after-cancel`, `try-twice`, `confirm-twice`, and
`cancel-twice`; a step an action undoes gets the same shapes with `forward` and
`compensate`. Each check starts a new run ID, so its keys never meet a real
run's. A system without tombstones reports:

```text
passed  try-then-confirm
passed  try-then-cancel
passed  cancel-without-try
FAILED  try-after-cancel: The step was applied after its cancel or undo.
passed  try-twice
passed  confirm-twice
passed  cancel-twice
6 of 7 checks passed.
```

The command exits 0 when every check passes, 1 when one fails, and 2 when it
can't start, with a `WV-CONFORMANCE-*` code: for example a step that isn't undone
by actions, a value the step reads that `--steps` doesn't give, or a handler or
connection secret that is missing. In CI, use `--output json` and the exit code.
The report and its errors never show an input, an output, or a secret.

The check judges answers only. It can't see inside your system, so a system that
answers "done" but changes data later passes; test your own rows too. It doesn't
wait for a reservation to expire, so try a confirm after expiry by hand. For
HTTP and packaged connectors, call `check_step` from
`firefly_weave.sdk.participant_conformance` with a `ConnectorParticipant` that
takes your adapter.

## See also

- [Undo steps together with a transaction](transactions.md)
- [Worker protocol: what a participant must do](../reference/worker-protocol.md)
- [Read and write PostgreSQL from a workflow](../connectors/postgresql.md)
- [CLI reference](../reference/cli.md)
