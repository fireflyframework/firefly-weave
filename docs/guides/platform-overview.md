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

# Deploy, start, and use the platform

Installing `weave` gives you a command-line client. A running platform additionally
needs a Weave API, PostgreSQL, and configured identity. Start here to choose what
you need to run, then follow the linked tutorial for its exact commands.

## Choose your starting point

| Your situation | What you install or run | First guide |
| --- | --- | --- |
| I want to try a workflow on my laptop | CLI only; local files and simulation | [Offline quickstart](../quickstart.md) |
| My team already has a Weave API | CLI plus an API URL, login, scope IDs, and grants | [Connect to an API](connect-to-api.md) |
| I need my first complete local platform | Source checkout, isolated server environment, PostgreSQL, Keycloak, and foreground API | [Local platform setup](local-platform.md) |
| I need a worker to call an external system | The working local platform plus an admitted worker image and handler | [Worker and container deployment](../operations/deployment.md) |
| I need a shared cloud installation | Prepared infrastructure, registry images, PostgreSQL, HTTPS identity, and Kubernetes | [Cloud deployment](../operations/cloud-deployment.md) |

**For your first deployment, follow [local platform setup](local-platform.md).** It gives you a saved
successful workflow before adding workers or cloud infrastructure. An offline
simulation is useful for learning the language but creates no server or saved run.

## Understand what stays running

![API, dependencies, terminals, and optional executors](../diagrams/operations-topology.svg)

The first local setup uses the API and dependencies in this diagram. Worker and
native-executor processes are added in the next guide; they are optional for the
first echo workflow. [Open the diagram at full size](../diagrams/operations-topology.svg).

| Component | Why it exists | Required for the first saved run? |
| --- | --- | --- |
| `weave` CLI or Python SDK | Sends requests and reads results | A client is needed to submit the run; it can exit afterward |
| Weave API | Authorizes requests, stores definitions, starts runs, and schedules work | Yes; keep the API process running |
| PostgreSQL | Retains workflow state, grants, task leases, and history | Yes; local setup creates a dedicated database |
| Identity provider | Issues tokens the API verifies | Yes; local setup uses Keycloak and its own database container |
| Remote worker | Claims admitted tasks over HTTP and invokes your handler | Only for workflows using remote task handlers |
| Native executor | Runs configured built-in connectors with database authority | Only for workflows using those connectors |
| External service | Receives the integration's real requests | Only for integrations; the worker tutorial uses an owned local receiver |

The API enables background scheduling by default and uses a separate scheduler
database login. The first tutorial does not require another scheduler terminal.
A remote worker needs API credentials and grants, but no database credentials.
The [configuration guide](../operations/configuration.md) describes each process's
settings when you extend this topology.

## Keep client installation and server startup separate

`weave --help` runs the installed client and returns to your shell. It does not
start a server. `weave init DIRECTORY` creates a local workflow project; it does
not provision databases, identity, or containers. `weave docs platform` prints
the documentation URL for this page.

The local platform guide builds and installs a selected artifact into its own
server environment. It records that interpreter as `WEAVE_PYTHON` and uses
`"$WEAVE_PYTHON" -I -m firefly_weave.cli.main` for operator commands tied to that
installation. Your user-local `weave` command remains a separate installation.

The actual API entrypoint is `firefly_weave.main:create_application`, launched
through Uvicorn. Follow [the startup step](standalone.md#4-launch-and-check-the-api)
after creating the database, runtime configuration, and identity link. The full
command includes the selected Python executable, environment, interface, and
port; starting it without that configuration is not a complete installation.

`weave platform setup` prepares the local installation and `weave platform start`
launches its API. Use `status`, `demo`, and `stop` to inspect, try, and stop it.
Follow [the short CLI walkthrough](local-platform.md) for exact commands. The existing
`weave worker deploy --target compose` starts a prepared **worker container** on
a selected local Docker engine. Cloud infrastructure and Kubernetes deployment
use the operator procedures in the cloud guide.

## Inspect the manual setup steps

Prepare Python 3.12+, Git, `uv`, and a running local Docker engine with Compose
2.30 or newer. Use Bash or Zsh and the release checkout selected by the
[prerequisites](standalone.md#before-you-start). Allow package and image downloads.
A client-only installation does not supply the repository's setup scripts.

The CLI automates these steps for a first local installation. Use this checklist
with the [manual local walkthrough](standalone.md) when you want to inspect each
operator action. Each row
links to the commands and expected result for that stage; do not skip ahead.

| Step | Action | Ready to continue when… |
| --- | --- | --- |
| 1 | [Select the local engine, private work directory, and server artifact](standalone.md#1-reserve-the-installation-and-install-exact-artifacts) | The selected Python reports its installed Weave version |
| 2 | [Create PostgreSQL and Keycloak, then migrate the runtime database](standalone.md#2-create-fresh-database-and-identity-services) | Identity discovery succeeds and the schema is current |
| 3 | [Verify the host identity and bootstrap its link](standalone.md#3-verify-and-bootstrap-one-local-identity) | A private bootstrap receipt contains the real principal ID |
| 4 | [Start the API in terminal 2](standalone.md#4-launch-and-check-the-api) | Terminal 1's readiness check prints `API is ready` |
| 5 | [Create scope and grants, publish, activate, and run](standalone.md#5-grant-scope-publish-activate-and-run) | The saved run succeeds with `{"message": "Hello, Weave"}` |
| 6 | [Use the CLI to publish your own workflow](cli-tutorial.md) | You can start a run and inspect its history through the API |

**Terminal 1** is your setup and client shell. Run repository-relative commands
there from the checkout root. **Terminal 2** keeps the API running; a terminal
showing API logs is occupied and is not waiting for another setup command.
**Terminal 3** is needed only for the later worker tutorial's HTTP receiver.

New terminals do not inherit variables you exported elsewhere. The setup prints
exact `cd` and `source .../session.env` commands: save and reuse those commands.
`session.env` restores paths and ports. Load private runtime or identity settings
only where the relevant step tells you to; those are separate files.

Names such as `WEAVE_WORK_DIR` are variables populated by the walkthrough, not
paths to guess. A **receipt** is a private file containing real IDs returned by
setup or API calls. Keep receipts and credentials with the installation. Later
steps read those IDs; substituting an arbitrary UUID cannot create permission.

## Use the running platform

A durable workflow follows this sequence:

1. **Author and validate:** write a workflow definition and check it locally.
2. **Publish:** save an immutable version in the authorized project.
3. **Activate:** select that version for an environment.
4. **Run:** submit input to that activation.
5. **Inspect:** read the run's output, history, or incident state.

The [CLI tutorial](cli-tutorial.md) walks through each request file and command.
The [SDK guide](../reference/sdk.md) shows the same operations from Python. If the
workflow invokes a remote task, also package its handler, admit its release, grant
its worker authority, and start the worker using [the deployment guide](../operations/deployment.md).
Publishing a workflow does not start a worker, and building a worker image does
not grant permission to claim tasks.

## Stop and resume without repeating initialization

For the initial local setup, stop the foreground API with Ctrl-C, then use the
[scoped Compose stop command](standalone.md#7-stop-safely-and-retain-data).
Containers, volumes, credentials, and saved runs remain. If workers or a native
executor were added, follow [their shutdown order](../operations/deployment.md#stop-the-intended-scope)
first so every writer stops before its dependencies.

On your next visit, follow [resume this installation](standalone.md#resume-this-installation-later):
restore the same session and secret files, start the same dependencies, check
identity readiness, start the API, refresh the token, and read the existing run.
Do not regenerate secrets, bootstrap again, or rerun resource creation to resume.

If startup fails, begin with [local setup troubleshooting](standalone.md#if-a-step-does-not-work).
A `401` usually needs a current token; a `403` needs the correct identity link,
scope, and grants. A worker container showing `running` still needs a successful
workflow completion to demonstrate useful work. Cloud deployments require their
own [readiness and acceptance checks](../operations/cloud-deployment.md).
