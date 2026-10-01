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

# Start here: what to run, and why

**Goal:** choose one small task, complete it, and understand what you just ran.
You do not need to learn the entire platform before your first workflow.

## 1. Understand the four pieces

Think of an order approval process: receive an order, check it, ask for approval,
and notify the customer. Weave remembers where the process is while your systems
do the work.

| Piece | Plain-language meaning | Order example |
| --- | --- | --- |
| Workflow | A versioned description of the steps and their input/output | Check order → wait for approval → send notification |
| Platform API | The running service that saves definitions, starts runs, and reports progress | Your product asks it to start order 123 |
| Worker or native executor | Code that performs an external action | Call your warehouse or notification service |
| PostgreSQL | The platform's durable memory | Keep order 123 waiting even when the API restarts |

Keycloak supplies identities and access tokens. Weave's grants decide what each
identity may do. The CLI and Python SDK are clients of the API. Installing a
client does not start the API, just as installing a database client does not
start a database server.

![Clients, platform, durable state, and integration code](../diagrams/platform-in-plain-english.svg)

## 2. Choose one starting point

| I want to… | Follow this guide | What success looks like |
| --- | --- | --- |
| Try the language without services | [Install](../installation.md), then [first workflow](../quickstart.md) | The simulator returns your message |
| Run my own local platform | [Local platform in small steps](local-platform.md) | A real run is saved in PostgreSQL |
| Use my team's existing platform | [Connect to an API](connect-to-api.md) | Your client can call the authorized API |
| Explore requests in a browser | [Try the API](api-playground.md) | Swagger shows a response from your instance |
| Integrate Weave into my Python product | [Python SDK tutorial](sdk-tutorial.md) | Your code publishes and runs a workflow |
| Connect an external system | [Custom inbound and outbound connectors](custom-connectors-tutorial.md) | An incoming event starts work or a task calls your code |
| Run Weave on remote infrastructure | [Remote deployment, one stage at a time](../operations/remote-deployment.md) | A reachable API with persistent state and verified access |

Pick the first row if you are exploring. Pick the second if your goal is a
running service. You can skip the offline tutorial when you already understand
workflow definitions.

## 3. Learn the lifecycle with one example

1. **Write:** describe the process in YAML, JSON, or a Python workflow builder.
2. **Validate:** catch invalid fields, expressions, and missing dependencies.
3. **Simulate:** use sample input and mocked external results on your laptop.
4. **Publish:** send an immutable definition version to the API.
5. **Activate:** select that version and its dependencies for an environment.
6. **Run:** supply one input. The API returns the ID of this execution.
7. **Inspect:** read its current state, output, history, or incident.

Editing your YAML changes a file. Publishing saves a new version. Starting a run
creates one execution of an activated version. Those are three separate actions.
The [CLI tutorial](cli-tutorial.md) teaches each request and its response.

## 4. Know where a command runs

The tutorials label the terminal and working directory. Keep using the same
shell while following a numbered sequence, because shell variables belong to
that shell. A line beginning with `#` is a comment explaining why the next
command is present; you can paste comments along with commands.

```sh
# Ask the installed client for help; this does not start any services.
weave --help

# Find the platform guide without searching through the command reference.
weave docs platform
```

A command that keeps printing API logs is still running. Leave that terminal
open and use the second terminal for client commands. Ctrl-C stops the foreground
API. The local platform guide explains how to stop its dependencies while
keeping the saved data.

## 5. Understand “local cluster” and “remote deployment”

The local developer setup is a **Compose-backed stack**: PostgreSQL and Keycloak
in containers, plus the Weave API on your laptop. It is enough to develop and test
real workflows. It is not a Kubernetes cluster or a high-availability test.

For remote operation, Kubernetes can run API and worker containers. A registry
stores those images; PostgreSQL stores the business state; an identity provider
issues tokens. The [remote guide](../operations/remote-deployment.md) explains
these terms and gives an ordered route through AWS, Azure, or Google Cloud.
A remote worker only needs a reachable API and its scoped credentials; it does
not need direct access to the platform database.

## When something goes wrong

Read the first failing step and its expected result before continuing. A
compiler diagnostic means the definition needs attention. HTTP 401 usually
points to authentication; HTTP 403 points to permission. A queued task may mean
its worker is not running or not admitted. The
[troubleshooting guide](../operations/troubleshooting.md) connects these symptoms
to checks you can perform.

For more detail after your first successful run, read the
[architecture walkthrough](../architecture.md), [API contract](../reference/api.md),
and [configuration reference](../operations/configuration.md).
