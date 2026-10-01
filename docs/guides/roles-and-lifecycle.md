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

# Who does what, from a business process to a completed run

Suppose your product approves customer orders. It needs to check a customer in
another system, wait for approval, and send a message. Those steps may finish in
seconds or span several days. **Business process management (BPM)** means making
that sequence explicit, knowing where each order is, and handling delays or
failures without losing the process.

In Weave, you describe the process as a **Workflow**. The runtime remembers its
progress. Workers or connector executors perform the external actions. People
configure and operate these components; a worker is software, not a human team
member assigned to watch the process.

## 1. Separate the process from the code that does a task

![A product request, permission checks, durable state, and external execution](../diagrams/platform-in-plain-english.svg)

Read the numbered boxes in order. Your product starts a run through the API.
Weave saves progress in PostgreSQL and coordinates the next step. When a step
needs an external action, an admitted executor performs it and reports the
outcome. The product can read progress later; it does not need to keep the
original HTTP request open.

| In the order example | Weave concept | What it does |
| --- | --- | --- |
| “Check the customer, wait for approval, then notify” | Workflow definition | Describes steps, data, and decisions in YAML, JSON, or Python-generated data |
| One specific customer's order | Run | Records one execution of a selected workflow activation |
| “Check this customer” | Action and task | The Action describes the reusable operation; the task records this particular work item |
| Python code that calls the customer service | Remote worker handler | Receives a task lease and returns the business result; the worker SDK manages claiming, heartbeats, and completion |
| A packaged HTTP or messaging integration | Connector and native executor | Calls a configured operation through the installed connector implementation |
| “Wait until an approval arrives” | Wait and signal | Keeps durable waiting state; your product submits the authorized signal |

There is no built-in human approval inbox or graphical process editor in this
alpha. Your product can provide an approval screen and send a signal to Weave.
The [definition contracts](../contracts.md) describe supported steps; the
[capability matrix](../capabilities.md) records the current limits.

## 2. Know which responsibilities belong to your team

These are responsibilities, not an instruction to create six teams. One person
may perform several during local development. Permissions still need to be
assigned deliberately at the appropriate scope.

| Responsibility | Typical work | Start here |
| --- | --- | --- |
| Workflow author | Define inputs, steps, conditions, and outputs; validate and simulate | [First workflow](../quickstart.md), then [authoring](workflow-authoring.md) |
| Product developer | Call the API, start runs, display progress, and submit signals from a product | [Python SDK tutorial](sdk-tutorial.md) or [API playground](api-playground.md) |
| Integration developer | Implement a worker handler or package an inbound/outbound integration | [Workers](workers.md) and [custom connectors](custom-connectors-tutorial.md) |
| Deployer | Admit implementation releases, configure connections, and activate exact versions | [Worker deployment](../operations/deployment.md) |
| Platform operator | Configure the API, database, identity, monitoring, backups, and upgrades | [Remote deployment](../operations/remote-deployment.md) and [configuration](../operations/configuration.md) |
| Run operator | Inspect run history, investigate incidents, and take authorized recovery actions | [Observability](../operations/observability.md) and [incident operations](../reference/incident-operations.md) |

An **operator** here means the person or automation responsible for running the
platform or managing its executions. It does not mean a Kubernetes Operator
controller. The deployment guides use explicit CLI, Compose, and Kubernetes
procedures; Weave does not ship a Kubernetes Operator in this release.

## 3. Follow a workflow through its lifecycle

![Definition, published version, environment activation, and individual runs](../diagrams/definition-lifecycle.svg)

1. **Write and check.** Author a definition, validate its structure and schemas,
   then simulate with mock results. No external calls happen in the simulator.
2. **Publish.** Save an immutable version through the API or CLI. Keep the
   returned version ID and digest.
3. **Prepare execution.** Make required implementations available and admitted,
   configure connection revisions, and provision the relevant scoped grants.
   A transform-only workflow does not need an external worker.
4. **Activate.** Bind the selected version and its execution dependencies to an
   environment. Keep the activation ID. Publishing alone does not start a run.
5. **Start.** Submit input for that activation. Each start creates a run with its
   own ID and progress; use a stable idempotency key for a deliberate retry of
   the same request.
6. **Observe and respond.** Read run state and history, send an authorized signal
   when a wait needs one, or investigate an incident when execution cannot proceed.

Follow [publish and run with the CLI](cli-tutorial.md) to execute this sequence.
The [Python tutorial](sdk-tutorial.md) covers the same lifecycle from application
code. The [API reference](../reference/api-explorer.md) lists request and response
schemas for the HTTP equivalent.

## 4. Understand what happens when a worker stops

The runtime stores task state in PostgreSQL. A remote worker claims a task for a
limited time, maintains its lease, and reports completion. A stopped worker does
not erase the workflow. Recovery can make eligible work available again according
to the runtime's lease and retry rules.

An external service may have accepted a request just before the worker stopped.
Repeating that request can repeat its effect. Design handlers to use the target
system's idempotency mechanism or an explicit reconciliation procedure. Weave's
lease fencing rejects stale completion reports; it cannot undo a payment or
message already accepted elsewhere.

The [worker walkthrough](workers.md) explains handler responsibilities. The
[worker protocol](../reference/worker-protocol.md) describes leases, receipts,
and the exact HTTP contract. Begin with those pages before adding more worker
replicas or assuming a failed request is safe to repeat.

## 5. Choose your first practical route

- **Learn the language:** install the CLI and complete the [offline quickstart](../quickstart.md).
- **Operate a working development platform:** complete [local platform setup](local-platform.md), including its saved demo run.
- **Integrate an existing platform:** ask its operator for your API URL, approved login, scope IDs, and grants; then [connect](connect-to-api.md).
- **Build a product integration:** make one request in [Swagger](api-playground.md), then continue with the [Python SDK](sdk-tutorial.md).
- **Execute custom logic:** read [workers](workers.md), then package and deploy the handler using the [deployment guide](../operations/deployment.md).

Each route builds on a visible checkpoint. Keep the IDs returned by the platform;
workflow names, client names, and arbitrary UUIDs are not substitutes for those
receipts or for permissions.
