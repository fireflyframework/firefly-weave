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

# Identity, authorization and secrets

The standalone API initially uses Keycloak. A verified external identity resolves
to an explicitly linked local principal; local role bindings grant capabilities
within tenant/project/environment/resource scope. External roles are claims, not
a replacement for these bindings. Workers cannot become workflow authors merely
because a provider token contains a role string.

![Verified identity, local grants, scoped transactions and secret authority](../diagrams/security-boundaries.svg)

[Open diagram at full size](../diagrams/security-boundaries.svg)

## Configure token verification

Each `WEAVE_OIDC_PROVIDERS` entry supplies `provider_id`, exact `issuer`, trusted
`jwks_uri`, API `audience`, and an explicit client-to-actor-kind map. Defaults
allow RS256 and require the signed payload's `typ` to be `Bearer`, using `azp`
for client identity. Supported algorithms are explicitly allowed; JOSE header
`typ=JWT` alone does not establish an access token. A human and an application
are distinct actor kinds. HTTPS is required except explicitly enabled localhost
development endpoints.

JWKS retrieval uses configured trust, bounded responses/cache/refresh behavior
and no token-selected URL. Offline signature verification cannot instantly detect
an IdP-side revocation before token expiry. Local disable/grant removal is checked
on subsequent resolution. Do not retain a resolved principal indefinitely across
operations or share request tokens/state in singleton services.

The Keycloak realm template enables S256/device flows for the CLI and disables
password/implicit grants. Existing realm import skips retained realms; changing
a template does not reconcile existing clients or rotate secrets. Apply deliberate
additive administration to an owned realm. See [local runtime setup](../reference/local-runtime.md)
and [CLI login/storage](../reference/cli.md#login-and-secure-persistence).

## Bootstrap and grant deliberately

The explicit bootstrap command requires migration-owner authority and the exact
verified provider subject, not an email, username or client ID. It links a platform
administrator; it does not mint a credential or grant business-data access.
Provision tenant/project/environment and host/worker grants through the existing
administrative API. Services enforce authorization even when called in process.

The UoW sets transaction-local `weave.tenant_id` for PostgreSQL RLS. Scoped foreign
keys and service checks preserve finer boundaries. Migration authority owns DDL;
ordinary app sessions reject superuser/BYPASSRLS/table-owner credentials. Scheduler
authority is separately provisioned and does not imply unrestricted business-table
read access. Do not give remote workers either database login.

## Provider portability

| Boundary | Current support | What remains deployment-specific |
| --- | --- | --- |
| `AccessTokenVerifier` | Verified-identity port | Signature, issuer/audience/client and token-purpose policy |
| `ClaimsMapper` | Keycloak/generic/Entra claim profiles | Exact provider claim shape and allowed mapping |
| `PrincipalResolver` and identity links | Explicit local-principal mapping | Provisioning lifecycle and scope grants |
| Entra / other CIAM | Local profile/port support | Live provisioning and verification are not certified; no Entra group-overage expansion |

Embedding another verifier/resolver must retain these contracts. A generic mapper
is not proof that a provider's ID token is safe as an API access token or that its
roles should grant local administration. See [host integration](../guides/host-integration.md).

## Credentials and side effects

Use connection `secretRef` handles and explicit scoped grants. Environment and
mounted-file secret providers resolve credentials only under the relevant authority;
provider standing-source requirements and worker lease-bound requirements differ.
Follow the exact connector guide rather than sharing one universal token.

Never place credential values in definitions, workflow literals, task results,
logs, metrics, trace labels, error messages or public examples. Secret-classified
ordinary durable payload values are rejected; this does not erase secret-bearing
historical rows in an older deployment. Read redaction is not storage encryption.
Explicit egress and destination policy still applies after credentials resolve.

Provider acceptance is not delivery. Lease fencing, current grants and reference
revocation protect admission/state; they cannot undo a remote effect already sent.
See [security reporting](../../SECURITY.md) and [capability limits](../capabilities.md).
