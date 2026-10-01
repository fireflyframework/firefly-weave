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

A worker executes admitted task intent. It is not a host administrator and does
not obtain database or secret authority simply by knowing a tenant/project path.
Start with [the worker example](../../examples/worker/main.py), its
[manifest](../../examples/worker/manifest.json), and the [worker SDK](../../src/firefly_weave/sdk/worker.py).

1. Implement the declared Action contract and validate inputs/outputs using its
   schemas. Keep credentials outside business payloads, logs and completion data.
2. Package the implementation and record its immutable build/release identity.
   An operator must admit that release and grant the worker its intended scope.
3. Configure only the API/token endpoints, scoped principal and exact release
   identity that worker needs. Do not pass the API's database or migration env file.
4. Claim tasks and maintain lease heartbeats through the SDK. Execute only while
   the current lease/admission permits it; preserve cancellation and deadline behavior.
5. Complete with the exact lease generation and bounded result. A stale completion
   is rejected even if the external provider already accepted its request.

![Worker claim, external effect and fenced completion](../diagrams/worker-recovery.svg)

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
