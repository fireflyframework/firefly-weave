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

# Configuration

Use this page to decide **which process needs which settings**. Start with the
[standalone walkthrough](../guides/standalone.md) for a working local installation;
it creates the private files referred to here. Set configuration before starting
a process, then use readiness and an authorized request to verify the result.

## Choose the right configuration file

| File from the local walkthrough | Purpose | Who should receive it |
| --- | --- | --- |
| `session.env` | Restores nonsecret paths, context/project, and selected ports in another terminal | Every terminal in this local walkthrough |
| `postgres.env` | Starts the owned PostgreSQL service and identifies its provisioning database | Operator shell and PostgreSQL Compose setup |
| `identity.env` | Starts local Keycloak and contains its generated client credentials | Operator shell and identity Compose setup |
| `runtime.env` | Names the new runtime database and its application, scheduler, and migration logins | Operator shell; remove migration authority when launching the API |
| `api-container.env` | Container-reachable database and identity settings | API container only |
| `native-container.env` | Database settings, exact native release, and connector egress policy | Native executor container only |
| `worker.env` | Six API, token, release, and effect settings | Remote worker container only |

The first four files come from standalone setup; the last three are created in
[deployment](deployment.md). The shell-sourceable setup files and Compose's raw
container env files have different quoting rules. Follow their generation recipes
rather than copying one file wholesale into another process.

`postgres.env` includes a provisioning `WEAVE_DATABASE_URL`. In the operator
shell, load `runtime.env` **after** `postgres.env` so runtime commands use the
nonowner application login. Never pass the entire operator environment to a
remote worker.

## Server environment reference

Settings are loaded explicitly by the server entry point. Importing the compiler
never loads a database, identity provider or secret resolver. The authoritative
current models are [Settings](../../src/firefly_weave/settings.py),
[OIDC provider configuration](../../src/firefly_weave/access/oidc.py), and the
[broker policy](../../src/firefly_weave/connectors/broker.py).

| Environment variable | Default / requirement | Consumer and meaning |
| --- | --- | --- |
| `WEAVE_DATABASE_URL` | Required | Complete `postgresql+asyncpg` URL for nonowner application authority; secret |
| `WEAVE_MIGRATION_DATABASE_URL` | Explicit migration/bootstrap only | Migration owner URL; remove from runtime processes |
| `WEAVE_SCHEDULER_ENABLED` | `true` | Boolean; runtime recovery/scheduling ownership |
| `WEAVE_SCHEDULER_DATABASE_URL` | Required for startup compatibility inventory, including API-only processes | Separate execute-only catalog/scheduler URL; secret |
| `WEAVE_OIDC_PROVIDERS` | `[]` | JSON array of explicit provider verification profiles; no automatic discovery |
| `WEAVE_CONNECTOR_PACKAGES` | `[]` | JSON array selecting installed trusted connector entry points |
| `WEAVE_NATIVE_EXECUTORS` | `[]` | JSON executor admission configuration; an empty list grants no worker authority |
| `WEAVE_NATIVE_IMAGE_DIGEST` | Absent | Admitted `sha256:` build identity for native execution |
| `WEAVE_SECRET_GRANTS` | `[]` | JSON scoped grants for secret references; never put secret values here |
| `WEAVE_SECRET_ROOT` | Absent | Enables the mounted-file secret provider rooted at this path |
| `WEAVE_HTTP_PRIVATE_NETWORKS` | `[]` | Explicit private-network allowance for HTTP egress; empty permits no private exception |
| `WEAVE_POSTGRES_PRIVATE_NETWORKS` | `[]` | JSON private-network allowance for connector targets |
| `WEAVE_POSTGRES_PLAINTEXT_NETWORKS` | `[]` | Explicit plaintext PostgreSQL network exception |
| `WEAVE_POSTGRES_CA_FILE` | Absent | Trusted CA file for PostgreSQL connector TLS |
| `WEAVE_POSTGRES_MAX_CONNECTIONS` | `8`, range 1–64 | Connector connection bound |
| `WEAVE_BROKER_POLICY` | `{}` | Disabled/default broker profile until explicitly configured |
| `WEAVE_KAFKA_CONSUMER_ENABLED` | `false` | Requires the broker profile to be enabled |
| `WEAVE_OPERATIONS_POLICY` | `{}` | JSON operational policy; positive reductions from fixed per-project ceilings |
| `WEAVE_TELEMETRY` | `{}` | Explicit, disabled-by-default OTLP HTTP/protobuf export; see [observability](observability.md) |

The `Settings` model also has programmatic scheduler poll, database timeout and
shutdown timeout fields. The current `from_env()` does not expose environment
variables for those fields; do not invent names by convention. Configuration is
read at application creation, not dynamically watched for changes.

## Separate authorities and processes

The [runtime Compose overlay](../../compose.runtime.yaml) selects three distinct env files with
`WEAVE_API_ENV_FILE`, `WEAVE_NATIVE_ENV_FILE`, and `WEAVE_WORKER_ENV_FILE`.
The API needs its application database, identity configuration, and separate
catalog/scheduler authority for startup compatibility checks. Native executors additionally need exact
release/build configuration and granted secret handles. A remote worker needs
only its API/identity/effect settings, not orchestration database credentials.

Bare `compose.yaml` defaults PostgreSQL to localhost 55432; the standalone
walkthrough explicitly selects 55434 so its guarded backup/restore recipe can
use the same installation. Keycloak defaults to localhost 18080, and the
standalone foreground API defaults to port 8080; see
[deployment](deployment.md) for container ports and network configuration. Select an explicit Docker
context/project and nonconflicting ports for your owned deployment. Container
reachable JWKS/database hosts and exact token issuer are different configuration
values; changing a network route must not silently change issuer verification.

[Local setup](../reference/local-runtime.md) writes owner-only files and refuses
replacement. [Identity and secrets](identity-and-secrets.md) explains grants and
trust profiles. Connector-specific schemas/policies remain in their guides; a
validated configuration or `test_connection` response does not establish remote
credentials, permissions, or delivery.

## Operational policy

`WEAVE_OPERATIONS_POLICY` accepts the fields in
[OperationsPolicy](../../src/firefly_weave/contracts/operational_policy.py).
Omitted fields retain their defaults. Values must be positive and may only lower
built-in limits; zero does not mean unlimited. The policy covers per-project
retained and active work, pending messages, source records, and logical storage.
Logical byte accounting is not a physical disk quota: PostgreSQL indexes, WAL,
backups, and storage overhead still need infrastructure capacity planning.

The ordinary and control pools default to 4 GiB each. Control capacity is reserved
for terminal and revocation operations; it does not increase ordinary admission
capacity. See [retention](retention.md) for retained usage and cleanup behavior.

Ordinary runtime admission bounds serialized state to 32 MiB and a transition to
64 MiB. These limits do not prevent cancellation or due terminal timeouts for a
supported historical run created under an earlier policy. Terminal controls
retain the full durable state and history, fence outstanding work, and may return
a bounded [capacity acknowledgment](../reference/api.md) instead of the state.
They do not authorize further ordinary execution or increase its admission limit.
Serialized byte limits are separate from process memory and database disk usage.

Each API process also bounds request-body reception to 16 receivers and 128 MiB
in total. Ordinary traffic can use 12 receivers and 96 MiB; four receivers and
32 MiB remain reserved for control and inspection requests. These pools cannot
borrow from each other. The capabilities response exposes the total and reserved
control ceilings. Saturation returns a bounded capacity error rather than
queuing an unbounded number of request bodies.

Use the same policy when migrating and starting every replica. Its fingerprint is
checked against database policy metadata; a mismatch prevents readiness. A
smaller policy is an explicit configuration change, not a way to erase existing
usage. Follow [upgrades](upgrades.md) before applying it to an existing database.

For a **new database being provisioned with this policy**, this JSON lowers
active runs while retaining all other defaults. This is not a live tuning command
for the database already created by the standalone setup helper:

```sh
export WEAVE_OPERATIONS_POLICY='{"runs_active":500}'
```

Configuration is read when creating the application. Restart the owned process
after changing its private environment. Inspect capabilities and compatibility
through the authorized API to confirm the policy and readiness observed by that
replica.

## Apply a configuration change

1. Identify the process that consumes the setting from the table above. For
   example, changing `worker.env` cannot change API authorization policy.
2. Check the value against the referenced model. Lists and objects are JSON;
   booleans exposed by `from_env()` accept `true` or `false`.
3. Update the protected deployment configuration through its normal lifecycle.
   Generated local setup files are creation receipts; do not rerun setup scripts
   as a way to rotate or replace them.
4. Restart the intended foreground process, or deliberately replace its owned
   container with the new configuration. Restarting an existing container does
   not reread its Compose env file. Preserve the previous configuration for
   diagnosis and follow [deployment](deployment.md#stop-the-intended-scope).
5. Check `/health/ready`, then inspect the authorized compatibility/capabilities
   response and run the operation affected by the setting. Readiness alone does
   not test a remote connector credential or collector endpoint.

If startup reports a missing database URL, check the chosen env file and export
behavior first. If it starts but remains restricted, inspect catalog authority and
policy consistency using [upgrades](upgrades.md#start-and-inspect-compatibility).
Do not replace application credentials with the migration owner to bypass either
failure.
