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

# Local PostgreSQL and Keycloak runtime

The API uses native PyFly controllers, a native request filter and Starlette.
Keycloak access tokens resolve through explicit local identity links. No static
service-key authentication, provider passwords, auto-mounted IdP endpoints or
implicit startup migrations are enabled.

For the complete setup sequence, follow the [standalone quickstart](../guides/standalone.md).
It covers dependency installation, PostgreSQL and Keycloak readiness, generated
private configuration, identity bootstrap, grants, and the first workflow. Use
[deployment](../operations/deployment.md) to launch API and worker containers.
This page explains runtime authority and lifecycle behavior.

The setup scripts refuse to replace existing secret files. Reuse the intended
configuration when a file already exists. Generated environment files and
bootstrap receipts belong in protected local storage, outside source control.
The development services bind their published ports to loopback. Container JWKS
routing and the token's issuer are separate settings; use the documented Compose
configuration rather than changing the issuer to match a container hostname.

`setup-runtime.py` guards the task-owned control backend before creating a fresh
retained database and a generated login inheriting the nonowner `weave_app`
role plus an execute-only scheduler login. The selected private output file
(`$WEAVE_WORK_DIR/runtime.env` in the standalone guide) contains separate application,
scheduler and migration URLs. Runtime
startup rejects superuser, BYPASSRLS and public-table owner identities. Production
must provide external secrets and HTTPS issuer/JWKS endpoints; development
HTTP is accepted only for explicitly configured localhost endpoints.

Use the selected installed interpreter and private configuration from the
standalone guide for an explicit migration:

```sh
set -a
source "$WEAVE_WORK_DIR/runtime.env"
set +a
"$WEAVE_PYTHON" -I -m firefly_weave.cli.main admin migrate
```

Alembic is the migration authority: `0001_boot` adopts a compatible initial database,
then `0002_access` creates identity/access state and `0003_access_audit` adds
structured audit payloads without backfilling historical provenance. The legacy version row is a
compatibility sentinel updated by the revision; startup checks it and Alembic's
revision together. Startup never migrates. The migration command requires
`WEAVE_MIGRATION_DATABASE_URL`; no destructive downgrade/reset is implemented.
Migration assets are included in installed wheels. The expected schema head is
`0021_operations`. See [upgrades](../operations/upgrades.md) for compatibility
checks and the forward-migration procedure.

Follow [verified identity bootstrap](../guides/standalone.md#3-verify-and-bootstrap-one-local-identity)
to link an existing provider identity with explicit migration credentials.

The administrator supplies the exact trusted provider subject, never an email,
username or client ID. Keycloak service-account subjects are user UUIDs; obtain
one from the trusted administrative interface or a verified access token. This
command links it as platform administrator and writes a private receipt. It
does not mint a Weave password/key/token. It requires the migration table-owner
identity, audits bootstrap, and rejects application-role sessions. Platform administration does not imply
business-data read or workflow authoring; provision those grants explicitly.

Provisioning routes are `POST /admin/tenants`, `POST /admin/grants`,
`POST /api/v1/tenants/{tenant}/projects` and
`POST /api/v1/tenants/{tenant}/projects/{project}/environments`. The corresponding GET
environment route requires `status.read`. Requests use `Authorization: Bearer`
with the Keycloak token. All routes except exact health probes require a linked
active principal; an authenticated unknown route returns 404. Role checks also
run inside services. `weave-host` is an application actor, `weave-worker` resolves
to its separately provisioned worker principal. Neither is inferred to be human.

The realm template disables password and implicit grants. The CLI client enables
PKCE S256 and device authorization; token acquisition/storage use the [CLI login commands](cli.md#login-and-secure-persistence).
Audience and subject mappers are explicit, and the API-client role mapper is
restricted to `weave-api`. External roles describe claims only; local scope
bindings grant capabilities. The template begins with no provider role assignment
and no Weave scope grant. Grant service methods prevent tenant administrators
from delegating outside their administered tenant/project/environment/resource
ceiling. Host products can receive developer/deployer/operator grants; workers
cannot acquire authoring behavior even from an erroneous developer grant.

Startup realm import skips existing realms. Editing the template does not update
retained realms or rotate secrets. Inspect existing client flows, subject/audience
and role mappers before acceptance; reconcile intentional additions through the
explicit Keycloak administrator API. Never run overriding import or reset a realm
as setup. The generated temporary bootstrap admin service account remains local
and retained; remove or replace it only through an explicitly authorized lifecycle
operation. Its secret is not passed to the Weave runtime or request verifier.

JWKS fetches use only configured URLs, no redirects, ambient proxies or token-
selected discovery. Defaults: 5-second timeout, 300-second freshness, 5-second
refresh cooldown, at most 64 accepted keys, 256-KiB response and 32-KiB token.
Known keys work during an outage only until their original deadline; failures
never extend freshness. Unknown kids share a single coordinated refresh and
cooldown, so rotation inside the cooldown may temporarily deny until retry.
Keycloak requires signed payload `typ=Bearer`, configured issuer/audience/client,
nonempty subject and expiry, and RS256 by default. JOSE `typ=JWT` alone is not an
access-token discriminator. Generic/Entra-style profiles are configurable claim
normalizers; live Entra provisioning and verification are not claimed.

Local disable/grant removal applies on the next resolution, independent of JWT
expiry. Offline verification does not detect external provider revocation before
expiry; configure a future explicit introspection/revocation integration if that
is required. Principals supplied to a direct service are immutable request-time
snapshots; resolve again for a new operation instead of retaining them indefinitely.

PostgreSQL tenant tables use FORCE RLS and composite scoped foreign keys. UoW
binds `weave.tenant_id` transaction-locally with parameterized `set_config`;
commit, error and cancellation reset context. Authentication separately binds
`weave.principal_id` to enumerate only that linked principal's role bindings.
Identity/platform tables are global authorization state, never business data.
`weave_app` can read/update that state through checked application services;
audit is insert-only. The scheduler login has schema usage and execute permission
on explicitly granted catalog functions, with no direct business-table read.
Bounded scheduler functions use a dedicated `weave_catalog_reader` owner;
compatibility inventory functions use `weave_retention_owner`. Function ownership,
fixed search paths, explicit grants, and dedicated RLS policies are part of the
migration contract. The runtime does not use the migration owner's credentials.
New business migrations must add FORCE RLS, scoped foreign keys, and explicit
application grants.

Startup requires scheduler catalog authority for compatibility inventory even
when `WEAVE_SCHEDULER_ENABLED=false`. Disabling background scheduling does not
bypass compatibility checks. Missing authority, an incomplete inventory, or an
incompatible persisted requirement keeps readiness restricted and blocks new
effect-producing work. Inspect the scoped compatibility report and follow the
[upgrade guide](../operations/upgrades.md) before admitting work.

For contributor-only backend checks and fixture prerequisites, see
[contributing](../../CONTRIBUTING.md#backend-and-end-to-end-checks). Those tests use
fresh guarded databases and separate application/migration identities. They are
separate from the public startup journey and do not certify external provider
accounts or production infrastructure.

All databases/roles/volumes remain retained. Safe stop:

```sh
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml stop --timeout 30
```

Stop only the services belonging to your deployment. Stopping services preserves
stored workflow state; deleting databases or volumes is a separate destructive
operation. Owned telemetry and database cleanup are independently attempted after
failed startup or shutdown.

Weave supplies native PyFly beans for its OpenTelemetry providers. Export is
disabled by default and is enabled only through explicit `WEAVE_TELEMETRY`
configuration. Providers remain local to the application; ambient OTLP endpoints
do not enable export. See [metrics and tracing](../operations/observability.md)
for collector setup, data limits, and troubleshooting.

Authorization audit decisions are JSON messages on the explicitly INFO-enabled
`weave.authorization` logger while root remains WARNING. `AuditContext` carries
immutable UUID operation/request/run correlation. Native requests create a server
request ID, return `X-Weave-Request-ID`, and pass `request.state.audit_context` as
`context=` to services; arbitrary incoming request-ID values are not copied.
Background callers should construct their own context and pass any known run UUID.
Administrative success rows store scope, verified actor reference (or null when
unavailable), capability, correlation and relevant binding/identity details in
`access_audit.event`. Old rows remain null; no historical identity is invented.
Decision-log shipping and retention must be configured by the deployment owner.


## Runtime version and lifecycle

The locked framework is published PyFly 26.9.15. Read the exact wheel hash and
upstream commit from [project metadata](../../pyproject.toml). The API source
version is `0.1.0a1`. Validate readiness, an authorized workflow, and your
[backup and restore procedure](../operations/backup-restore.md) in the environment
you intend to operate.

The setup helper deliberately supports a guarded local test control database and
localhost Keycloak profiles; it is not a general production provisioning tool.
Wait for the owned PostgreSQL/Keycloak services to be healthy before running it.
It creates and migrates a new retained runtime database with separate identities.
For existing runtime databases, apply forward migration explicitly and retain
backup/recovery planning; startup never performs that operation implicitly.

See [configuration](../operations/configuration.md), [identity and secrets](../operations/identity-and-secrets.md),
[workers](../guides/workers.md), [capabilities](../capabilities.md) and
[troubleshooting](../operations/troubleshooting.md).
