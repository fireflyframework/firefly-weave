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

# Connect the CLI to an existing Weave API

Use this guide when your team already runs Weave. You do not need to run
PostgreSQL, Keycloak, or the server yourself. Your first checkpoint is a
**read-only catalog request** that confirms your identity and project access.

Install the [CLI](../installation.md) first. If you need to create a development
server instead, start with [Run Weave locally](standalone.md).

![Your product or CLI calls Weave through an authenticated API boundary](../diagrams/authoring-execution-boundaries.svg)

Follow row B from your authenticated client to the running API. The server
checks access; your CLI needs no database credentials. [Open diagram](../diagrams/authoring-execution-boundaries.svg).

## 1. Get your connection details

Ask the operator of your Weave installation for these values:

| Value | What it selects |
| --- | --- |
| API origin | The HTTPS address of Weave, without an `/api/v1` suffix |
| Tenant ID | Your organization or other top-level workspace |
| Project ID | The project containing the workflows |
| Environment ID | Where versions are activated and runs execute |
| Authentication | An approved login configuration or a current access token |

The three IDs are UUIDs returned by Weave. A project name or Keycloak client name
cannot replace them. The operator must also link your verified identity to a
Weave principal and grant the required access. A valid token alone does not grant
project access.

## 2. Select your API and scope

In Bash or Zsh, read the supplied values into environment variables. These
prompts avoid examples with made-up IDs that cannot work on your installation:

```sh
printf 'Weave API origin: '; read -r WEAVE_BASE_URL
printf 'Tenant UUID: '; read -r WEAVE_TENANT_ID
printf 'Project UUID: '; read -r WEAVE_PROJECT_ID
printf 'Environment UUID: '; read -r WEAVE_ENVIRONMENT_ID
export WEAVE_BASE_URL WEAVE_TENANT_ID WEAVE_PROJECT_ID WEAVE_ENVIRONMENT_ID
```

These variables apply only to this terminal and select the destination of later
CLI calls. Remote commands also accept `--base-url`, `--tenant`, `--project`, and
`--environment`. Explicit options take precedence over environment defaults.
For local development, use the exact loopback API origin provided by its operator.

## 3. Authenticate using one method

### Option A: your team supplies a login configuration

Set `WEAVE_AUTH_CONFIG` to the local JSON configuration file provided by your
operator. It describes the approved issuer, OAuth client, and API origin. It is
not an arbitrary provider configuration copied from another installation.

```sh
printf 'Path to login configuration: '; read -r WEAVE_AUTH_CONFIG
export WEAVE_AUTH_CONFIG
WEAVE_AUTH_OPTIONS=(--auth-config "$WEAVE_AUTH_CONFIG")
weave auth login "${WEAVE_AUTH_OPTIONS[@]}"
weave auth status "${WEAVE_AUTH_OPTIONS[@]}"
```

Follow the displayed browser/device instructions. The login uses the operating
system's credential store by default. Status reports `authenticated: true` after
a successful login. `WEAVE_AUTH_OPTIONS` is a Bash/Zsh array kept in this terminal.
It carries the same configuration and credential store into every command.

**If the native credential store is unavailable**, select a private file explicitly.
Create a new owner-only directory and extend the same options array, then retry:

```sh
umask 077
WEAVE_AUTH_DIRECTORY="$(mktemp -d "$HOME/.weave-login.XXXXXXXX")"
export WEAVE_AUTH_DIRECTORY
WEAVE_AUTH_OPTIONS=(--auth-config "$WEAVE_AUTH_CONFIG"
  --credential-store file --credential-file "$WEAVE_AUTH_DIRECTORY/session.json")
weave auth login "${WEAVE_AUTH_OPTIONS[@]}"
weave auth status "${WEAVE_AUTH_OPTIONS[@]}"
```

Retain the directory path privately. When opening a new terminal, rebuild the
options array with that same path instead of creating another session directory.
The CLI requires a caller-owned 0700 directory and a 0600 credential file.
See [credential persistence](../reference/cli.md#login-and-secure-persistence)
for the exact checks.

Now make the read-only request using that same configuration:

```sh
weave remote catalog "${WEAVE_AUTH_OPTIONS[@]}" --output json
```

`WEAVE_AUTH_CONFIG` is a convenience variable in this guide; it is not an
automatically discovered CLI setting. Pass `"${WEAVE_AUTH_OPTIONS[@]}"` on each
remote command that should use the saved login; the array also preserves the
private-file settings if you chose that fallback.

### Option B: your team supplies a current access token

Keep the token out of command history and terminal output. This Bash/Zsh command
prompts without echoing the value:

```sh
export WEAVE_ACCESS_TOKEN="$(python3 -c 'import getpass; print(getpass.getpass("Access token: "))')"
weave remote catalog --output json
```

The CLI reads `WEAVE_ACCESS_TOKEN` for remote commands without `--auth-config`.
It does not refresh a token supplied through this variable. Obtain a new token
through your team's approved flow when it expires.

## 4. Recognize success and resolve errors

Expected: a JSON catalog of the project's available definitions and contracts.
An empty catalog can be valid for a new project. This command reads data; it does
not publish a workflow, create a tenant, or start a run.

| Result | Next action |
| --- | --- |
| Catalog JSON and exit 0 | Your CLI can read this project; continue below |
| Authentication error / 401 | Check issuer, API target, token expiry, and the selected login method |
| Access denied / 403 | Ask the operator to check your identity link and project grants |
| Invalid configuration / exit 2 | Check UUIDs, URL, configuration file, and that client dependencies are installed |
| Connection or server error / exit 3 | Confirm the origin and connectivity with the operator |

Read-only access does not imply permission to publish, activate, or execute.
Have the operator grant the particular operations you need.

## 5. Choose your next action

- Author locally with the [first workflow tutorial](../quickstart.md).
- Inspect available commands with `weave help definitions` and `weave help runs`.
- Follow the [CLI lifecycle tutorial](cli-tutorial.md#use-these-commands-with-an-existing-api)
  to publish, activate, and execute using this connection.
- Integrate your application with the [Python SDK](../reference/sdk.md).

When finished with a supplied token, run `unset WEAVE_ACCESS_TOKEN`. For a saved
login, use `weave auth logout "${WEAVE_AUTH_OPTIONS[@]}"`; this clears the
CLI's local credentials. Provider-side revocation is a separate supported option
(`--revoke`), described in the [CLI reference](../reference/cli.md).
