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

# Understand Weave through one example

Imagine a customer-onboarding feature in your product. It receives an application,
checks the customer in another system, waits for a reviewer, and sends a welcome
message. Your product supplies the request and displays the result. Weave keeps
track of the process, including work that has to wait or survive a restart.

You do not need every concept on this page for your first workflow. The
[quickstart](quickstart.md) begins with a single transform and no external systems.
Use this page as a companion when the later tutorials introduce more pieces.

![Immutable version and activation used by independent runs](diagrams/definition-lifecycle.svg)

Read downward from source to deployed selection, then outward to separate
executions. An activation is reusable; a run carries one invocation’s state.

[Open diagram at full size](diagrams/definition-lifecycle.svg)

## Definition, artifact, and run

A **definition** is the YAML or JSON document describing what should happen.
It is comparable to a recipe. A **run** is one execution with a particular input,
comparable to preparing that recipe for one order.

Between the two, the **compiler** checks the definition and produces an
**artifact**: a machine-readable representation with a digest identifying its
executable content. Invalid schemas, unresolved action versions, and incompatible
values should be found before you activate the workflow.

| Item | Onboarding example | When it changes |
| --- | --- | --- |
| Workflow definition | “Check customer, wait for approval, send welcome message” | When the author edits the process |
| Compiled artifact | The validated steps and exact dependency versions | When the executable definition or dependencies change |
| Published version | `customer-onboarding@1.0.0` stored in a project | Publication creates a new immutable version |
| Activation | Version 1.0.0 with the test environment's connections and workers | A new activation selects a deployment configuration |
| Run | Onboarding request for one customer | State advances as that customer's work completes |

Editing a YAML file does not change an existing run. Publish and activate a new
version for new work; running executions retain their original selections.

## The lifecycle, one step at a time

1. **Author:** write the input schema, steps, and output schema.
2. **Validate:** check the source. Without a catalog, Weave can perform only a
   partial check and produces no executable artifact.
3. **Compile:** provide the **catalog**, the exact action, connector, schema, and
   worker task contracts available to the definition. The compiler resolves those
   dependencies and produces an artifact.
4. **Publish:** store an immutable version in a project. The publication API checks
   the submitted definition; sending an arbitrary artifact is not a deployment shortcut.
5. **Activate:** select that version for an environment and supply its required
   connection and worker release bindings.
6. **Start a run:** provide the activation ID and an input object.
7. **Observe:** read status, results, events, and any incidents. A run may finish
   immediately, wait for a worker, or pause for a timer or signal.

In the offline tutorial you perform steps 1–3 and simulate execution. In the
[API tutorial](guides/standalone.md), you publish, activate, and start a real run.

An optional **draft** supports iterative editing before publication. Each edit has
a revision number. If two authors edit the same revision, the stale update must
be reconciled instead of silently overwriting the other person's change.

## Workflow, action, connector, and connection

These names describe different responsibilities:

| Concept | Responsibility | Example |
| --- | --- | --- |
| **Workflow** | Coordinates the process | Check a customer, wait for approval, then send a message |
| **Action** | Defines the input/output contract for one reusable operation | `check-customer@1.0.0` accepts a customer ID and returns an eligibility result |
| **Connector** | Describes supported operations for an integration adapter | An HTTP connector can expose a typed request operation |
| **Connection revision** | Supplies one environment's permitted configuration and credential references | The test customer-service URL and its approved token reference |
| **Worker release** | Describes the worker artifact and task contracts it can execute | A container image providing `check-customer@1.0.0` |
| **Worker process** | Actually claims and executes tasks | A running container making the customer-service request |

An action can be implemented by a remote worker or by a connector adapter. A
connection describes a configured destination, not the process that calls it.
Changing a connection creates another revision so existing activations can keep
their original binding.

A **transform** step only computes a value from workflow data. It does not require
an external worker, which is why the first tutorial can return a message without
building a worker image.

## Where your data lives

Weave organizes resources in a hierarchy:

```text
Tenant: your organization or customer boundary
└── Project: one product or group of related workflows
    ├── Published definitions: shared authoring versions
    ├── Environment: test
    │   └── Activations, connections, workers, and runs
    └── Environment: production
        └── Separately configured activations, connections, workers, and runs
```

The tuple of tenant, project, and environment IDs is called a **scope**. API URLs
include these IDs so requests identify the intended resources. Knowing the IDs
does not give a caller access to them.

PostgreSQL stores durable definitions, execution state, task leases, and receipts.
The remote worker talks to the API and does not need a database login. The
[architecture guide](architecture.md) explains how the modules cooperate.

## Identity and permission are different questions

**Authentication** asks “Who is calling?” Your configured identity provider issues a token; Weave checks
its signature, issuer, audience, and allowed client. An **identity link** maps that
verified identity to a local **principal**, the Weave user or application record.

**Authorization** asks “What may that principal do here?” Weave's local **grants**
assign roles at particular scopes. For example, a developer can work on project
definitions; a deployer can activate them in an environment; an operator can
control runs; a worker receives narrowly scoped task permissions.

An identity-provider role does not automatically become a Weave role. The tutorial makes
identity linking and grants explicit so the same model can be used with another
supported OIDC provider. See [identity and secrets](operations/identity-and-secrets.md)
for the concrete setup and scope model.

## Tasks, leases, and retries

When a workflow reaches an action, Weave records a **task**: work that needs to be
done. A worker **claims** it and receives a **lease**, temporary permission to
execute that task. Heartbeats can renew a valid lease. A completion carries the
lease proof so an old worker cannot complete a task after a newer claim has taken over.

This protects Weave's accepted state, but it cannot undo a request already sent
to another system. Consider this sequence:

1. The worker asks the customer service to create a record.
2. The customer service creates it.
3. The worker crashes before Weave receives the completion.

Weave cannot infer from the missing completion that no record was created. For
safe retries, the target system should recognize a stable **idempotency key** and
return the previous result instead of creating a duplicate. Otherwise the process
needs reconciliation or an operator decision. **At least once** describes this
possibility of repeated delivery; it does not promise exactly-once external effects.
The [worker guide](guides/workers.md) shows how a handler uses the operation key.

## Waiting and reacting to events

| Mechanism | Use it when… | Example |
| --- | --- | --- |
| Wait | The current run should resume after a duration | Wait ten minutes before another check |
| Signal | The current run needs an external decision or value | Receive a reviewer's approval |
| Schedule | New runs should start on a recurring timetable | Process yesterday's orders each morning |
| Webhook/provider source | An incoming event should start or signal work | Start a workflow when a supported message arrives |
| Integration event/outbox | A committed Weave event should notify another system | Send a completion notification to your product |

Waits and signals are stored with the run; restarting the API does not turn them
into forgotten in-memory timers. A **receipt** records acceptance under a defined
identity, allowing supported retries to recognize an already accepted operation.
Its deduplication scope matters: a receipt for one source is not a global promise
that every external event happened once.

## Debugging without confusing it with execution

**Simulation** evaluates a compiled workflow locally with explicit mock responses.
It does not call the customer service. **Recorded replay** inspects retained,
redacted run evidence without contacting providers and can report incomplete
history. An **incident** represents a condition that needs an operator's attention,
including some ambiguous external outcomes.

Use [authoring and simulation](guides/workflow-authoring.md) while developing,
[history](reference/history-and-replay.md) to inspect a real run, and
[incident operations](reference/incident-operations.md) when work needs intervention.
