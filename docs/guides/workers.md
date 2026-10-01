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

# Implement and operate a worker

A **worker** is a process that receives a task from Weave, calls your business
code, and reports a result. Use one when a workflow must perform work outside the
pure expression language, such as recording a customer in another service.
The echo workflow in the [standalone tutorial](standalone.md) needs no worker;
finish that tutorial before adding this integration.

This guide explains the checked-in [worker example](../../examples/worker/main.py)
and its [manifest](../../examples/worker/manifest.json). Its external receiver,
identity, image, and grants must be provisioned through the
[deployment guide](../operations/deployment.md). Running `main.py` alone is not a
complete deployment.

![Worker release admission, instance registration, and task lease lifecycle](../diagrams/authoring-worker-lifecycle.svg)

Read preparation across the top, then the task attempt below. A release, instance, and lease answer different questions: which build is allowed, which process is running, and which attempt it may execute. [Open the diagram at full size](../diagrams/authoring-worker-lifecycle.svg).

## 1. Define the work before implementing it

An **Action** is the reusable workflow-facing contract. A **task capability** is
the worker-facing contract that implements it. This example uses the exact
capability `example-record@1.0.0`:

| Contract field | Example value | Meaning |
| --- | --- | --- |
| `taskType` / `taskVersion` | `example-record` / `1.0.0` | Exact handler identity |
| Input | `{"customer": "demo"}` | Required string, no extra properties |
| Output | `{"receipt": "accepted", "customer": "demo"}` | Required receipt constant and customer string |
| `timeoutSeconds` | `180` | Absolute task execution budget |
| `sideEffect` | `idempotency_key` | The external target must support deduplication by operation key |

The published Action names this capability in its `implementation`. The workflow
calls the Action by name/version. The admitted release declares the same schemas
and policy. All three must agree; a similarly named handler does not satisfy a
different version's contract.

## 2. Implement one handler

The [example handler](../../examples/worker/main.py) receives a `TaskLease`.
Its `input` is the validated task input. It posts that input to `WEAVE_EFFECT_URL`
and sends `lease.operation_key` as the target's `Idempotency-Key`. It returns the
target's JSON response, which must satisfy the output contract above.

The essential wiring inside an already authenticated process is:

```python
from firefly_weave.sdk.worker import Worker

# transport is the authenticated WorkerTransport created after registration.
# record is an async function accepting one TaskLease and returning JSON.
worker = Worker(transport, {"example-record@1.0.0": record}, concurrency=1)
await worker.run()
```

This is a wiring excerpt; use the complete example for authentication,
registration, and shutdown. `Worker` handles claims, heartbeats, and completion
delivery around the handler. A **lease** is temporary permission to execute one
task attempt. Its generation changes when recovery issues a new attempt; an old
generation cannot complete the new one. Never print the lease's secret proof.

Keep credentials outside business inputs, logs, and returned results. The API
validates results against the pinned schemas; it rejects present secret-classified
values rather than storing a masked successful output.

## 3. Admit the release, then register an instance

A **release** identifies an immutable build and its allowed capabilities. An
**instance** is one running process of that release. A deployer admits the release
before a worker can register; a worker cannot grant itself new capabilities.
The deployment guide walks through the commands in this order:

1. Build the image and record its actual immutable image digest.
2. Provision the worker's verified identity and scoped grants, admit the manifest,
   and retain the returned release ID.
3. Activate the workflow with the matching worker release binding.
4. Start the process with the environment below. It registers its instance and
   claims only compatible, authorized tasks.

| Variable used by the example | Source |
| --- | --- |
| `WEAVE_API_URL` | The deployed API origin |
| `WEAVE_ENVIRONMENT_URL` | The scoped API path for the selected environment |
| `WEAVE_TOKEN_URL` | The configured identity provider's token endpoint |
| `WEAVE_WORKER_SECRET` | The separately provisioned `weave-worker` client credential |
| `WEAVE_WORKER_RELEASE_ID` | The admitted release ID |
| `WEAVE_EFFECT_URL` | The external receiver with durable idempotency support |

Pass only worker configuration. The example explicitly refuses database and
administrator environment variables. It obtains one access token per invocation
and stops claiming early enough to drain its declared 180-second tasks; a
supervisor can restart it with fresh credentials. It is not an indefinitely
refreshing token client.

## 4. Observe one task through completion

Start a workflow that calls the published Action. Inspect its run history and
worker status through the API. You should see a task claim, then a completion
receipt, followed by the workflow's next step. No claim may simply mean no
compatible work exists; check the Action identity, activation release pins, and
current grants before changing the handler.

![Worker claim, external effect and fenced completion](../diagrams/worker-recovery.svg)

Follow the external effect separately from the completion receipt. A crash between
them explains why the target needs the same operation key after recovery.
[Open diagram at full size](../diagrams/worker-recovery.svg)

See [worker protocol](../reference/worker-protocol.md) for release admission,
claims, capacity, fencing, rejected classified outputs and current wire models.
Native connectors use this same task/lease model; they do not grant themselves
access to arbitrary connection secrets or destinations.

## Containers and recovery

The [deployment guide](../operations/deployment.md) walks through packaging a
worker, building its image, provisioning its identity and grants, admitting its
release, and launching it with Compose. The worker uses a separate dependency
closure and receives API credentials rather than orchestration database access.
Server, Teams, and Kafka images use their own locked closures. API and native
executor processes receive separate private environment files.

At least one runtime replica must own recovery. Every runtime replica also needs
execute-only catalog authority for startup compatibility checks, even when its
background scheduler is disabled. This requirement does not apply to remote
worker-only processes.

A crash after an external effect and before the accepted completion can cause a
new lease and another invocation. Use supported provider idempotency or independent
reconciliation. Heartbeats and lease fencing do not roll back external effects;
cancellation may leave an operation in flight. Inspect the durable run and incident
state after a restart, and preserve the original operation identity when an
authorized safe retry is appropriate.
