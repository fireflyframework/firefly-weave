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

# Documentation

Firefly Weave provides typed workflow authoring and durable integration execution.
Start with [capabilities and verification](capabilities.md) for the current alpha
scope, then choose a reader path. [Architecture](architecture.md) shows the source
boundaries and durable state model.

| Reader | Start here | Continue with |
| --- | --- | --- |
| Workflow author | [Quickstart](quickstart.md), [concepts](concepts.md), [authoring guide](guides/workflow-authoring.md) | Compiler/schema, simulation, versioning and incidents |
| Host-product integrator | [Host integration](guides/host-integration.md), [embedding](reference/embedding.md) | SDK, API, CLI and native OpenAPI |
| Worker/connector developer | [Workers](guides/workers.md), [connector authoring](connectors/authoring.md) | Worker protocol, connections and provider sources |
| Operator | [Standalone quickstart](guides/standalone.md), [deployment](operations/deployment.md), [configuration](operations/configuration.md), [identity and secrets](operations/identity-and-secrets.md) | Provider setup, schedules, history and troubleshooting |

## Contracts and execution

- [Compiler](reference/compiler.md): bounded parsing, diagnostics, partial/complete compilation and artifacts.
- [Schema profile](reference/schema-profile.md): supported schemas, expressions and secret classification.
- [Contracts](contracts.md): language, versions and public boundaries.
- [API](reference/api.md): scoped requests, responses and operation families.
- [SDK](reference/sdk.md): typed clients, transport errors and authentication storage.
- [CLI](reference/cli.md): offline and authenticated remote commands, outputs and exit codes.
- [Native OpenAPI](reference/native-openapi.md): native model-derived API export and request/response contracts.
- [Worker protocol](reference/worker-protocol.md): admission, leases, fencing and completion.
- [Schedules and timers](reference/schedules-and-timers.md): UTC schedules, waits and recovery.
- [Simulation](reference/simulation.md): effect-free mocks and debugging.
- [History and replay](reference/history-and-replay.md): redacted recorded evidence and completeness.
- [Incident operations](reference/incident-operations.md): inspect, resolve, retry and resume controls.

## Connectors and event delivery

- [HTTP and webhooks](reference/http-and-webhooks.md): bounded egress and signed ingress.
- [PostgreSQL](connectors/postgresql.md): SQL policy, parameters and connection setup.
- [Kafka](connectors/kafka.md): acknowledged publication and durable consumer sources.
- [Integration events](reference/integration-events.md): subscriptions, outbox delivery and attempts.
- [Provider sources](reference/provider-sources.md): authenticated inbox, receipts and dispatch authority.
- [Connector authoring](connectors/authoring.md): trusted package metadata, fixtures and conformance.
- [OpenAPI import](connectors/metadata-import.md): validate and generate connector definitions.
- [HTTP profiles](connectors/http-profiles.md): versioned request, authentication, and response contracts.
- [Teams](connectors/teams.md): personal bot text, stored references and revocation.
- [WhatsApp](connectors/whatsapp.md): Cloud API text/templates, ingress and status history.
- [Telegram](connectors/telegram.md): webhook text and source-local update receipts.

## Operations and contribution

[Configuration](operations/configuration.md), [identity and secrets](operations/identity-and-secrets.md),
[troubleshooting](operations/troubleshooting.md), [contributing](../CONTRIBUTING.md),
[security reporting](../SECURITY.md), [source attribution](contributing/source-documentation.md),
[visual assets](visual-assets.md), and [change summary](../CHANGELOG.md).

For running services, follow [deployment](operations/deployment.md),
[observability](operations/observability.md), [upgrades](operations/upgrades.md),
[retention](operations/retention.md), and [backup and restore](operations/backup-restore.md).
The [capability matrix](capabilities.md) records supported behavior and verification boundaries.
