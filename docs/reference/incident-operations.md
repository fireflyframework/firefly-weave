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

# Resolve incidents, cancel runs, and retry them

An **incident** records a condition that needs a person's decision, most often
because Weave cannot tell whether an external action completed. While an incident
is active, the run is **suspended**: normal continuation waits for the decision.
This page shows how to make that decision safely, how to cancel a run, and how to
start a new run from a finished one.

**Who it is for.** Operators who keep runs healthy. **What you need.** The CLI
connected to a platform ([connect the CLI](../guides/connect-to-api.md)); the
`operator` role in the run's
environment (it grants `incident.read`, `incident.resolve`, `run.cancel`, and
`run.retry`), plus `viewer` to read runs and their history; and the run's `id`,
from its start response or a trigger or provider receipt. Allow 15 minutes, plus
the time to check the external system.

![Read-only replay separated from reconciliation, incident resolution, and whole-run retry](../diagrams/integrations-incidents-replay.svg)

Follow the lower row left to right when you change execution: check the external
system first, then submit a decision protected by the current revision and a
stable receipt ID. The upper replay row is useful inspection, but it cannot retry
an action or clear an incident.
[Open diagram at full size](../diagrams/integrations-incidents-replay.svg)

**Three actions, three meanings:**

| Action | Changes | Use it when |
| --- | --- | --- |
| Resolve an incident | The same run continues, or stops | One step's outcome is uncertain and you can decide it |
| Cancel a run | The run stops; work already sent elsewhere may continue | The execution should not go on |
| Retry a run | A **new** run starts, linked to the finished one | A finished run must be done again; it may repeat external effects |

## Resolve an incident

Run the commands with a saved platform and workspace from `weave auth setup` (see
[how remote commands choose a platform](../guides/connect-to-api.md#how-remote-commands-choose-a-platform)).
Saved platforms are new in 0.1.0a7; with an alpha6 or earlier CLI, these
commands use [explicit mode](../guides/connect-to-api.md#scripts-and-ci-explicit-mode).

1. **Spot the blocked run.** In Studio, **Runs** shows the status "On hold" for
   it, and its detail shows "This run stopped at STEP." with the incident's
   code, where STEP is the step's ID. Studio shows this state but has no
   controls to resolve an incident, cancel a run, or retry it; use the CLI
   commands below or the API. From the CLI, list the run's incidents:

    ```sh
    # List the incidents of one run, active and closed.
    weave runs incidents run-list RUN_ID --output json
    ```

    Expected: a page whose `items` include the active incident with `id`,
    `revision`, `node_id`, `generation`, `code`, and `"status": "active"`. Record
    them. `weave runs incidents list` shows every incident in the environment.

    The `code` says what happened:

    | `code` | What happened | Decisions that fit |
    | --- | --- | --- |
    | `WV-TASK-AMBIGUOUS` | An attempt of an Action that is not safe to repeat ended without a usable result, so its outcome is unknown | Check the other system, then `accept_reconciled_result` or `terminate` |
    | `WV-TASK-RETRIES-EXHAUSTED` | A safe-to-repeat Action used every automatic attempt, or no time was left for another | Fix the cause, then `retry_safe` before the Action's deadline, or `terminate` |
    | `WV-TASK-DEADLINE` | Nobody claimed the task before its deadline | Its time budget is spent, so another attempt cannot run: usually `terminate`, then check that an executor or worker is running and [start a linked retry](#start-a-new-run-from-a-finished-one) |
    | `WV-TASK-FAILED` | The executor or worker reported a failure | `retry_safe` only when the side effect is safe to repeat; otherwise reconcile or `terminate` |

    The Action's side effect decides what is safe. Studio shows it as **Side
    effect**, under **What this action needs** in the **Call an action** step's
    inspector, when connected; from the CLI, read
    `spec.sideEffect` in `weave definitions export ACTION_VERSION_ID
    --collection actions --output json`.

2. **Understand which attempt is involved.** Read the run's
   [history](history-and-replay.md) and the [worker recovery](../guides/workers.md)
   rules to see which action and attempt are uncertain.

3. **Check the external system.** Use that system's own authorized procedure.
   Weave does not look up a payment, message, or database change for you.

4. **Choose a decision:**

    | Decision | Choose it when | Requirements |
    | --- | --- | --- |
    | `retry_safe` | The action is safe to repeat | Only for actions declared `read_only`, `idempotent`, or `idempotency_key` |
    | `accept_reconciled_result` | Independent evidence shows the result | An `output` valid for the action's pinned output schema, and an `evidence_reference` |
    | `terminate` | The run should stop | Nothing else |

5. **Write the decision.** Generate a new UUID for `receipt_id`; reusing one
   from another decision conflicts:

    ```sh
    # Generate a new receipt ID for this decision.
    python3 -c 'import uuid; print(uuid.uuid4())'
    ```

    Save this as `resolution.json` with that UUID, your real evidence reference,
    and the verified result in place of the output (`42` only fits an output
    schema that accepts that number):

    ```json
    {
      "receipt_id": "8947c9df-8766-4df1-963f-87acf8627edf",
      "kind": "accept_reconciled_result",
      "reason": "Provider lookup confirmed completion",
      "evidence_reference": "provider:receipt:123",
      "output": 42
    }
    ```

6. **Submit it with the revision you just read:**

    ```sh
    # Resolve the incident; the revision guards against a concurrent decision.
    weave runs incidents resolve INCIDENT_ID --revision REVISION --request resolution.json --output json
    ```

    Replace `REVISION` with the incident's `revision` from step 1.

    Expected: the incident with `"status": "resolved"` and your `resolution`.

7. **Read the incident and the run again.** The decision is audited, but another
   active incident or a deadline may still block the run. If the response was
   lost, resend the same receipt, body, and revision; if another decision won
   (HTTP 409), read the incident again before deciding anything.

## Cancel a run

Cancellation stops scheduling and revokes leases at once. It cannot recall work
already sent to an external system.

```sh
# Write the audited reason, then cancel the run.
printf '%s\n' '{"reason":"operator decision"}' > cancel.json
weave runs cancel RUN_ID --request cancel.json --output json
```

Expected: the cancelled run. Check `external_effects_may_continue` in the
response: when `true`, work already issued may still finish elsewhere. Cancelling
again returns the same cancelled snapshot without a new event; a run that already
ended in another state cannot be rewritten.

## Start a new run from a finished one

A **retry** of a whole run creates a new execution with new operation keys. The
original run, its outputs, and its history are unchanged.

1. **Check that the original run has finished.** Retry needs a terminal parent.
2. **Find the activation and the input.** Read the original run with
   `weave runs read RUN_ID --output json`. Copy its `activation.id`, or a newer
   activation's ID from `weave definitions activations list --output json`, and,
   to repeat the same data, its `state.input`.
3. **Write the start request.** Save this as `new-run.json`, replacing the
   activation UUID and the input:

    ```json
    {"activation_id":"00000000-0000-4000-8000-000000000001","input":{}}
    ```

4. **Start it with an idempotency key:**

    ```sh
    # Start a new run linked to the finished one; the key makes a resend safe.
    weave runs retry RUN_ID --idempotency-key retry-123 --request new-run.json --output json
    ```

    Expected: a new run whose `parent_run_id` is the original run. Reuse the same
    key only to resend this exact request.

The new run is checked against the current definition, release, and connection
bindings, and **it may cause external effects again**.

## Decisions and endpoints in detail

All paths are under
`/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}`.
Authorization uses your current scoped capabilities, even when a receipt is
repeated. Platform administration alone grants no execution rights.

| Request | CLI | Capability | Result |
| --- | --- | --- | --- |
| `GET /runs/{run}/incidents` | `weave runs incidents run-list` | `incident.read` | Incident views, including closed history |
| `GET /incidents` | `weave runs incidents list` | `incident.read` | Incidents across the environment |
| `POST /incidents/{incident}/resolve` | `weave runs incidents resolve` | `incident.resolve` | An immutable, revision-fenced resolution |
| `POST /runs/{run}/cancel` | `weave runs cancel` | `run.cancel` | Terminal cancellation with the external-effects flag |
| `POST /runs/{run}/retry` | `weave runs retry` | `run.retry` and normal start admission | A new run linked by `parent_run_id` |

**Resolve requests.** Send `If-Match` with the positive revision from the read,
and a JSON body with a UUID `receipt_id`, a `kind`, and a nonblank `reason` (at
most 2,000 characters). Only `accept_reconciled_result` takes an `output`;
`retry_safe` and `terminate` reject any `output`, even `null`. A reconciled output
is checked against the 1 MiB payload limit before anything is stored. The same
person resending the same request with the original revision gets the original
receipt; a different request with that receipt ID conflicts; two competing
receipts for one revision cannot both win. Authority is checked before a resend
is honored, so removing a grant also stops resends.

**`retry_safe`** may allow one attempt beyond the exhausted automatic budget, with
the original operation key and the remaining original action and run deadlines.
It neither resets the automatic budget nor extends a deadline. For an action whose
side effect is not safe to repeat, it fails with HTTP 409
`WV-RUNTIME-RECONCILIATION_REQUIRED`.

**`accept_reconciled_result`** needs an explicit `output` and a nonblank evidence
reference. An explicit `null` is valid only when the pinned output schema allows
it. The output is validated against the exact pinned schema and dependency bundle;
Weave performs no external lookup or effect itself.

**`terminate`** cancels the run: scheduling stops and leases are revoked
atomically. The response sets `external_effects_may_continue: true`. Authentic
late completions for revoked generations may receive an immutable `ignored`
receipt; they never advance the run.

**Several incidents.** Each resolution stores the actor, reason, kind, evidence,
time, and receipt. Independent incidents on different nodes or generations share a
run-wide suspension barrier: authenticated results that arrive meanwhile are kept,
and the run continues once, after the last blocking incident clears. An expired
overall or wait deadline can still end a suspended run; reconciliation cannot
bypass it.

**Cancel and retry requests.** Cancel takes `{"reason": "..."}` (nonblank, at most
2,000 characters). Retry takes a normal start request, `{activation_id, input}`
with optional `correlation_key` and `business_key`, and requires an
`Idempotency-Key` header.

**Older operator commands.** `weave incident list RUN_ID`,
`weave incident resolve INCIDENT_ID --revision N --request FILE`,
`weave run cancel RUN_ID --reason TEXT`, and
`weave run retry RUN_ID --idempotency-key KEY --request FILE` remain available.
They do not use saved platforms: they need `--environment-url` (or
`WEAVE_ENVIRONMENT_URL`) set to the full scoped environment URL and a
`WEAVE_ACCESS_TOKEN`. They require TLS outside loopback, refuse redirects, and
print JSON without the token.

## Forward migration and compatibility

This section is for administrators who apply database migrations.

Revision `0010_incidents` adds the scoped `incidents`,
`incident_resolution_receipts`, and `run_retry_links` tables. The kernel and its
accepted events stay authoritative; the new tables are projections kept in sync
in the same transaction, and no read repairs them.

- Existing active incidents are backfilled with deterministic run and key
  identities and revision 1, keeping node, generation, and code, with no invented
  actor or evidence.
- Older suspended runs without that information become `@legacy`, with unknown
  node and generation, and support only termination.
- Previously closed incidents stay in the immutable `run_events`; they are not
  reconstructed as audited resolution rows. Existing run and event JSON is
  unchanged.

The migration identity needs its normal DDL and table privileges plus explicit
catalog-only enumeration authority. Before migrating, the function owner, or an
administrator who can grant its privileges, must run:

```sql
GRANT EXECUTE ON FUNCTION public.weave_tenant_ids() TO your_migration_role;
```

Never grant this to the application role. Without it, the migration fails
clearly. It lists tenant IDs through the existing fixed-search-path
`SECURITY DEFINER` function and backfills under each tenant's forced row-level
security. It never changes role membership, membership options, business-table
policies, or the function's owner or `PUBLIC` privileges. Migration and backfill
are transactional; there is no destructive downgrade or startup repair. Follow
the [upgrade procedure](../operations/upgrades.md) before changing the running
version.

## Runs with unavailable legacy evidence

Some runs created before the current admission policy cannot be shown safely.

- **Reads.** Normal reads of such uncertain legacy runs return HTTP 409
  `WV-LEGACY-UNAVAILABLE` with a safe run ID, status, and omission metadata for
  `/state`; no fake redacted state is returned. New runs carry the server-owned
  `admission_policy: classified-v1`, which records policy provenance, never caller
  identity or authorization. Older unmarked artifacts stay compatible.
- **Cancellation still works.** It replaces only the mutable execution snapshot
  with an explicit `unavailable` terminal state, using the kernel's existing
  cancellation and revocation. The acknowledgment contains the ID, the cancelled
  status, the accepted sequence, the unavailable omissions, and
  `external_effects_may_continue`, and no activation, request, source, or
  payload. Repeating it returns the same acknowledgment. Cancellation never parses
  legacy activation, request, or artifact metadata, so malformed data cannot block
  it. Earlier history stays untouched, so replay reports incomplete evidence. An
  unavailable state cannot continue normally.
- **Deadlines still apply.** Persisted overall timeouts stay enforceable using
  database time and their issued deadline. Structurally verified signal deadlines
  can also end the run when no receipt was accepted strictly before them; an
  earlier receipt never causes unsafe continuation. A bounded structural check
  records `terminal_signals_verified`. Unverifiable artifacts support only overall
  timeout and cancellation. External effects may continue after these controls,
  and no historical payload is migrated or deleted.
- **Every access re-checks the artifact.** The pinned artifact is validated
  against the current structural and classification policy before the provenance
  marker is trusted, so the marker cannot bypass corrected admission rules.
  Invalid retained artifacts follow the unavailable, policy-block, and safe
  terminal-control paths even when an earlier build marked them.

## Next steps

- [Recorded history and offline replay](history-and-replay.md): read what
  happened before you decide.
- [Implement and operate a worker](../guides/workers.md): leases, attempts, and
  why an outcome can be uncertain.
- [Manage executions and business cases](../guides/execution-management.md):
  pause, resume, archive, and purge runs.
- [Troubleshooting](../operations/troubleshooting.md): locate the failing stage
  first.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| HTTP 409 `WV-RUNTIME-RECONCILIATION_REQUIRED` | `retry_safe` was used for an action that is not safe to repeat, the incident is not tied to an action (only `terminate` works), or a reconciled result lacks `output` or `evidence_reference` | Reconcile externally and use `accept_reconciled_result` with both fields, or `terminate` |
| HTTP 409 `WV-INCIDENT-REVISION` | Another decision changed the incident since you read it | Read the incident again; decide only if it is still active |
| HTTP 409 `WV-RUNTIME-STATE` | The incident is no longer active, or no failed task matches it | Read the run and its incidents again |
| HTTP 409 `WV-RUNTIME-DEADLINE` on `retry_safe` | The action's original deadline has passed | Use `terminate`, then start a linked retry of the whole run |
| HTTP 422 `WV-RUNTIME-OUTPUT` | The reconciled `output` does not match the action's pinned output schema or exceeds the payload limit, or an `output` was sent with `retry_safe` or `terminate` | Fix the output to match that schema; leave `output` out for the other decisions |
| HTTP 409 `WV-INCIDENT-RECEIPT-CONFLICT` | You reused a `receipt_id` with a different body or revision, or another person used it | Resend the original request unchanged, or generate a new `receipt_id` for a new decision |
| The run stays suspended after a resolution | Another incident is still active, or a deadline applies | List the run's incidents again and resolve each active one |
| `retry` is rejected, for example with HTTP 409 `WV-RUNTIME-STATE` | The parent run has not finished, or the start request is invalid | Wait for the run to finish, or cancel it first; check the input |
| `external_effects_may_continue: true` after cancel | Work was already sent to another system | Check that system and compensate there if needed |
| HTTP 409 `WV-LEGACY-UNAVAILABLE` | An older run's state cannot be shown safely | Cancel it if needed; replay reports its evidence as incomplete |
