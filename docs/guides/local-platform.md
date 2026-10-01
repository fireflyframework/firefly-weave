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

# Start a local platform in small steps

Use this guide to run a real Weave API on your laptop. The CLI creates private
configuration, starts its own PostgreSQL and Keycloak containers, and prepares an
isolated server environment. You do not need to copy database URLs, look up
identity subjects, or grant permissions by hand.

Keycloak is the identity provider for this local exercise. For a deployed platform,
[configure your own compatible OIDC/CIAM provider](../operations/identity-and-secrets.md#use-your-own-identity-provider).

For an offline first workflow, use the [quickstart](../quickstart.md).
For an existing API, use [connect to an API](connect-to-api.md).

## 1. Check your tools

You need Python 3.12 or later, `uv`, and a running local Docker engine with
Compose 2.30 or later. This workflow supports macOS and Linux with a Unix-socket
Docker context. It creates a local development platform with loopback ports;
production deployment has separate requirements.

The platform commands use a matching source checkout because the Compose files
and provisioning helpers are operator assets. The installed CLI stays small.
Follow [installation](../installation.md) to install the CLI. Obtain a separate
matching checkout, preserving any checkout you already use for development:

```sh
# Download the release's operator files into a new directory.
git clone --branch v0.1.0a4 --single-branch https://github.com/fireflyframework/firefly-weave.git firefly-weave-local
# Run the following steps from that matching checkout.
cd firefly-weave-local
```

Then check the prerequisites:

```sh
# Check the matching checkout and local Docker engine without changing services.
weave platform doctor
```

Expected: your CLI version, checkout path, and selected Docker context. This
command does not create files or change services. To choose a different local
Docker engine, add `--context NAME` to `doctor` and `setup`.

If the versions differ, preserve your existing checkout and use a separate
checkout of the CLI's release. From another working directory, specify
`weave platform doctor --source /absolute/path/to/firefly-weave`.

## 2. Set up once

From the matching checkout:

```sh
# Prepare this installation once and keep its private configuration.
weave platform setup
```

The command selects unused local ports, creates `.local/platform` with private
permissions, builds one wheel, installs locked server dependencies in its own
Python environment, and starts a uniquely named Docker project. Container images
and dependencies download when needed. Progress shows the current stage.

It then creates the guarded runtime database, restricted application and scheduler
logins, and a verified local host identity. API processes never receive migration
credentials or the Keycloak administrator secret. The final output includes the
API URL and its interactive documentation URL.

Expected: `Stage: ready`, followed by the next commands. Keep the installation
directory: it holds your retained data configuration and receipts. To choose a
different location, put `--directory` **before** the subcommand every time:

```sh
# Choose an explicit private location when the default is unsuitable.
weave platform --directory /absolute/private/weave-local setup --source /absolute/path/to/firefly-weave
```

Setup never replaces an existing directory. Use `start` to resume a successful
installation. If setup fails, its saved stage and private logs remain available;
do not delete receipts or repeat database provisioning blindly. `status` can
inspect incomplete setup, and `stop` can stop any dependencies already created.

## 3. Start the API

In the same terminal:

```sh
# Keep this terminal open while the API serves requests.
weave platform start
```

Leave this terminal open. The API runs in the foreground and prints its startup
logs. Ctrl-C stops this API process. Starting again reuses the same dependencies,
database, secrets, and identity; it does not migrate or provision again.

The command verifies the original checkout, Docker engine, and installed wheel
before starting. Keep that checkout unchanged for this installation. Upgrading an
existing installation requires the explicit [upgrade procedure](../operations/upgrades.md).

## 4. Run your first real workflow

Open a second terminal in the same checkout. Check readiness:

```sh
# Wait for both API and identity readiness before making authenticated requests.
weave platform status
```

Continue when `Api ready: True` and `Identity ready: True` appear. Then run:

```sh
# Create the example scope and verify one real successful run.
weave platform demo
```

This creates a tenant, project, environment, and scoped grants through the public
API. It publishes and activates an example workflow, executes it, and saves the
real result in `.local/platform/first-run.json`.

Expected: the receipt reports `succeeded` and output
`{"message": "Hello, Weave"}`. Repeating `demo` reads that receipt instead of
creating another tenant or run. The underlying requests are shown in
[`examples/first_run.py`](../../examples/first_run.py).

## 5. Explore the API

Open the `Docs url` printed by `status`. This local API enables an interactive
OpenAPI playground at `/docs`. Authentication still applies to API operations.
The documentation route itself is public only when explicitly enabled; ordinary
server deployments keep it disabled unless configured otherwise.

Refresh the local host credential when needed:

```sh
# Renew the token privately without recreating identities or grants.
weave platform token
```

This saves a verified access token in `.local/platform/host-token.json` and prints
only its path. Keep that file private. Use its `access_token` value in the
playground's **Authorize** field. The saved demo receipt contains the tenant,
project, environment, activation, and run IDs for requests; you do not need to
create them again.

To continue with guides that use shell variables, load the generated nonsecret
session file from the same checkout. This restores the selected API URL, private
work directory, and installed interpreter without loading administrator secrets:

```sh
# Reuse this installation's paths and selected ports in your client terminal.
source .local/platform/session.env
# Refresh the private host token before a later authenticated example.
weave platform token
```

For a custom location, source that directory's `session.env` instead. Keep using
`platform token` to refresh credentials; do not repeat bootstrap or setup.

See the [SDK reference](../reference/sdk.md) to call the same API from Python,
and [CLI reference](../reference/cli.md) for workflow publication and execution.

## 6. Stop and come back later

Press Ctrl-C in the API terminal first. Then run:

```sh
# Stop only this installation after Ctrl-C has stopped its foreground API.
weave platform stop
```

Only this installation's dependency containers stop. Volumes, databases, secrets,
and workflow history remain. To return later, enter the same checkout and run
`weave platform start`. Use the same `--directory` if you selected a custom one.

There is deliberately no reset or delete command in this lifecycle. It never
changes another project's containers or removes a database or volume.

## Files you keep

| File inside the private installation directory | Purpose |
| --- | --- |
| `session.env` | Nonsecret shell variables for subsequent CLI, SDK, and deployment guides |
| `platform.json` | Version, original checkout, engine, ports, project ownership, setup stage |
| `runtime/` and `release/` | Isolated installed server and exact build receipt |
| `postgres.env`, `identity.env`, `runtime.env` | Private credentials and configuration |
| `bootstrap.json` | Verified initial principal receipt |
| `host-token.json` | Renewable host access token; never share it |
| `first-run.json` | Demo scope and successful run receipt |
| `*.log` | Private stage diagnostics; inspect locally before sharing any excerpt |

For scripts, `doctor`, `setup`, `status`, `demo`, `token`, and `stop` support
`--output json`. JSON output disables animated progress. `start` is intentionally
foreground and human-facing. Set `WEAVE_NO_ANIMATION=1` to disable animation in a
terminal while retaining progress messages.

## When Docker has no free network ranges

Most installations can use Docker's default network pool. If Docker reports that
all predefined address pools are exhausted, preserve existing projects. Choose a
different local Docker context or an explicit unused private IPv4 subnet in a new
installation directory. For example, after checking that this range is suitable
for your local Docker engine and network:

```sh
# Use a new private installation and an explicit unused Docker network range.
weave platform --directory .local/platform-with-subnet setup --subnet 10.245.1.0/24
```

Setup rejects overlap with existing Docker networks and saves this choice for
later starts and stops. It accepts canonical RFC1918 IPv4 networks with prefixes
from `/16` through `/28`. This option never changes the daemon configuration or
prunes another project's resources. A failed setup directory retains its logs
and stage; using a new directory preserves that evidence.
