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

Use this page when a name or a boundary is unfamiliar, or when you want the
picture behind a guide. Every diagram also appears in the guide that explains
it, next to the commands and limits. A diagram is a map; the guide gives the
steps. Open the SVG on its own when your screen is too narrow to read the labels.

## Start with the learning route

![Learning route from a local simulation to a running platform and an integration](diagrams/tutorial-route.svg)

Begin with [the quickstart](quickstart.md) and simulate the first workflow on
your computer (1). [Start a local platform](guides/local-platform.md) and sign
in (2), then use [the CLI tutorial](guides/cli-tutorial.md) to publish,
activate, start, and inspect a run. Next, call another system (3), with
[a REST API without code](connectors/http-without-code.md) or your own connector
or worker, or connect your product (4). [Start here](guides/learning-path.md)
orders the guides for each role.
[Open diagram at full size](diagrams/tutorial-route.svg)

## Choose the question you need to answer

| Your question | Read and try | Picture to follow |
| --- | --- | --- |
| What runs when I use Weave? | [Start here](guides/learning-path.md), [roles and lifecycle](guides/roles-and-lifecycle.md) | [What runs when you use Weave](diagrams/platform-in-plain-english.svg) |
| How does my YAML become a run? | [Concepts](concepts.md), [CLI tutorial](guides/cli-tutorial.md) | [Definition, activation, and runs](diagrams/definition-lifecycle.svg) |
| How do I get a working CLI? | [Install the CLI](installation.md) | [Isolated installation](diagrams/cli-installation.svg) |
| How do Studio and the CLI connect and sign in? | [Connect the CLI to a platform](guides/connect-to-api.md), [Studio](guides/studio.md#connect-to-a-platform) | [Server address to saved platform](diagrams/connect-and-sign-in.svg) |
| Who prepares sign-in, and who grants access? | [Identity setup](operations/identity-and-secrets.md#use-your-own-identity-provider), [people and access](guides/people-and-access.md) | [Administrator, person, pairing, and authorization](diagrams/sign-in-roles.svg) |
| Can I use my own identity provider? | [Identity setup](operations/identity-and-secrets.md#use-your-own-identity-provider) | [Provider trust and local grants](diagrams/identity-provider-setup.svg) |
| What does Studio do, and what does the platform do? | [Studio](guides/studio.md) | [Studio and the platform](diagrams/studio-and-runtime.svg) |
| What does a Call an action step actually call? | [Step reference](guides/studio-step-reference.md) | [Step, Action, and implementation](diagrams/studio-integration-call.svg) |
| Can I call a REST API without writing a connector? | [Call a REST API without code](connectors/http-without-code.md), [HTTP profile reference](connectors/http-profiles.md) | [Describe, allow, pin, and run](diagrams/no-code-integration.svg) |
| Why is a definition invalid? | [Authoring lab](guides/workflow-authoring.md), [compiler](reference/compiler.md) | [Diagnostics and repair](diagrams/authoring-diagnostic-loop.svg) |
| What do schemas check? | [Definition contracts](contracts.md), [schema profile](reference/schema-profile.md) | [Definition and value boundaries](diagrams/authoring-schema-boundaries.svg) |
| How do I debug without calling a provider? | [Simulation](reference/simulation.md) | [Commands and virtual time](diagrams/authoring-simulation-controls.svg) |
| What is the difference between a worker and an operator? | [Worker walkthrough](guides/workers.md) | [People and execution processes](diagrams/worker-and-operator-roles.svg) |
| Which component runs inside my product? | [Host integration](guides/host-integration.md), [embedding](reference/embedding.md) | [Three execution boundaries](diagrams/authoring-execution-boundaries.svg) |
| How do I find and manage the runs of one case? | [Manage runs and business cases](guides/execution-management.md) | [Business keys, archive, and purge](diagrams/execution-lifecycle.svg) |
| What do I check before resolving an incident? | [Incident operations](reference/incident-operations.md) | [Replay versus authorized change](diagrams/integrations-incidents-replay.svg) |
| How do I run the processes locally? | [Manual local setup](guides/standalone.md), [worker deployment](operations/deployment.md) | [Terminals and runtime ownership](diagrams/operations-topology.svg) |
| How do I move to a cloud account? | [Cloud overview](operations/cloud-deployment.md), [Kubernetes](operations/kubernetes.md) | [Artifact and runtime delivery](diagrams/cloud-deployment.svg) |
| Which credentials go into each process? | [Configuration](operations/configuration.md), [identity](operations/identity-and-secrets.md) | [Process authority](diagrams/operations-authority.svg) |
| What has actually been verified? | [Capabilities](capabilities.md) | [Separate evidence categories](diagrams/capability-evidence.svg) |

## Draw your own workflow

Use [the graph tutorial](guides/workflow-graphs.md) to export your compiled
workflow as text, Mermaid, or SVG. The diagrams on this page explain how the
platform behaves; a generated workflow graph explains your own definition.

## How to read the diagrams

- **Numbers give the reading order.** Follow them from 1 upward; a caption under
  each diagram in its guide explains where to start.
- **A box** names a resource, a process, or a decision. The caption says which.
- **A solid arrow** is a request, a data flow, or a lifecycle step. **A dashed
  arrow** is usually the answer coming back. An arrow never means that one
  transaction spans several systems.
- **Columns and lanes** separate who owns a step: you, an administrator, the
  platform, or an external system. Credentials appear only on the side that
  needs them.
- **A shaded note** gives a rule or a boundary that applies to the whole picture.
  **Lumi's takeaway**, at the bottom, is the one idea to remember.
- **Database diagrams** show selected relationships, not a complete schema or
  permission to delete referenced records.

## Diagram catalog

Every diagram is a hand-written SVG with an accessible text description, kept in
the repository. You need no external fonts or services to view or edit one;
[visual assets](visual-assets.md) explains how to change and check them. The
last column lists every page that embeds or links the diagram.

### Start here

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Your learning route](diagrams/tutorial-route.svg) | Simulate locally, run a platform and sign in, then add an integration or connect your product | [Project README](../README.md) |
| [What runs when you use Weave](diagrams/platform-in-plain-english.svg) | Clients, the API, PostgreSQL, and the integration code that calls your systems, plus the local simulation shortcut | [Documentation home](README.md), [Architecture: follow one run through Weave](architecture.md), [Start here: choose your path](guides/learning-path.md), [Who does what, from a business process to a completed run](guides/roles-and-lifecycle.md) |
| [How your product connects to Weave](diagrams/system-context.svg) | Your product, the offline compiler, and the identity provider feed the API, which uses PostgreSQL, connectors, and workers | [Project README](../README.md), [Architecture: follow one run through Weave](architecture.md), [Use the Weave logo, Lumi, and diagrams](visual-assets.md) |
| [Write once, run with many inputs](diagrams/definition-lifecycle.svg) | Source, published version, activation in an environment, and the separate runs it starts | [Understand Weave through one example](concepts.md), [Publish, activate, and run a workflow from the CLI](guides/cli-tutorial.md), [Who does what, from a business process to a completed run](guides/roles-and-lifecycle.md) |
| [Follow the message through echo](diagrams/echo-data-flow.svg) | The first workflow: input schema, transform, and output schema, simulated locally or run on a platform | [Write and simulate your first workflow](quickstart.md) |
| [Install the CLI without disturbing your apps](diagrams/cli-installation.svg) | Verify the release, install it into an isolated environment, and put only `weave` on your PATH | [Install the Weave CLI](installation.md) |
| [Read evidence without overclaiming](diagrams/capability-evidence.svg) | Implemented, checked locally, and verified with a live provider are separate questions | [Capabilities and verification](capabilities.md) |

### Connect, sign in, and give access

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Connect, sign in, and choose a workspace](diagrams/connect-and-sign-in.svg) | Server address, public sign-in settings, discovery, review, sign-in, credential store, and saved platform, the same for the CLI and Studio | [Architecture: follow one run through Weave](architecture.md), [Connect the CLI to a platform](guides/connect-to-api.md), [Install and use the Studio desktop app](guides/desktop.md), [Design and run workflows in Studio](guides/studio.md) |
| [Who does what when people connect](diagrams/sign-in-roles.svg) | What an administrator prepares once, what each person does on their computer, and why pairing is not a sign-in | [Connect the CLI to a platform](guides/connect-to-api.md), [Give people the right access](guides/people-and-access.md), [Design and run workflows in Studio](guides/studio.md), [Set up identity, sign-in, and secrets](operations/identity-and-secrets.md) |
| [Bring your own identity provider](diagrams/identity-provider-setup.svg) | The six one-time identity setup steps, and the checks Weave makes on every request | [Set up identity, sign-in, and secrets](operations/identity-and-secrets.md) |
| [Who may do what, and where](diagrams/security-boundaries.svg) | Who is calling, whether their grant allows this in this workspace, and which data the transaction may touch; secrets resolve only for authorized work | [Security policy](../SECURITY.md), [Architecture: follow one run through Weave](architecture.md), [Set up identity, sign-in, and secrets](operations/identity-and-secrets.md) |

### Design workflows in Studio

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Studio edits; the platform runs](diagrams/studio-and-runtime.svg) | The Studio page, its paired local host, and the platform API that stores and runs workflows | [Architecture: follow one run through Weave](architecture.md), [Design and run workflows in Studio](guides/studio.md) |
| [What Call an action calls](diagrams/studio-integration-call.svg) | A step selects an Action version and a connection slot; the Action pins the connector or worker that runs it | [Choose and configure a workflow step](guides/studio-step-reference.md) |

### Author, check, and simulate

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Fix one boundary at a time](diagrams/authoring-diagnostic-loop.svg) | Validate, compile with a catalog, read the diagnostic's code and path, fix, and check again | [Author, check, and simulate a workflow](guides/workflow-authoring.md), [CLI reference](reference/cli.md), [Compile workflows and read the results](reference/compiler.md) |
| [Check the object you receive and return](diagrams/authoring-schema-boundaries.svg) | Which values the input and output schemas accept or reject, using the echo workflow | [Read and write definition contracts](contracts.md), [Write schemas that Weave accepts](reference/schema-profile.md) |
| [Move execution and time separately](diagrams/authoring-simulation-controls.svg) | The simulator's commands: inspect, step, continue, deliver a signal or decision, and advance virtual time | [Simulate a workflow run without side effects](reference/simulation.md) |
| [From a written workflow to durable work](diagrams/compiler-execution.svg) | Compile without services or secrets, then publish, activate, and execute on the platform | [Architecture: follow one run through Weave](architecture.md) |

### Call APIs and other systems

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Call a REST API without code](diagrams/no-code-integration.svg) | Describe or import a request, allow it in your environment, pin it at activation, and run it inside the egress boundary | [Call a REST API without code](connectors/http-without-code.md), [Design and run workflows in Studio](guides/studio.md) |
| [Keep the HTTP operation fixed](diagrams/integrations-http-profile.svg) | How the connection, the Action's HTTP profile, and the workflow's values combine into one request | [HTTP profile v2 reference](connectors/http-profiles.md), [Call a REST API without code](connectors/http-without-code.md) |
| [Review an imported API](diagrams/integrations-openapi-review.svg) | Inventory, policy, offline check, and review of an OpenAPI import before the usual publishing gates | [Import an OpenAPI document as a connector package](connectors/metadata-import.md) |
| [Whose API are you describing?](diagrams/authoring-openapi-directions.svg) | Exporting Weave's own OpenAPI document versus importing another system's API | [Export Weave's OpenAPI document](reference/native-openapi.md) |
| [Turn installed code into an executable workflow](diagrams/integrations-admission.svg) | Install a connector package, publish its descriptor, admit a release, grant it, and activate a workflow | [Author a trusted connector package](connectors/authoring.md), [Build a custom integration, one boundary at a time](guides/custom-connectors-tutorial.md) |
| [Follow the event in the direction it travels](diagrams/integrations-directions.svg) | Signed webhooks, provider sources, and outbound event deliveries, each with its own receipt | [Build a custom integration, one boundary at a time](guides/custom-connectors-tutorial.md), [Call HTTP APIs and accept signed webhooks](reference/http-and-webhooks.md), [Notify your application about workflow events](reference/integration-events.md), [Receive messaging events with provider sources](reference/provider-sources.md) |
| [Events come in; delivery attempts go out](diagrams/integration-delivery.svg) | Incoming events become receipts and runs; outgoing events become delivery attempts | [Architecture: follow one run through Weave](architecture.md) |
| [Save the receipt before committing the offset](diagrams/integrations-kafka-receipts.svg) | How a Kafka trigger records a receipt and starts a run before it commits the offset | [Publish to Kafka and start workflows from Kafka records](connectors/kafka.md) |
| [Keep each messaging provider's identity](diagrams/integrations-messaging.svg) | What Teams, WhatsApp, and Telegram store for replies and status | [Receive and reply to Microsoft Teams messages](connectors/teams.md), [Start a workflow from a Telegram message and reply](connectors/telegram.md), [Send and receive WhatsApp messages](connectors/whatsapp.md) |
| [Keep one SQL invocation in one transaction](diagrams/integrations-sql-transaction.svg) | A reviewed PostgreSQL operation, its connection, and the single transaction each call runs in | [Read and write PostgreSQL from a workflow](connectors/postgresql.md) |
| [A schedule starts a run; a wait stays in one](diagrams/integrations-time-and-signals.svg) | Schedules create new runs; timers and signals pause inside one run | [Start runs on a schedule and pause with timers](reference/schedules-and-timers.md) |

### Embed Weave and run workers

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Choose where your application meets Weave](diagrams/authoring-execution-boundaries.svg) | Build and simulate locally, call a running API, or embed trusted server services, and what each one starts | [Integrate Weave into a host product](guides/host-integration.md), [Embed authoring and orchestration in your product](reference/embedding.md) |
| [Carry each returned ID into the next call](diagrams/authoring-host-sequence.svg) | The calls a host product makes, from draft to publish, activate, start, and read, and the IDs each returns | [Integrate Weave into a host product](guides/host-integration.md), [Use the HTTP API](reference/api.md), [CLI reference](reference/cli.md), [Python SDK](reference/sdk.md) |
| [Give a worker permission for one attempt](diagrams/authoring-worker-lifecycle.svg) | Admit a release, register the worker, claim a task with a lease, and complete it | [Implement and operate a worker](guides/workers.md), [Remote worker protocol](reference/worker-protocol.md) |
| [People and execution processes](diagrams/worker-and-operator-roles.svg) | Authors, deployers, and operators decide; the engine records progress; workers and native executors do the work | [Implement and operate a worker](guides/workers.md) |
| [A worker can crash after the remote action](diagrams/worker-recovery.svg) | Claim, lease, completion, and what happens when a worker stops after the external system already acted | [Architecture: follow one run through Weave](architecture.md), [Implement and operate a worker](guides/workers.md) |

### Operate runs and the platform

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Find the case, choose the run, manage its history](diagrams/execution-lifecycle.svg) | Business keys group runs; a finished run can be kept, archived, restored, or purged | [Manage runs and business cases](guides/execution-management.md) |
| [Read evidence before changing execution](diagrams/integrations-incidents-replay.svg) | Read-only replay versus the authorized changes: resolving an incident and retrying a whole run | [Inspect run history and replay it offline](reference/history-and-replay.md), [Resolve incidents, cancel runs, and retry them](reference/incident-operations.md) |
| [One installation, separate processes](diagrams/operations-topology.svg) | The terminals and processes of a local installation: API, PostgreSQL, Keycloak, and optional executors and workers | [Deploy, start, and use the platform](guides/platform-overview.md), [Set up a local platform manually](guides/standalone.md), [Deploy your first worker, step by step](operations/deployment.md), [Understand the local runtime: PostgreSQL, Keycloak, and the API](reference/local-runtime.md) |
| [Give each process only the authority it needs](diagrams/operations-authority.svg) | Which credentials the migration job, the API, the scheduler, and workers receive | [Configure the server and its clients](operations/configuration.md), [Set up identity, sign-in, and secrets](operations/identity-and-secrets.md) |
| [From a build to a running cloud service](diagrams/cloud-deployment.svg) | Build and push images, then run the API and workers on Kubernetes with your database and identity provider | [Prepare AWS EKS and ECR](operations/aws.md), [Prepare Azure AKS and ACR](operations/azure.md), [Deploy Weave on AWS, Azure, or Google Cloud](operations/cloud-deployment.md), [Prepare Google GKE and Artifact Registry](operations/gcp.md), [Deploy Weave on an existing Kubernetes cluster](operations/kubernetes.md), [Remote deployment, one stage at a time](operations/remote-deployment.md) |
| [Find the failing boundary before retrying](diagrams/operations-evidence.svg) | Five questions in order: dependencies, readiness, authorization, what Weave recorded, and what happened outside Weave | [Export metrics and traces](operations/observability.md), [Troubleshoot a Weave platform](operations/troubleshooting.md) |
| [Pass three gates before reopening writers](diagrams/operations-upgrade.svg) | Schema, retained requirements, and a real run, each with what to do when it fails | [Upgrade a platform safely](operations/upgrades.md) |
| [Restore to a new target](diagrams/operations-restore.svg) | Fence the source, capture evidence, restore into a new database, compare, and verify | [Back up and restore an owned local installation](operations/backup-restore.md) |
| [Review a deletion plan before applying it](diagrams/operations-retention.svg) | Plan, review, recheck, commit, and recover the removal of expired debug sessions | [Remove expired debug sessions with a reviewed plan](operations/retention.md) |

### Database relationships and contributing

| Diagram | What it shows | Where it is explained |
| --- | --- | --- |
| [Definition relationships](diagrams/data-model-definitions.svg) | Database relationships of published versions, activations, and connection bindings | [Architecture: follow one run through Weave](architecture.md) |
| [Runtime relationships](diagrams/data-model-runtime.svg) | Database relationships of runs, task intents, leases, and completion evidence | [Architecture: follow one run through Weave](architecture.md) |
| [Integration relationships](diagrams/data-model-integrations.svg) | Database relationships of integration events, provider receipts, and delivery facts | [Architecture: follow one run through Weave](architecture.md) |
| [Document a behavior readers can verify](diagrams/documentation-contribution.svg) | How contributors establish, explain, and check what a page claims | [Contributing](../CONTRIBUTING.md), [Document and attribute every source file](contributing/source-documentation.md) |

## Examples, operations, and project documentation

The [documentation home](README.md) links all guide and reference families. Also
read the [WhatsApp fixture walkthrough](../examples/connectors/whatsapp/README.md),
[trusted connector fixture](../tests/fixtures/e2-provider/README.md), and
[Kubernetes template inventory](../deploy/kubernetes/README.md) when you use those
files. They separate offline fixture evidence from a live deployment.

For contributors, [source documentation](contributing/source-documentation.md)
explains attribution and validation, and [visual assets](visual-assets.md)
explains rendering and visual review. [Security reporting](../SECURITY.md) and
the [changelog](../CHANGELOG.md) keep their policy and history roles; they are
not workflow tutorials.
