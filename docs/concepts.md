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

# Core concepts

A definition describes typed behavior. An **Action** describes a callable contract;
a **Connector** describes external integration capabilities; a **Workflow** composes
steps with schemas, expressions and control flow. The compiler validates data and
structure; it does not turn arbitrary Python code into a workflow.

| Concept | Meaning | Consequence |
| --- | --- | --- |
| Draft and revision | Editable authoring state with explicit revision checks | A stale edit must be reconciled rather than silently replacing newer work |
| Catalog lock | Immutable Action/Connector contracts used during compilation | Complete compilation needs the exact dependencies |
| Artifact | Versioned IR, executable identity, diagnostics and source mapping | Equivalent executable content has a canonical identity; source context remains distinct |
| Published version | Immutable definition admitted by the definition service | Editing a draft does not mutate an existing version |
| Activation | Scoped selection of an artifact, connections and worker releases | Static validity is followed by authority/resource admission |
| Run | Durable execution pinned to its activation/artifact | Deploying a newer revision does not silently change an existing run |
| Task and lease | Work intent and a temporary, generation-fenced worker claim | A late completion cannot acquire authority from an expired lease |
| Wait, signal and schedule | Durable external/time-based continuation points | Restart does not turn a wait into an in-memory timer |
| Receipt | Evidence of an accepted operation under a defined identity | Deduplication is scoped; it does not guarantee every external effect occurred once |

Tenant, project, and environment identify scope. A caller also needs a verified
principal and the required local grants. A URL, scope object, provider role, or
successful compilation is not authorization. Transactions apply tenant context
for PostgreSQL RLS; services still enforce the finer project/environment rules.

Partial validation checks what is possible without the catalog and produces no
executable. Complete compilation checks dependencies, expressions, schemas and
budgets. Publication and activation add durable ownership and execution admission.

Execution is at least once. A worker can finish an external request and crash
before its completion is recorded. Lease fencing protects accepted state, but
cannot undo a provider's action. Use supported idempotency keys or reconciliation
for uncertain external outcomes. Cancellation can leave an effect in flight.

Simulation executes with explicit mocks. Recorded replay checks retained,
redacted evidence without calling providers and can report incomplete history.
Neither is a substitute for a live-provider test.

Continue with [workflow authoring](guides/workflow-authoring.md),
[architecture](architecture.md), and the [capability matrix](capabilities.md).
