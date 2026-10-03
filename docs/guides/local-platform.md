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

Use this guide to run a real Weave platform on your own computer: the API, its
PostgreSQL database, and a Keycloak identity provider for sign-in. It is for
anyone who wants a private platform to learn on, develop against, or try Studio
with. Each step is one command; the first `setup` takes longest, because it
downloads container images and dependencies. The CLI creates private configuration,
starts its own containers, and prepares an isolated server environment. You do
not copy database URLs, look up identity subjects, or grant permissions by hand.

By the end you have:

- a running API with one saved, successful workflow run;
- a person who signs in to the CLI and Studio through the local Keycloak;
- optionally, a workflow that calls a public REST API through the built-in HTTP
  connector.

**This is a development platform.** Keycloak, the generated passwords, and the
loopback ports are for your computer only. For a shared platform,
[configure your own compatible OIDC identity provider](../operations/identity-and-secrets.md#use-your-own-identity-provider)
and follow the [deployment guides](../operations/remote-deployment.md). For an
offline first workflow, use the [quickstart](../quickstart.md); to use a platform
that already exists, [connect the CLI to it](connect-to-api.md).

## The route at a glance

| Step | You run | Checkpoint |
| --- | --- | --- |
| [1. Check your tools](#1-check-your-tools) | `weave platform doctor` | Your CLI version, checkout, and Docker context |
| [2. Set up once](#2-set-up-once) | `weave platform setup` | `Stage: ready` |
| [3. Start the API](#3-start-the-api) | `weave platform start` (keep it running) | Startup logs in this terminal |
| [4. Run your first real workflow](#4-run-your-first-real-workflow) | `weave platform demo` | `Workflow: succeeded` |
| [5. Create a person who can sign in](#5-create-a-person-who-can-sign-in) | `weave platform user` | A username and a one-time password |
| [6. Sign in from the CLI](#6-sign-in-from-the-cli) | `weave auth setup` | `weave auth status` shows you signed in |
| [7. Open Studio](#7-open-studio) | `weave studio` | Studio opens on your saved platform |
| [8. Run built-in HTTP connector actions](#8-run-built-in-http-connector-actions) | `weave platform integrations enable` | `Integrations enabled: True` |
| [9. Explore the API](#9-explore-the-api) | `weave platform token` | Swagger UI at `/docs` |
| [10. Stop and come back later](#10-stop-and-come-back-later) | `weave platform stop` | Your data is kept |

**What was verified end to end.** On macOS with a local Docker engine and the
pinned Keycloak 26.7.4, this sequence ran from `setup` through a successful
connector run. Studio was checked separately against the same kind of platform by
an opt-in browser test, started with `--assets` from a source build; it connects
on its own, without the CLI's saved platform. Sign-in completed against the real
Keycloak sign-in pages with PKCE and with the device code flow, using the private
file credential store. The desktop app's sign-in, and the Windows and Linux
credential stores, were not part of that run.

## 1. Check your tools

You need Python 3.12 or later, `uv`, and a running local Docker engine with
Compose 2.30 or later. This workflow supports macOS and Linux with a Unix-socket
Docker context. It creates a local development platform with loopback ports;
production deployment has separate requirements.

The platform commands run from a source checkout, because the Compose files and
provisioning helpers are operator assets that the small installed CLI does not
contain. The platform builds its server from that checkout, so the checkout
decides which features the platform has. With the alpha7 CLI from
[installation](../installation.md), clone the matching release into a new
directory, and keep any checkout you already use for development untouched:

```sh
# Download the alpha7 release's operator files into a new directory.
git clone --branch v0.1.0a7 --single-branch https://github.com/fireflyframework/firefly-weave.git firefly-weave-local
# Run the following steps from that matching checkout.
cd firefly-weave-local
```

Expected: Git reports a detached `HEAD` at the release tag; that is normal.

**Contributors** can instead [install the current source](../installation.md#install-the-current-source),
which clones `firefly-weave` and installs the matching CLI from it, and run every
`weave platform` command from that checkout.

**Already run an alpha6 local platform?** It keeps its data and keeps working
with the alpha6 CLI and checkout that created it; set up alpha7 from its own
checkout, as a separate installation. An alpha6 or earlier checkout has no
`weave platform user`, `integrations`, or `secret`, and its CLI has no
`weave auth setup`, so with it you can follow steps 1 to 4, 9, and 10 only.

Check the prerequisites from the checkout:

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

Expected: `Stage: ready`, the API and docs URLs, and the next three commands:
`start` in this terminal, `demo` in another, then `user` to create a person who
can sign in. Keep the installation directory: it holds your retained data
configuration and receipts. To choose a different location, put `--directory`
**before** the subcommand every time:

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

Before the server logs, `start` prints `Starting the local API. Keep this terminal
open; Ctrl-C stops the API.` and, when they apply, short notices:

- `Updated the local Keycloak login client so browser sign-in accepts any loopback
  port.` — `start` added the port-free loopback callbacks to a realm created
  before they existed. See [troubleshooting](#browser-sign-in-is-refused).
- `Secret handles granted to the demo environment: ...` — the handles stored with
  `weave platform secret set`.
- `Connector actions: enabled in the demo environment (built-in HTTP connector,
  local development build).` — set up in [step 8](#8-run-built-in-http-connector-actions).

## 4. Run your first real workflow

Open a second terminal in the same checkout. Check readiness:

```sh
# Wait for both API and identity readiness before making authenticated requests.
weave platform status
```

Continue when `Api ready: True` and `Identity ready: True` appear. Then run:

```sh
# Create the demo workspace and verify one real successful run.
weave platform demo
```

This creates a tenant, project, environment, and scoped grants through the public
API. It publishes and activates an example workflow, executes it, and saves the
real result in `.local/platform/first-run.json`.

Expected: `Workflow: succeeded`, output `{"message": "Hello, Weave"}`, the run ID,
the saved receipt path, and next commands for `user` and `integrations enable`.
Repeating `demo` reads that receipt instead of creating another tenant or run.
The underlying requests are shown in [`examples/first_run.py`](../../examples/first_run.py).
The tenant, project, and environment it creates are the **demo workspace** that
the following steps use.

## 5. Create a person who can sign in

The local Keycloak starts with no people. Create one development account, linked
to a Weave person with roles in the demo workspace. The API must be running:

```sh
# Create a development account; choose your own lowercase username.
weave platform user --username YOUR_NAME
```

Expected: `Created a local sign-in account and linked it to a new person in the
demo workspace.`, then `Username:`, a generated `Password:`, a development-only
warning, the roles, and the next two commands. **Copy the password now.** It is
printed once and saved nowhere; running the command again for an existing username
fails and never resets its password.

Usernames are 3 to 64 lowercase letters and digits, joined by single `.`, `_`, or
`-`. The default roles are the smallest set to author, publish, activate, run,
read runs, and work on human tasks:

| Role | Scope | Lets the person |
| --- | --- | --- |
| `developer` | Demo project | Read the catalog, compile, simulate, write, and publish definitions |
| `deployer` | Demo environment | Activate and retire versions, bind connections, manage triggers and subscriptions |
| `operator` | Demo environment | Start, pause, resume, retry, cancel, and signal runs; inspect and resolve incidents |
| `viewer` | Demo environment | Read runs, deliveries, status, and the catalog |
| `task_participant` | Demo environment | Read, claim, complete, and release assigned human tasks |

**Choose the roles now: you cannot add them later with this command.** An
existing username is never changed, and `--role` replaces the defaults, so list
every role you want. The defaults miss two roles that later guides need:

- **Integration connections** need `tenant_admin` (step 8 and
  [Call a REST API without code](../connectors/http-without-code.md)).
- **Reviewer groups and assignment bindings** need `task_manager`
  ([human tasks](human-tasks.md)).

To follow every guide as one person, grant all of them at once:

```sh
# Development only: one person who can follow the REST, approval, and Studio guides.
weave platform user --username YOUR_NAME --role tenant_admin --role developer \
  --role deployer --role operator --role viewer --role task_participant \
  --role task_manager
```

Expected: the same output, with `tenant_admin (demo tenant)` first in `Roles:`.
`weave platform user --help` lists every role you can grant; `platform_admin` and
`worker` are never offered. **Already created your person with fewer roles?**
Create a second username with the full set, then sign in as it with
`weave auth login --switch-account`.

## 6. Sign in from the CLI

`weave platform status` prints the exact command on its `Sign in:` line. Run it
with a name for this saved platform:

```sh
# Connect to this local API, review its identity provider, sign in, and pick the workspace.
weave auth setup http://127.0.0.1:API_PORT --name local
```

Replace `API_PORT` with the port from `status`. The command walks through four
steps on standard error. It asks you to trust the identity provider, `[y/N]`:
answer `y`, because this Keycloak is the one `weave platform setup` started for
you. It then opens your browser at the local Keycloak. Sign in with the username
and password from step 5.

This transcript comes from a real run with `--yes --flow browser --no-browser`,
which skip the question and print the browser address instead of opening it.
Ports, the address, the workspace name, and the folder differ on your computer:

```text
Step 1 of 4 · Server
Contacting http://127.0.0.1:API_PORT ...
Found Local Weave platform at http://127.0.0.1:API_PORT.

Step 2 of 4 · Review
Checking the identity provider at http://localhost:KEYCLOAK_PORT ...
  Platform:          Local Weave platform (http://127.0.0.1:API_PORT)
  Sign-in provider:  Local Keycloak (development)
  Identity provider: http://localhost:KEYCLOAK_PORT
  Sign-in methods:   browser on this computer, code on another device
  Login client:      weave-cli (scopes: openid)
  Profile name:      local
Trust confirmed with --yes.
Saved platform 'local' to /Users/YOUR_USER/Library/Application Support/Firefly Weave/profiles.json (server address, identity provider and workspace choice only; no passwords or tokens).

Step 3 of 4 · Sign in
Open this address in a browser on this computer to sign in:
  http://localhost:KEYCLOAK_PORT/realms/weave/protocol/openid-connect/auth?response_type=code&client_id=weave-cli&redirect_uri=http%3A%2F%2F127.0.0.1%3ALOOPBACK_PORT%2Fcallback&...&code_challenge_method=S256
Waiting for you to finish signing in (Ctrl+C cancels)...
Done (0s).
Signed in as YOUR_NAME.

Step 4 of 4 · Workspace
Using the only workspace available: first-run-... / first-project / local
Workspace: first-run-... / first-project / local
```

Expected: exit status 0 and a summary that starts with `Platform 'local' is set
up and active.`

The browser sign-in uses PKCE (a one-time proof that ties the browser's answer
to this CLI) and returns to a random port on `127.0.0.1`. To sign in on another
device instead, add `--flow device`: the CLI prints an address and a code to
enter there. The saved platform file, `profiles.json`, holds no passwords or
tokens. Credentials go to the system credential store by default; add
`--credential-store file --credential-file /absolute/private/path.json` to keep
them in a private file instead. The verified run used the file store.
[What is saved, and where](connect-to-api.md#what-is-saved-and-where) lists the
locations on each operating system.

Check the result:

```sh
# Show the saved platform, account, and workspace.
weave auth status
```

Expected, with the default credential store:

```text
Platform:          local (active)
Server:            http://127.0.0.1:API_PORT
Identity provider: http://localhost:KEYCLOAK_PORT
Sign-in:           signed in as YOUR_NAME (until ..., renews automatically)
Workspace:         first-run-... / first-project / local
Credentials:       system credential store
```

Remote commands such as `weave runs list` now use this platform and workspace
without `--base-url` or scope flags. [Connect the CLI to a platform](connect-to-api.md#everyday-commands)
shows the everyday commands: sign in again, switch account, change workspace,
and sign out.

## 7. Open Studio

Studio is the visual editor. Because the CLI and Studio share saved platforms,
Studio opens already connected to the platform you saved in step 6.

**First install Studio's browser application** if you have not yet: download
the matching alpha7 bundle and install it as in
[Install the alpha7 browser application](studio.md#install-the-alpha7-browser-application).
Then start Studio:

```sh
# Start Studio's local host with the installed browser application.
weave studio
```

Expected: `Firefly Weave Studio · Platform: local`, then `Open
http://127.0.0.1:8766` and a `Pairing code:` line. Your browser opens; type the
code into **Pairing code** and select **Pair browser**. Pairing only links
this browser tab to the host on your computer; it is not a sign-in. Keep the
terminal running while you use Studio.

With no installed bundle, the command stops with `Studio is not installed. Run
weave studio install --help, or use --assets with a local build`. Contributors
who [build Studio from the current source](studio.md#start-from-source) start it
with `weave studio --assets studio/dist/studio/browser` instead. The
[Studio guide](studio.md) explains pairing, drawing workflows, and running them.
In the desktop app, sign-in with a code was verified on a locally built macOS
app; browser sign-in from the app was not, as the
[desktop guide](desktop.md#what-has-been-verified) explains.

## 8. Run built-in HTTP connector actions

The local platform can run Actions on the built-in `weave-http@2.0.0` connector
in the demo environment. With the API running, in your second terminal:

```sh
# Publish the built-in connector, register this runtime as its release, and grant its executor.
weave platform integrations enable
```

Expected:

```text
Connector: weave-http@2.0.0 (version ID CONNECTOR_VERSION_ID)
Local release: RELEASE_ID
Activations of workflows using these actions pin connector_release_ids:
  {"CONNECTOR_VERSION_ID": "RELEASE_ID"}
Restart the API to run connector actions: press Ctrl-C in its terminal, then run:
  weave platform --directory /absolute/path/.local/platform start
```

followed by the commands for storing a secret and granting a connection. Repeating
`enable` creates nothing new.

If the API you will call needs a key, store it behind a handle now, so a single
restart applies both:

```sh
# Store an API key from a private file; the value is never printed.
weave platform secret set --handle pets-api-key --value-stdin < /absolute/path/to/pets-api-key.txt
```

Expected: `Handle: pets-api-key` and `Stored privately. Restart the API (Ctrl-C,
then start) so the demo environment can use this handle.` Without
`--value-stdin`, the command asks for the value at a hidden, confirmed prompt.
`--value-stdin` refuses a terminal because the value would be visible. The value
lives in `.local/platform/secrets/` and is granted to the demo environment only.

Restart the API: press Ctrl-C in the first terminal, then run `weave platform
start` again. Expected: `Secret handles granted to the demo environment:
pets-api-key` (when you stored one) and `Connector actions: enabled in the demo
environment (built-in HTTP connector, local development build).` Then `weave
platform status` shows `Integrations enabled: True` and the `Connector release
ids` map.

Now run a connector action: follow [Call a REST API without code](../connectors/http-without-code.md#call-an-api-the-verified-example).
It builds a `GET /todos/{id}` Action against `https://jsonplaceholder.typicode.com`,
creates the connection, grants it, and runs a workflow, signed in as the person
from step 5. Or, in Studio: [Call a REST API from a step](studio.md#call-a-rest-api-from-a-step).

**The local executor has fixed limits.** It runs only the built-in HTTP connector,
never connector packages. It calls only public HTTPS destinations: an API on your
own computer or private network is refused. A new secret handle needs a restart;
replacing an existing handle's value does not. A connection whose secret handles
it must read needs `weave platform integrations grant --connection
REVISION_ID --access read` (or `write`). To stop running connector actions, run
`weave platform integrations disable` and restart; the server keeps the release
and grants, and `enable` reuses them.

## 9. Explore the API

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

See the [Python SDK tutorial](sdk-tutorial.md) to call the same API from
Python, and the [API playground guide](api-playground.md) to send your first
request from the browser.

## 10. Stop and come back later

Press Ctrl-C in the API terminal first. Then run:

```sh
# Stop only this installation after Ctrl-C has stopped its foreground API.
weave platform stop
```

Only this installation's dependency containers stop. Volumes, databases, secrets,
and workflow history remain. To return later, enter the same checkout and run
`weave platform start`. Use the same `--directory` if you selected a custom one.
Your saved CLI platform and the person's account remain too; `weave auth status`
tells you whether to sign in again.

There is deliberately no reset or delete command in this lifecycle. It never
changes another project's containers or removes a database or volume.

## What to do next

Your platform is running and you can sign in to it. Continue with the task you
came for:

- Publish, activate, and run your own workflow from the terminal with the
  [CLI tutorial](cli-tutorial.md).
- Draw a workflow, add an approval, and run it in [Studio](studio.md). Create
  the reviewer assignment with the CLI first, as in
  [human tasks, step 2](human-tasks.md#2-create-a-reviewer-group-and-assignment).
- Call a REST API from a workflow without code:
  [Call a REST API without code](../connectors/http-without-code.md).
- Give other people access with [People and access](people-and-access.md).

## Files you keep

| File inside the private installation directory | Purpose |
| --- | --- |
| `session.env` | Nonsecret shell variables for subsequent CLI, SDK, and deployment guides |
| `platform.json` | Version, original checkout, engine, ports, project ownership, setup stage |
| `runtime/` and `release/` | Isolated installed server and exact build receipt |
| `postgres.env`, `identity.env`, `runtime.env` | Private credentials and configuration |
| `bootstrap.json` | Verified initial principal receipt |
| `host-token.json` | Renewable host access token; never share it |
| `first-run.json` | Demo workspace and successful run receipt |
| `integrations.json` | Connector release, executor principal, and build identity from `integrations enable` (mode 0600) |
| `secrets/` | One file per secret handle (directory 0700, files 0600); values are never printed |
| `*.log` | Private stage diagnostics, including `build-identity-*.log` from each start with integrations; inspect locally before sharing any excerpt |

For scripts, `doctor`, `setup`, `status`, `demo`, `token`, `stop`, `user`,
`integrations`, and `secret` support `--output json`. JSON output disables
animated progress. `start` is intentionally foreground and human-facing. Set
`WEAVE_NO_ANIMATION=1` to disable animation in a terminal while retaining progress
messages.

## Troubleshoot

| What you see | Why | What to do |
| --- | --- | --- |
| `Api ready: False` | The API is not running, still starting, or stopped with an error | Read the API terminal. If it stops after you enabled integrations, run `weave platform integrations disable`, then `start` again |
| `Identity ready: False` | Keycloak is still starting, or failed to start | Check again. If it stays false, inspect the newest `dependencies-start-*.log` |
| `The saved API port is already occupied; ...` | Another `start` for this installation is still running | Use that terminal; nothing was stopped or replaced |
| `Installation directory already exists. ...` | `setup` never replaces an installation | Run `start` to resume it, or choose a new `--directory` |
| `Stage NAME failed. Inspect the private NAME-*.log file ...` | A setup or start stage failed | Open that log in the installation directory; the sections below cover common causes |
| `Docker network pools are exhausted. ...` | Docker has no free address range for a new network | [Choose a free network range](#when-docker-has-no-free-network-ranges) |
| A Keycloak error about `redirect_uri` in the browser | The realm predates the port-free loopback callbacks | See [Browser sign-in is refused](#browser-sign-in-is-refused) |
| `A local sign-in account named NAME already exists. ...` | Existing accounts and passwords are never reset | Choose another username |
| `You signed in, but this platform does not recognize your account yet.` | That account is not linked to a Weave person | Sign in with the account from step 5, created by `weave platform user` |
| `Connector actions are disabled: the runtime build no longer matches the enabled release. ...` | The runtime's build identity changed after `integrations enable` registered its release | With the API running, run `weave platform integrations enable`, then restart |
| `The private secret store contains an unexpected entry; ...` | A file that is not a handle, such as `.DS_Store`, is in `secrets/` | Remove that file, then retry |
| `Use this installation's original directory and matching CLI version.` | The installation was created by another CLI version, or you ran it from another directory | Use the CLI version and directory that created it. To move to a new release, set up a new installation from that release's checkout; see [upgrades](../operations/upgrades.md#a-local-platform-has-no-in-place-upgrade) |
| `The source checkout has changed. Restore its original content before resuming this installation.` | Files in the checkout that created the installation changed, for example after `git pull` | Restore that checkout's content, or set up a new installation from a separate checkout |
| A connector run stays queued | Integrations are not enabled for the current demo workspace, or the API was not restarted | Check `Integrations enabled: True` in `status` and the connector notice at the top of the API terminal |

### Browser sign-in is refused

Browser sign-in listens on a random loopback port. Keycloak accepts any port only
for callbacks registered without one, so the local login client registers
`http://127.0.0.1/callback` and `http://[::1]/callback`. Realms created before
these entries existed keep their old list, because Keycloak never re-imports a
retained realm. `weave platform start` checks the `weave-cli` client and adds the
two entries when they are missing, printing `Updated the local Keycloak login
client so browser sign-in accepts any loopback port.` It only adds entries; it
never removes or rewrites the others.

If `start` cannot update the client, it prints `Browser sign-in may be refused by
the local Keycloak login client. Sign in with a code instead: weave auth login
--flow device`. The device code flow does not use a callback, so it still works.

### When Docker has no free network ranges

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

### When Docker runs out of disk space

Setup downloads images, builds a wheel, and installs dependencies, so a nearly
full Docker disk fails a stage with `Stage NAME failed`. Weave does not detect
this case for you: look in that stage's log for Docker's own error, which
typically reads `no space left on device`. Weave never prunes images or
volumes. Free space in that Docker engine yourself, removing only resources you
own, or give its virtual machine a larger disk. Then stop the failed installation
and set up a new one:

```sh
# Stop any dependencies the failed setup already started; data is kept.
weave platform --directory .local/platform stop
# Set up again in a new directory; the failed one keeps its logs for inspection.
weave platform --directory .local/platform-2 setup
```

Expected: the new setup ends with `Stage: ready` and the next commands, as in
[step 2](#2-set-up-once). Use the same `--directory` for every later command of
the new installation.
