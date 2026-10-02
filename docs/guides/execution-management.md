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

# Manage executions and business cases

A workflow is a reusable definition. An **execution** (also called a run) is one
instance of that definition, with its own input, state, tasks, and history.
A project can have many definitions and many simultaneous executions. Each run
belongs to one environment. Project and environment quotas still bound capacity;
there is no one-run-per-project restriction.

The built-in policy allows up to 1,000 active runs per environment and 10,000
retained runs per project, subject to the other task, message, and storage limits.
Operators can lower these ceilings through the
[operational policy](../operations/configuration.md#operational-policy).
Archiving preserves the retained-run count; a successful purge releases it.
These are admission ceilings, not a claim of measured throughput at that scale.

These additions are included in alpha5; alpha4 predates them. First follow
[Studio setup](studio.md) or [connect your client](connect-to-api.md).

![Business keys, independent runs, and the archive lifecycle](../diagrams/execution-lifecycle.svg)

## 1. Choose identifiers with different jobs

| Identifier | Example | Meaning |
| --- | --- | --- |
| Run ID | Server-generated UUID | Addresses one exact execution, its signals, and its history |
| `business_key` | `order-1042` | Groups executions concerning the same business object |
| `correlation_key` | `fulfillment-2026-1042` | Groups related executions in a wider process or integration |
| `Idempotency-Key` | `submit-order-1042-attempt-1` | Lets the same caller safely repeat the same start command |

Business and correlation keys are optional, case-sensitive, exact-match strings
of at most 200 characters. They are **not unique**. For example, an order may
have separate payment, shipping, and return runs with the same business key.
Do not use sensitive customer information as a key.

To retry an HTTP request whose response was lost, reuse its idempotency key and
identical request. To intentionally create another execution, issue a new key.
Neither changing the business key nor refreshing a screen is a substitute for
choosing the intended execution identity.

## 2. Start and locate an execution

In Studio, open **Runs**, select **Start execution**, choose an activation, enter
its input, and optionally supply the business and correlation keys. An activation
selects the published workflow version and prepared execution bindings.

For a host application, extend the normal start request:

```python
from uuid import uuid4
from firefly_weave.contracts.runtime import StartRunRequest

# client is an authenticated WeaveClient scoped to the target environment.
# activation_id is the UUID returned when your published version was activated.
request = StartRunRequest(
    activation_id=activation_id,
    input={"orderId": "1042"},  # Must match that workflow's input schema.
    business_key="order-1042",
    correlation_key="fulfillment-2026-1042",
)

# Keep this key with your product's command record before sending the request.
command_key = str(uuid4())
run = await client.start_run(request, idempotency_key=command_key)

# Search returns a page, because one business key can match several executions.
page = await client.list_runs(business_key="order-1042", status="waiting")
for execution in page.items:
    print(execution.id)
```

This is a fragment inside an async SDK application. The
[complete SDK tutorial](sdk-tutorial.md) explains imports, authentication, scope,
and the client's async context manager. Replace the illustrative order input
with input accepted by your workflow.

The API search is `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs`.
It accepts `business_key`, `correlation_key`, `status`, `include_archived`, `limit`,
and `cursor`. Multiple filters are combined. Keep the same filters when following
a cursor; changing them invalidates a canonical API cursor. In the CLI, use
`weave runs list --help` for scope/authentication options and pass
`--business-key order-1042 --status waiting`.

Select a specific returned run before sending a signal. A business key does not
silently broadcast an event or choose the first matching run. The existing
`runs/{run_id}/signals` endpoint targets one exact instance, preserving ambiguity
as an explicit decision for your application.

## 3. Understand why a run is waiting

A human task waits for an eligible, authenticated person. A signal wait waits for
an external event. A timer waits until its deadline. An operator pause holds
progress until an authorized resume; an incident needs an operational decision.
These situations have different controls and should not share a generic
“complete” button.

Open a run in Studio to inspect its pinned workflow, state, history, and wait
reason. Use **Tasks** to claim and complete assigned human work. Follow the
[human-task walkthrough](human-tasks.md) for groups, forms, decisions, expiry,
claim conflicts, pause/resume, and API examples. Closing a browser never completes
or cancels a task. Email replies remain [email events](email.md), not implicit
human approvals.

## 4. Archive finished work

Archiving hides a terminal run from the normal list while preserving its data,
tasks, and history. Allowed states are `succeeded`, `failed`, `cancelled`, and
`timed_out`. Finish or explicitly cancel an active execution first.

The `operator` and `execution_manager` roles include `run.archive`.
In Studio, open the finished run, enter a reason, and select **Archive**.
Enable **Include archived** in the list to find it again. **Restore** puts it back
in the normal list; restoring does not restart execution.

For SDK callers, read the separate lifecycle revision before making a change:

```python
from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest

# Lifecycle revision is independent of runtime sequence or human task revision.
lifecycle = await client.run_lifecycle(run.id)
archived = await client.archive_run(
    run.id,
    RunLifecycleRequest(
        expected_revision=lifecycle.revision,
        reason="Order processing and review are complete",
    ),
    idempotency_key="archive-order-1042-attempt-1",
)
```

The API equivalents are `GET runs/{run_id}/lifecycle`,
`POST runs/{run_id}/archive`, and `POST runs/{run_id}/restore`, under the same
environment path. Mutation bodies contain `expected_revision` and `reason`,
and require an `Idempotency-Key` header. A stale revision returns HTTP 409;
refresh the lifecycle before deciding whether to submit a new command.

## 5. Permanently delete an archived execution

Permanent deletion requires the separate **`execution_manager`** role with
`run.purge`. Ordinary operators cannot purge runs. The execution must already be
archived and terminal. Studio requires the exact run UUID as confirmation.

```python
from firefly_weave.contracts.run_lifecycle import RunPurgeRequest

# This removes execution payloads and history; run it only after retention review.
receipt = await client.purge_run(
    run.id,
    RunPurgeRequest(
        expected_revision=archived.revision,
        reason="Approved retention period has ended",
        confirm_run_id=run.id,
    ),
    idempotency_key="purge-order-1042-attempt-1",
)
```

The API operation is `POST runs/{run_id}/purge` with the same body and header.
The CLI exposes `weave runs lifecycle`, `archive`, `restore`, and `purge` using
the positional run UUID, `--request`, and `--idempotency-key`; inspect each command's help
for the required scope and authentication arguments.

Deletion removes the run snapshot, inputs/outputs, event history, step results,
worker completion data, signals, incidents, and associated human task forms,
decisions, and task history. It retains a small scoped deletion receipt, the
access audit of who requested the operation, and redacted idempotency records.
Replaying an earlier start after deletion returns HTTP 410 instead of recreating
the execution or returning its old payload. Repeating the same purge command
returns its recorded receipt.

A still-active worker lease or a retained reference from email, schedules,
triggers, or retry relationships blocks deletion with HTTP 409. The operation is
atomic: a blocked purge removes nothing. Leave such runs archived; this version
does not cascade deletion into those independently managed resources. Archiving
is therefore the supported retention option for linked conversations and similar
records until their dependency lifecycle permits removal.

Purging local execution records does not recall an email, undo external work,
erase exported files, remove backups, or delete data held by a connected product.
Plan retention for those systems separately.
