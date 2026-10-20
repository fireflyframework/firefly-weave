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

# Call another workflow

A **Call a workflow** step (`callWorkflow`) starts an exact published version of
another workflow as a separate run, called a child run. Use it to reuse a
workflow from several others, or to give a part of a process its own history,
incidents, and owners. This guide publishes the two example workflows under
`examples/language/`, activates them, and runs a call end to end.

## Before you start

You need a platform that lists `flow.callWorkflow` in its language manifest
(`weave remote capabilities` shows `language_features`), the `developer`,
`deployer`, and `operator` roles in an environment, and a signed-in CLI
([Connect the CLI to a platform](connect-to-api.md)).

## 1. Let the called workflow accept calls

`examples/language/notify-customer.workflow.yaml` declares `callable: {}`, so any
workflow in the project may call it. To allow only some callers, list them:
`callable: {allowedCallers: [order-intake]}`. Being callable is part of the
version: change it in a new version.

## 2. Call it

`examples/language/order-intake.workflow.yaml` calls it twice: `notify` waits for
the result and continues on failure, so the step returns
`{status, runId, output, error}`; `audit` is detached, so it returns `null` and the
workflow continues at once.

```yaml
- id: notify
  kind: callWorkflow
  uses: notify-customer@1.0.0
  with:
    object:
      customerId: {ref: /input/customerId}
      message:
        op:
          name: concat
          args: [{literal: "Order "}, {ref: /input/orderId}, {literal: " received"}]
  mode: wait
  onFailure: continue
  businessKey: {ref: /input/orderId}
```

Without `onFailure: continue`, the step returns the called workflow's output, and
a child that fails fails the caller with the child's error code.

## 3. Publish and activate both

Publish `notify-customer` first, activate it, then publish `order-intake`. When
you activate `order-intake`, pin the activation each call starts:

```bash
weave definitions activations create --request order-intake-activation.json \
  --pin-current-calls --idempotency-key order-intake-1 --output json
```

`--pin-current-calls` adds the current activation of `notify-customer@1.0.0` to
`workflow_activation_ids` and prints the pins before it submits. A newer
activation of `notify-customer` does not change a pinned caller; retiring the
pinned activation or its version makes later calls fail to start.

## 4. Run it and follow the child

Start `order-intake` as usual. The run waits at `notify` while the child runs;
the recovery loop starts the child within about a second and delivers its result
within about another second. Then list the calls:

```bash
weave runs calls "$RUN_ID" --output json
```

Each item names the step and iteration, the call's generation, the child run's
ID, and its status. `weave runs read "$CHILD_RUN_ID"` shows the child, whose
`caller` field names the run and step that started it.

## What happens when something goes wrong

| What happened | With `onFailure: stop` (the default) | With `onFailure: continue` |
| --- | --- | --- |
| The child fails at a `fail` step | The caller fails with the child's code and message | The step returns `status: failed` and the child's `error` |
| The child is cancelled or times out | The caller stops behind `WV-CALL-INTERRUPTED` | The step returns `status: cancelled` or `timed_out` |
| The child cannot start | The caller stops behind `WV-CALL-START` (or `WV-CALL-DEPTH`) | The same |

Resolve a call incident like any other ([incident operations](../reference/incident-operations.md)):
`retry_safe` starts a new child run, `accept_reconciled_result` continues with an
output you supply, and `terminate` ends the run. Cancelling the caller cancels its
waiting children, and theirs; detached children keep running.

## Limits

- Calls stay in one environment, and the chain of calls is at most 8 deep.
- A child runs under the calling run's principal, which must still hold
  `run.start` when the child starts.
- There is no timeout on the call itself: set `timeoutSeconds` on the called
  workflow or on the caller.
- Each waiting call costs about two recovery polls of latency; inside a loop, set
  `concurrency` to run several calls at once.

## Next steps

- [Workflow language: Workflow calls](../contracts.md)
- [History and replay: calls in a history](../reference/history-and-replay.md#calls-in-a-history)
