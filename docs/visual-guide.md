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

# A visual guide to Weave

Use this page when a name or boundary is unfamiliar. Each picture also appears in
its owning tutorial or reference, beside the commands and contracts it explains.
The diagram is a map; the linked guide supplies the executable steps and limits.
Open the original SVG when your screen is too narrow to read its labels.

## Start with an execution you can understand

![Learning path from local CLI authoring to a real integration](diagrams/tutorial-route.svg)

Begin with [the quickstart](quickstart.md). Follow the message through its input
schema, transform, and output schema. [Start the API](guides/standalone.md), then
use [the CLI lab](guides/cli-tutorial.md) to publish, activate, start, and inspect
that workflow. A worker is needed when an Action performs external work; its
image, admission, process, and task lease are different resources.

## Choose the question you need to answer

| Your question | Read and try | Picture to follow |
| --- | --- | --- |
| How do I get a working CLI? | [Install the CLI](installation.md) | [Isolated installation](diagrams/cli-installation.svg) |
| How does my YAML become an execution? | [Core concepts](concepts.md), [CLI lab](guides/cli-tutorial.md) | [Definition, activation, and runs](diagrams/definition-lifecycle.svg) |
| Why is a definition invalid? | [Authoring lab](guides/workflow-authoring.md), [compiler](reference/compiler.md) | [Diagnostics and repair](diagrams/authoring-diagnostic-loop.svg) |
| What do schemas check? | [Definition contracts](contracts.md), [schema profile](reference/schema-profile.md) | [Definition and value boundaries](diagrams/authoring-schema-boundaries.svg) |
| How do I debug without calling a provider? | [Simulation](reference/simulation.md) | [Commands and virtual time](diagrams/authoring-simulation-controls.svg) |
| Which component runs inside my product? | [Host integration](guides/host-integration.md), [embedding](reference/embedding.md) | [Three execution boundaries](diagrams/authoring-execution-boundaries.svg) |
| How do I run the processes locally? | [Standalone setup](guides/standalone.md), [deployment](operations/deployment.md) | [Terminals and runtime ownership](diagrams/operations-topology.svg) |
| How do I move to a cloud account? | [Cloud overview](operations/cloud-deployment.md), [Kubernetes](operations/kubernetes.md) | [Artifact and runtime delivery](diagrams/cloud-deployment.svg) |
| Which credentials go into each process? | [Configuration](operations/configuration.md), [identity](operations/identity-and-secrets.md) | [Process authority](diagrams/operations-authority.svg) |
| What has actually been verified? | [Capabilities](capabilities.md) | [Separate evidence categories](diagrams/capability-evidence.svg) |

For connector, messaging, schedule, recovery, and maintenance questions, select
the relevant figure from the catalog below. Each owning page explains how to
read its arrows and where a receipt, provider ACK, or workflow outcome differs.

## How to read the diagrams

- A box names a resource, process, or explicit decision. Read the caption to learn
  which of those meanings applies in that picture.
- An arrow labels an actual data flow, dependency, or lifecycle transition; it
  does not imply one transaction spans multiple external systems.
- Lanes separate scopes, process owners, or trust boundaries. Credentials belong
  only on the side that needs their authority.
- A checkpoint names the evidence to inspect before continuing. Provider
  acceptance, durable admission, and successful execution are separate checkpoints.
- Selected ER views show database relationships. They are not a complete schema
  export or permission to delete referenced records.

## Diagram catalog

All figures are editable SVGs with accessible text descriptions. The source is
kept in the repository; image-generation services and external fonts are not
needed to view or edit them.

| Topic | Open the figure |
| --- | --- |
| Read the result before moving forward | [authoring diagnostic loop](diagrams/authoring-diagnostic-loop.svg) |
| Choose where your host meets Weave | [authoring execution boundaries](diagrams/authoring-execution-boundaries.svg) |
| Carry returned identities into the next request | [authoring host sequence](diagrams/authoring-host-sequence.svg) |
| Two OpenAPI directions, two different products | [authoring openapi directions](diagrams/authoring-openapi-directions.svg) |
| A schema governs values at each boundary | [authoring schema boundaries](diagrams/authoring-schema-boundaries.svg) |
| Step execution and move time separately | [authoring simulation controls](diagrams/authoring-simulation-controls.svg) |
| Admission and registration precede task authority | [authoring worker lifecycle](diagrams/authoring-worker-lifecycle.svg) |
| Read evidence without overclaiming | [capability evidence](diagrams/capability-evidence.svg) |
| Install once, use Weave anywhere | [CLI installation](diagrams/cli-installation.svg) |
| Move the same application into a cloud environment | [cloud deployment](diagrams/cloud-deployment.svg) |
| Firefly Weave compiler and execution lifecycle | [compiler execution](diagrams/compiler-execution.svg) |
| Definition and connection relationships | [data model definitions](diagrams/data-model-definitions.svg) |
| Integration, provider and status relationships | [data model integrations](diagrams/data-model-integrations.svg) |
| Runtime, leases and completion evidence | [data model runtime](diagrams/data-model-runtime.svg) |
| One definition, many executions | [definition lifecycle](diagrams/definition-lifecycle.svg) |
| Document a behavior that readers can verify | [documentation contribution](diagrams/documentation-contribution.svg) |
| Follow the message through echo | [echo data flow](diagrams/echo-data-flow.svg) |
| Ingress and outbound integration delivery | [integration delivery](diagrams/integration-delivery.svg) |
| From installed code to an admitted execution | [integrations admission](diagrams/integrations-admission.svg) |
| Three integration paths and their commit boundaries | [integrations directions](diagrams/integrations-directions.svg) |
| HTTP profile v2: fixed policy, bounded invocation | [integrations http profile](diagrams/integrations-http-profile.svg) |
| Inspect evidence, then choose an authorized action | [integrations incidents replay](diagrams/integrations-incidents-replay.svg) |
| Kafka consumption: receipt first, offset second | [integrations kafka receipts](diagrams/integrations-kafka-receipts.svg) |
| Messaging providers retain different durable facts | [integrations messaging](diagrams/integrations-messaging.svg) |
| OpenAPI import: review before deployment | [integrations openapi review](diagrams/integrations-openapi-review.svg) |
| PostgreSQL: one invocation, one external transaction | [integrations sql transaction](diagrams/integrations-sql-transaction.svg) |
| Calendar starts, duration waits, and signals | [integrations time and signals](diagrams/integrations-time-and-signals.svg) |
| Give each process only its own authority | [operations authority](diagrams/operations-authority.svg) |
| Find the boundary, then choose the evidence | [operations evidence](diagrams/operations-evidence.svg) |
| Restore into a new target; keep the source fenced | [operations restore](diagrams/operations-restore.svg) |
| Retention is a reviewed, bounded transaction | [operations retention](diagrams/operations-retention.svg) |
| One installation, separate processes | [operations topology](diagrams/operations-topology.svg) |
| Three acceptance gates for an upgrade | [operations upgrade](diagrams/operations-upgrade.svg) |
| Identity, local authority and secret boundaries | [security boundaries](diagrams/security-boundaries.svg) |
| Firefly Weave system context and components | [system context](diagrams/system-context.svg) |
| Your path from YAML to a working integration | [tutorial route](diagrams/tutorial-route.svg) |
| Worker execution and the crash window | [worker recovery](diagrams/worker-recovery.svg) |

## Examples, operations, and project documentation

The [documentation home](README.md) links all guide and reference families. Also
read the [WhatsApp fixture walkthrough](../examples/connectors/whatsapp/README.md),
[trusted connector fixture](../tests/fixtures/e2-provider/README.md), and
[Kubernetes template inventory](../deploy/kubernetes/README.md) when using those
files. They distinguish offline fixture evidence from a live deployment.

For contributors, [source documentation](contributing/source-documentation.md)
explains attribution and validation, and [visual assets](visual-assets.md) explains
rendering and visual review. [Security reporting](../SECURITY.md) and the
[changelog](../CHANGELOG.md) retain their policy/history roles; they are not
workflow tutorials.
