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

# Handle step errors

By default, a step that fails for good stops the run behind an incident, and a
person decides what happens next. The **On error** setting (`onError`) lets a
step handle its own failure instead, the way n8n's "On error" setting does:

| `onError` | What a final failure does |
|---|---|
| `stop` (default, omitted) | The run stops behind an incident, as before. |
| `continue` | The run goes on. The step's output is `null`, and its error is recorded. |
| `errorOutput` | The step's **error path** (`errorPath`) runs, and its `output` becomes the step's output. The run then goes on after the step. |

A handled failure is data, not an incident. Later steps read it at
`/steps/<id>/error`. Runs with handled failures still end `succeeded`, and their
run facts count the handled errors.

Studio does not have controls for this setting yet: edit `onError` and
`errorPath` in the source view.

## Before you start

You need a platform that lists `flow.onError` in its language manifest
(`weave remote capabilities` shows `language_features`). The example workflow is
`examples/language/step-errors.workflow.yaml`.

## Which steps can handle errors

Actions (including Call an API, email, and file steps), AI tasks, transforms,
decision tables, human tasks, signals, transactions, and calls that wait for the
called workflow's result (`mode: wait`). Switches, parallel steps, loops, waits,
and Fail steps cannot. Detached calls cannot either: there is no result to wait
for.

Some failures are never handled, whatever the setting says: platform invariants
(`WV-RUNTIME-*`), capacity errors, the run's own timeout, cancellation, and
termination.

## Continue

```yaml
- id: lookup
  kind: action
  uses: records.lookup-record@1.0.0
  with:
    object:
      id: {ref: /input/requestId}
  onError: continue
```

When `lookup` fails for good, its binding becomes `{"output": null, "error": E}`
and the run follows its next step. Read the error with `exists` and `coalesce`:

```yaml
found:
  op: {name: not, args: [{op: {name: exists, args: [{ref: /steps/lookup/error}]}}]}
lookupError:
  op: {name: coalesce, args: [{ref: /steps/lookup/error/code}, {literal: ""}]}
```

## Continue with error output

```yaml
- id: submit
  kind: action
  uses: requests.submit-request@1.0.0
  with:
    object:
      id: {ref: /input/requestId}
  onError: errorOutput
  errorPath:
    steps:
      - id: notify-team
        kind: action
        uses: team.notify-team@1.0.0
        with:
          object:
            text: {ref: /steps/submit/error/code}
    output:
      object:
        submitted: {literal: false}
        reason: {ref: /steps/submit/error/code}
```

The error path is a block like a branch of a switch: its steps run in order, it
reads everything the step could read plus `/steps/submit/error`, and its
`output` becomes `/steps/submit/output`. Inside the path, the step has no
output yet, so `/steps/submit/output` is refused (`WV-COMP-ERROR_PATH_OUTPUT`).
A Fail step in the path fails the run with its own code. When the step
succeeds, the path never runs.

`errorPath` is required with `errorOutput` and refused with any other value
(`WV-COMP-ERROR_PATH`).

## The error object

| Field | Meaning |
|---|---|
| `code` | The failure's code, such as `RATE_LIMITED`, `WV-TASK-DEADLINE` or `WV-SIGNAL-TIMEOUT`. |
| `message` | Product copy for the code (a called workflow's Fail step keeps its own message). Never worker, provider or input text. |
| `outcome` | `not_started` (nothing ran), `failed` (it ran and applied nothing), or `unknown` (it may have applied). |
| `attempts` | How many attempts started. |
| `instanceKey` | The step instance that failed, such as `verify[2]` inside a loop. |
| `childRunId` | For a call, the child run, when one started. |

The compiler types it: after Continue, the step's output is the action's output
or `null`; after an error path, it is the action's output when the path's output
fits it, otherwise either one. Reading `/steps/<id>/error` of a step without the
setting is refused (`WV-COMP-ERROR_REFERENCE`).

## Retries come first

The setting applies only where the run would otherwise stop: after the action's
own retries are used up, a task nobody picked up before its deadline, a worker
that stopped responding on the last attempt of an action that is safe to
repeat, a step whose input or expression is invalid, a signal or human task that
timed out, a call that could not start, or a called workflow that failed, was
cancelled or ran out of time.

## When the outcome is unknown

An action that changes data and is never retried (`sideEffect: non_idempotent`)
may have applied its change when its outcome is unknown. Weave never guesses:
the run still stops behind `WV-TASK-AMBIGUOUS`, whatever the setting says, and
the compiler notes it on the step (`WV-COMP-ON_ERROR_AMBIGUOUS`). A person checks
the other system, then either:

- resolves it as before (`accept_reconciled_result` with the real output, or
  `terminate`), or
- chooses **Confirm it had no effect** (`accept_reconciled_failure`), with a
  reason and an evidence reference. The step's setting then applies: the error
  reads `WV-TASK-RECONCILED-FAILURE` with outcome `failed`.

Confirm it had no effect is refused (`409 WV-RUNTIME-RECONCILIATION_REQUIRED`)
for a step without the setting, without evidence, and for incidents of calls.
See [Incident operations](../reference/incident-operations.md).

## Waits and calls

A signal or human task whose step handles errors does not end the run when its
timeout passes: the step fails with `WV-SIGNAL-TIMEOUT` or `WV-HUMAN-EXPIRED`,
an open human task is closed as expired, and a signal received before the
deadline still wins. A waiting call with the setting receives a child that
failed (`outcome: failed`, with the child's code and message), was cancelled or
timed out (`outcome: unknown`), and a call that could not start
(`WV-CALL-START`, `outcome: not_started`).

## Loops, parallel steps and transactions

Inside a loop, each iteration handles its own failure; the loop still returns
one entry per item, in item order. A handled failure inside a parallel branch
stays in that branch. Inside a transaction, a step with the setting takes a
failure only when nothing was applied; a failure that may have applied still
undoes the transaction. A transaction step with the setting handles its own
clean rollback (`WV-TXN-ROLLED-BACK`, outcome `failed`); a transaction in
doubt is never handled.

## What you see

- **History:** a `step_failed` event, and the step instance recorded `failed`
  with its error. Histories with handled failures export and replay.
- **Facts:** step facts say how the failure was handled (`continue` or
  `errorOutput`); run summaries count `handled_errors` (omitted when 0), and
  run lists filter on `has_handled_errors`.
- **Metrics:** `weave.step_errors`, by operation (`step.continue`,
  `step.errorOutput`, `step.incident`).
- **No incident** and no alert.

## Try it in a debug session

A debug session can make a step fail on purpose. Add `errors` next to `mocks`,
keyed the same way (`node:<step>`, `node:<instance key>`, `action:<reference>`):

```json
{"errors": {"node:verify[2]": {"code": "RATE_LIMITED"}}}
```

`outcome` (default `failed`) and `attempts` (default 1) are optional. A key in
both `mocks` and `errors` is refused (`WV-DEBUG-MOCK-CONFLICT`). With the local
simulation, repeat `--error KEY=CODE`. See [Simulation](../reference/simulation.md).
