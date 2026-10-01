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
| Who authors, executes, and operates a business process? | [Roles and lifecycle](guides/roles-and-lifecycle.md) | [The platform in plain English](diagrams/platform-in-plain-english.svg) |
| What is the difference between a worker and an operator? | [Worker walkthrough](guides/workers.md) | [People and execution processes](diagrams/worker-and-operator-roles.svg) |
| Can I use my own identity provider? | [OIDC/CIAM setup](operations/identity-and-secrets.md#use-your-own-identity-provider) | [Provider trust and local grants](diagrams/identity-provider-setup.svg) |
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

## Draw your own workflow

Use [the graph tutorial](guides/workflow-graphs.md) to export your compiled
workflow as text, Mermaid, or SVG. The drawings in this catalog explain platform
behavior; a generated workflow graph explains your own definition.

[The platform in plain English](diagrams/platform-in-plain-english.svg) is the
best first diagram if you are learning how the pieces fit together.

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
| Fix one boundary at a time | [authoring diagnostic loop](diagrams/authoring-diagnostic-loop.svg) |
| Choose where your application meets Weave | [authoring execution boundaries](diagrams/authoring-execution-boundaries.svg) |
| Carry each returned ID into the next call | [authoring host sequence](diagrams/authoring-host-sequence.svg) |
| Whose API are you describing? | [authoring openapi directions](diagrams/authoring-openapi-directions.svg) |
| Check the object you receive and return | [authoring schema boundaries](diagrams/authoring-schema-boundaries.svg) |
| Move execution and time separately | [authoring simulation controls](diagrams/authoring-simulation-controls.svg) |
| Give a worker permission for one attempt | [authoring worker lifecycle](diagrams/authoring-worker-lifecycle.svg) |
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
| Turn installed code into an executable workflow | [integrations admission](diagrams/integrations-admission.svg) |
| Follow the event in the direction it travels | [integrations directions](diagrams/integrations-directions.svg) |
| Keep the HTTP operation fixed; fill in values | [integrations http profile](diagrams/integrations-http-profile.svg) |
| Read evidence before changing execution | [integrations incidents replay](diagrams/integrations-incidents-replay.svg) |
| Save the receipt before committing the offset | [integrations kafka receipts](diagrams/integrations-kafka-receipts.svg) |
| Keep each messaging provider’s identity intact | [integrations messaging](diagrams/integrations-messaging.svg) |
| Review an imported API before you deploy it | [integrations openapi review](diagrams/integrations-openapi-review.svg) |
| Keep one SQL invocation in one transaction | [integrations sql transaction](diagrams/integrations-sql-transaction.svg) |
| A schedule starts a run; a wait stays in one | [integrations time and signals](diagrams/integrations-time-and-signals.svg) |
| Give each process only the authority it needs | [operations authority](diagrams/operations-authority.svg) |
| Find the failing boundary before retrying | [operations evidence](diagrams/operations-evidence.svg) |
| Restore to a new target; keep the source fenced | [operations restore](diagrams/operations-restore.svg) |
| Review a bounded deletion plan before applying it | [operations retention](diagrams/operations-retention.svg) |
| One installation, separate running processes | [operations topology](diagrams/operations-topology.svg) |
| Pass all three gates before reopening writers | [operations upgrade](diagrams/operations-upgrade.svg) |
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
