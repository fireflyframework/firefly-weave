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

# Learn and use Firefly Weave

![Lumi, a firefly with folded woven wings](../assets/lumi.svg){ .lumi-guide }

Meet **Lumi**, your guide through Firefly Weave. Start small: create a workflow,
see how it runs, then connect it to your systems. [Meet the mascot](visual-assets.md#meet-lumi).

Weave remembers the steps of a business process and coordinates the systems that
do the work. Start with one working example, then add the pieces your product
needs.

**New to Weave? Read [Start here](guides/learning-path.md).** It explains the API,
workers, database, and workflow lifecycle in plain language.

## Understand the platform before choosing commands

Read [people, workers, and the workflow lifecycle](guides/roles-and-lifecycle.md)
for an order-approval example. It explains what BPM means here, what the runtime
coordinates, what a worker executes, and what platform and run operators manage.

The **API and SDKs** navigation tab contains the browser playground, the Python
tutorial, every HTTP operation and schema, and the SDK reference. The public
reference is browsable without an installation; Swagger on your running API lets
you execute authorized requests against that instance.

## Choose your next task

| Your goal | Start here | You will finish with… |
| --- | --- | --- |
| Try a workflow without running services | [Install the CLI](installation.md) → [first workflow](quickstart.md) | A successful local simulation |
| Start your own platform | [Local platform in small steps](guides/local-platform.md) | An API, database, identity service, and saved run |
| Connect to a platform your team runs | [Connect to an API](guides/connect-to-api.md) | An authenticated client with the right scope |
| Try API requests in a browser | [API playground](guides/api-playground.md) | Swagger connected to your instance |
| Add Weave to a Python product | [Python SDK tutorial](guides/sdk-tutorial.md) | A workflow published and run from Python |
| Build your own integration | [Inbound and outbound connectors](guides/custom-connectors-tutorial.md) | A custom action or incoming webhook workflow |
| Implement and run a task handler | [Worker walkthrough](guides/workers.md) → [worker deployment](operations/deployment.md) | An admitted handler that can claim and complete work |
| Use your organization's identity provider | [OIDC/CIAM setup](operations/identity-and-secrets.md#use-your-own-identity-provider) | Verified tokens, explicit identity links, and scoped grants |
| Draw a workflow | [Workflow graphs](guides/workflow-graphs.md) | Terminal, Mermaid, and SVG views |
| Deploy on remote infrastructure | [Remote deployment](operations/remote-deployment.md) | An ordered path through AWS, Azure, or Google Cloud |

## The first three things to understand

1. **Installing the CLI gives you a client.** It can edit, validate, compile, and
   simulate files without any services. It can also call a running API.
2. **Starting the platform gives you persistence.** The API uses PostgreSQL to
   save workflows and runs, and verifies identities supplied by your configured OIDC provider.
3. **Adding workers connects external systems.** A worker runs integration code
   and reports its result to the API. An internal transform needs no worker.

![How a request moves through clients, the API, storage, and workers](diagrams/platform-in-plain-english.svg)

## Read a tutorial one step at a time

Run one command block, check its expected result, then continue. Comments inside
code blocks explain why the commands are there. When a terminal is running API
logs, keep it open and use a second terminal for requests.

The current installation guide pins **v0.1.0a4 alpha**. This is a preview release.
[Capabilities and limits](capabilities.md) distinguishes local verification from
provider-account and cloud checks you must perform in your environment.

## Find an exact interface

| Interface | Reference |
| --- | --- |
| Every API operation, request, response, and schema | [Full API reference](reference/api-explorer.md) |
| Authentication, errors, revisions, and idempotency | [API behavior](reference/api.md) |
| CLI commands and file formats | [CLI reference](reference/cli.md) |
| Python clients and builders | [SDK reference](reference/sdk.md) |
| Workflow language and expressions | [Definition contracts](contracts.md) and [compiler](reference/compiler.md) |
| Worker execution | [Worker protocol](reference/worker-protocol.md) |
| Service settings | [Configuration](operations/configuration.md) |

## Go deeper after the first successful run

- [Architecture](architecture.md): follow one run and see which module owns each responsibility.
- [Visual guide](visual-guide.md): find the diagram for the question you are answering.
- [CLI lifecycle tutorial](guides/cli-tutorial.md): author, publish, activate, execute, and inspect.
- [Connector authoring](connectors/authoring.md): package and extend an integration.
- [Troubleshooting](operations/troubleshooting.md): work from a symptom to the right check.
- [Operate the platform](operations/deployment.md): workers and container execution in detail.
- [Contribute](../CONTRIBUTING.md), [security](../SECURITY.md), [changelog](../CHANGELOG.md), and [website maintenance](contributing/documentation-site.md).
