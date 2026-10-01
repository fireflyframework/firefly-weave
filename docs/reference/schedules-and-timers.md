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

# UTC schedules and duration waits

## Start periodically or pause an existing run

A **schedule** creates a new run at future UTC calendar times. A Workflow **wait**
pauses the same run until a persisted duration deadline. Both need a running
scheduler/recovery replica; neither requires an in-memory timer in your worker.

For a first schedule, complete [standalone setup](../guides/standalone.md) and
activate a Workflow through the authoring/deployment flow. If it contains Actions,
complete [worker admission](../guides/workers.md) too. Obtain `activation_id` from
the activation response; it is not the Workflow definition version UUID.

1. Save the complete create body below as `schedule.json`, replacing the activation
   UUID and `input` with input accepted by that Workflow's schema. `*/5 * * * *`
   means every five minutes on the UTC clock.
2. Run `weave triggers schedules save --request schedule.json` with the scoped CLI
   configuration described below. Save the returned `id` and `revision`. A
   `ScheduleView` also exposes `status`, `next_due_at`, `principal_id`, and
   `blocked_reason`; inspect these before expecting a firing.
3. After the next due time, inspect `history`. A `started` occurrence includes
   `run_id`; read that run separately to see completion. A `skipped` range means
   missed work was recorded without creating catch-up runs.
4. Before editing/disabling, read the current revision and send it as `If-Match`
   (or CLI `--revision`). After a conflict, reread and reassess the change; do not
   guess the next revision.

If a schedule is `blocked`, inspect `blocked_reason`, its pinned activation's
readiness and the current owner's grants. An authorized save/enable creates a
fresh future cursor; it does not replay the missed interval. If a run remains
waiting after its duration, check the scheduler and any run-wide incident barrier.
A cron schedule is not a promise of exactly-once external effects or catch-up.

## Calendar and request contract


Schedules use the current database UTC clock. `skip` starts at most the occurrence
in the current minute, in the half-open interval `[instant, instant + 60 seconds)`.
A final database-time admission check prevents stale starts after readiness waits.
Older occurrences become one immutable skipped range per observed revision/span;
there is no backlog enumeration. The pinned cron expression determines exact
instants within the range. A backward clock never moves the cursor backward.

The grammar has exactly five numeric minute/hour/day-of-month/month/day-of-week
fields, Sunday 0, with `*`, numbers, nonwrapping ranges, lists (at most 32 terms per
field), and positive steps on `*` or ranges. Expressions are at most 256 characters.
Numeric components/steps have at most two digits. At least one of DOM/DOW must be
literal `*`. Names, macros, seconds/year fields, `?`, `L`, `W`, `#`, random/hash
syntax and non-UTC zones are rejected. Impossible dates are rejected. The pure
`contracts.schedules.validate_calendar` owns this grammar and realizability check;
both ScheduleRequest and the runtime calendar constructor call it. The pure
module imports no PyFly. Calendar math uses public PyFly 26.9.15 `CronExpression`, backed by pinned croniter 6.2.4
and its 50-year search bound. Date-range exhaustion blocks a schedule safely.

Create/update with `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules`:

```json
{"cron":"*/5 * * * *","timezone":"UTC","missed_policy":"skip","activation_id":"00000000-0000-4000-8000-000000000001","input":{}}
```

An optional `id` selects an existing schedule; updates require its positive
revision in `If-Match`. Canonical `GET /schedules` accepts `cursor` and
`limit=1..100`, returning `items` and an opaque `next_cursor`.
`GET /schedules/{id}` returns the head. `POST /schedules/{id}/disable`, `/enable`
and `/delete` require `If-Match`. Delete is a tombstone retaining revisions and
history. Enable creates a new revision with a fresh future cursor. Occurrences
are at `GET /schedules/{id}/occurrences?limit=100`, including
skipped ranges, observation time and run identity. Pass the returned `next_cursor`
as `cursor` to continue either canonical list. Cursors bind the caller scope and
collection. Lists include tombstones. Unversioned compatibility routes retain
their older `after` parameters and response shapes.

All mutations require current `trigger.manage`; save/enable also require current
`run.start`, current execution readiness and valid input. The authenticated
modifier becomes the new revision's execution principal. No arbitrary principal
selector exists. Detail/list/history require `run.read`. Every firing reloads
owner status/grants. Revoked/disabled owners block without consuming an occurrence;
authorized save/enable can rebind and restore a fresh future cursor. Run creation,
occurrence receipt, skipped ranges and cursor advancement commit atomically.

For the canonical CLI, configure `WEAVE_BASE_URL`, `WEAVE_TENANT_ID`,
`WEAVE_PROJECT_ID`, `WEAVE_ENVIRONMENT_ID`, and protected authentication as described
in [remote operations](cli.md#remote-authoring-and-operations). Save the returned
schedule UUID as `SCHEDULE_ID`. Before each mutation, read the current schedule
and set `SCHEDULE_REVISION` to its returned revision. The mutation examples below
are separate operations; do not reuse a stale revision after a successful change.

```sh
weave triggers schedules save --request schedule.json
weave triggers schedules list --limit 100
weave triggers schedules read "$SCHEDULE_ID"
weave triggers schedules history "$SCHEDULE_ID" --limit 100
weave triggers schedules save --request schedule-edit.json --revision "$SCHEDULE_REVISION"
weave triggers schedules disable "$SCHEDULE_ID" --revision "$SCHEDULE_REVISION"
weave triggers schedules enable "$SCHEDULE_ID" --revision "$SCHEDULE_REVISION"
weave triggers schedules delete "$SCHEDULE_ID" --revision "$SCHEDULE_REVISION"
```

Use `--cursor` with list/history to request subsequent pages. The older singular
`weave schedule` family remains available for unversioned compatibility URLs.

The single lifespan-owned recovery loop visits at most 16 tenants per cycle,
reserving one environment per tenant using durable rotating cursors. Each turn
reserves recovery pages of 10 deadlines, 10 expired ready tasks and 10 recoverable
leases before a separate page of at most 10 due schedules. These are separate
work classes, not a claim that ten total rows change. Independent transaction/time
budgets (at most five seconds per phase including pool acquisition), four-second
statement timeout and one-second lock timeout isolate failures. Tenant and
environment traversal resumes after failures/restarts. A failed turn consumes no
business occurrence; the traversal cursor simply allows other tenants to progress.
The actual latency bound depends on tenant/environment count and configured poll
interval; overloaded schedules may skip minutes by policy. This is not a promise
that unlimited tenants all execute within one minute.

Ordinary `wait` persists a deadline and continues with null step output when due.
It uses the same kernel and deadline scanner as signals, including nested branch
capacity, cancellation, restart and the incident suspension barrier. Elapsed waits
are counted separately from terminal timeouts in recovery reports. An elapsed
fact behind suspension consumes its deadline once and defers continuation until
resolution; branch capacity is not released early.

The persisted `wait_elapsed` event records `node_id`, issued `deadline`, durable
`wait_id`, database observation timestamp, event ID and accepted sequence. Replay
uses those facts, never a live clock. The pure kernel checks the active WaitNode,
its exact issued due instant and no-early-wakeup rule. When several deadlines are
due at observation, overall-run timeout is authoritative, then terminal signal
timeouts, then elapsed duration waits ordered by due instant and node ID. A signal
receipt strictly before its signal deadline wins that signal's timeout; equal time
does not. Signal events also record receipt acceptance time and issued due instant.
The [history and replay](history-and-replay.md) and [simulation](simulation.md)
APIs use recorded transition facts without contacting external providers.

## Forward migration privileges

Revision `0011_schedules` follows `0010_incidents`; forward migrations preserve
prior migration history.
Besides ordinary DDL/REFERENCES and schema-version permissions, this migration
requires ownership (or inherited ownership) of `wait_wakeups`, permission to
`SET ROLE weave_catalog_reader`, and schema `CREATE WITH GRANT OPTION`. Deployment
administrators must grant these deliberately; `weave_tenant_ids()` EXECUTE alone
is insufficient. The migration checks before any schema mutation and never grants
role membership. A database owner/superuser deployment identity is another option.
Do not give these deployment privileges to API or scheduler login roles.

The new `weave_scheduler_tenants(integer)` is owned by the existing non-superuser,
non-BYPASSRLS catalog reader, has fixed search path and accepts pages 1..16. It
returns tenant IDs and advances only its catalog cursor. Only `weave_scheduler`
has runtime EXECUTE; its login retains no direct tenant/schedule/run table access.
The migration temporarily gives the function owner schema CREATE solely for the
ownership transfer, then revokes it. Business tables retain FORCE RLS; environment
rotation and all starts use ordinary tenant-bound application transactions.

Delivery remains at least once; external effects are not exactly once.
[Secret classification](schema-profile.md#durable-secret-classification) applies
to persisted schedule input and workflow execution data.
