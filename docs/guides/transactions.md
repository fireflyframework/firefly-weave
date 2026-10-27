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

# Undo steps together with a transaction

A **Transaction** step (`transaction`) groups steps that change other systems,
so that either all of them go through or what they did is undone. Each step
inside it that changes data says how it is undone, and Weave runs those undos
for you, in reverse order, when something fails, when the run is canceled, or
when it times out. This guide runs the example under
`examples/language/move-between-systems.workflow.yaml` and shows what to do
when a transaction is **in doubt**: when Weave can't tell, on its own, what
happened in another system.

## Before you start

You need a platform that lists `flow.transaction` in its language manifest
(`weave remote capabilities` shows `language_features`), the `developer`,
`deployer`, and `operator` roles in an environment, and a signed-in CLI
([Connect the CLI to a platform](connect-to-api.md)). Resolving a transaction
in doubt by saying how it ended, or ending it by force, also needs the
`transaction_reconciler` role.

## 1. Say how each step is undone

The example moves a quantity from one system to another. It reserves the
quantity in the source, adds it to the target, and then submits the order,
which can't be undone:

```yaml
- id: move
  kind: transaction
  businessKey: {ref: /input/moveId}
  steps:
    - id: hold            # reserves: confirmed later, or released
      confirm: {kind: action, uses: source-system.confirm-hold@1.0.0, ...}
      cancel: {kind: action, uses: source-system.release-hold@1.0.0, ...}
      holdSeconds: 600
    - id: add             # reversed by a called workflow
      compensate: {kind: callWorkflow, uses: reverse-target-addition@1.0.0, ...}
    - id: submit          # can't be undone, so it comes last
      irreversible: true
```

The compiler checks every path before you publish: every step that changes
data says how it is undone, the step that can't be undone comes after every
step that can, and reservations last longer than that step takes. The full
rules are in [Workflow language: Transactions](../contracts.md#transactions).

## 2. Publish, activate, and run it

Publish `reverse-target-addition` and activate it, then publish and activate
`move-between-systems`, pinning the reversal like any call
(`--pin-current-calls`). Start a run as usual. When every step succeeds, Weave
confirms the reservation and the transaction is `completed`:

```bash
weave runs transactions "$RUN_ID" --output json
```

Each item names the transaction, its state, its business key, each step's key,
how it is undone and where it stands, and never a business value.

## 3. What Weave does when something fails

| What happened | What Weave does |
| --- | --- |
| A step before the order fails | Undoes the steps that went through, newest first: it reverses `add`, then releases `hold`. The transaction is `compensated` and the run fails with `WV-TXN-ROLLED-BACK` |
| A step failed and its worker says nothing was applied | Skips undoing that step; only what went through is undone |
| A reservation is about to expire | Undoes the transaction before it does (`WV-TXN-HOLD-EXPIRED`) |
| You cancel the run, or it times out | Undoes the open transaction first, then ends the run canceled or timed out |
| The order is in flight when you cancel | Waits for its result: if it went through, the transaction finishes; if it definitely failed, it is undone |

An undo is retried on its own (by default up to 10 times over up to a day). The
run's log says what is being undone and why: `move: undoing 2 steps after add
failed (UNAVAILABLE)`.

## 4. Resolve a transaction in doubt

When an undo or a confirm can't finish, or nobody knows whether the order went
through, the transaction is **in doubt**. The run stops behind a high-priority
incident (`WV-TXN-UNDO-STUCK`, `WV-TXN-CONFIRM-STUCK`,
`WV-TXN-AFTER-PIVOT-STUCK`, `WV-TXN-PIVOT-UNKNOWN` or `WV-TXN-SETTLE-TIMEOUT`)
whose `transaction` field lists each step and the ones still pending. Nothing
but a person's decision moves it. Check the other systems first, then choose:

| Decision | Kind | When |
| --- | --- | --- |
| **Retry the undo** (or the confirm) | `retry_safe` | The cause is fixed. The same step runs again with the same operation key and a fresh time limit, and undoing resumes where it stopped |
| **Mark as reconciled** | `accept_reconciled_result` with a reconciliation record | You fixed things by hand. Say how the transaction ended (`compensated` or `completed`) and what happened to each stuck step; with `completed`, give the block's output, which later steps read |
| **It happened** | `accept_reconciled_result` with the step's output | The order went through. Weave confirms the reservations and runs the steps after it |
| **It didn't happen** | `accept_reconciled_failure` | The order never went through. Weave undoes the steps before it |
| **Force end** | `terminate` | Stop here: nothing more is done or undone. The run ends canceled and what was applied stays applied |

Retry needs `incident.resolve`. The others also need `transaction.reconcile`,
an audited reason, and (except Force end) an evidence reference. See
[Incident operations](../reference/incident-operations.md#transactions-in-doubt)
for the request bodies.

## 5. Stop a run without undoing it

A reconciler can cancel a run without undoing its open transactions: they end
`force_ended` and `external_effects_may_continue` is `true`.

```bash
weave runs cancel "$RUN_ID" --request cancel.json   # {"reason": "...", "skip_undo": true}
```

## Test a step that takes part in a transaction

A debug session doesn't simulate transactions yet: it stops with
`WV-DEBUG-MISSING_MOCK` and asks you to run the workflow on a test activation.
To check that the systems you call behave as a transaction expects, try these by
hand on a test activation:

- send the same confirm, cancel or undo twice: the second must change nothing;
- cancel a reservation that never went through: the cancel must succeed;
- let a step hang past its time limit: Weave treats its outcome as unknown and
  undoes it;
- confirm a reservation after it expired: the confirm must fail with a code the
  action lists in `definiteFailures` or report `effect: none`.

## Next steps

- [Workflow language: Transactions](../contracts.md#transactions)
- [Worker protocol: tasks of a transaction](../reference/worker-protocol.md)
- [Integration events: transaction events](../reference/integration-events.md)
- [History and replay](../reference/history-and-replay.md)
