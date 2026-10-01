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

# Integrate Weave into a host product

Choose the boundary that fits the host. All three approaches share the same
language and contracts; they do not share implicit administrative authority.

| Approach | Use it for | Host responsibility |
| --- | --- | --- |
| Pure compiler and immutable builder | Authoring tools, validation and previews | Supply definitions/catalogs; preserve diagnostics; no server resources are started |
| Typed SDK or native HTTP API | A separate host application or service | Obtain a verified access token, provision local identity/grants, choose scope and manage revision/idempotency contracts |
| In-process native services | A trusted Python application composition | Preserve constructor injection, explicit actor/scope/audit/UoW and service authorization; do not retain request state in singletons |

The [embedding reference](../reference/embedding.md) includes a runnable pure
builder example. The [SDK reference](../reference/sdk.md) shows the typed remote
client, token callback and errors. Use the [API reference](../reference/api.md)
and [native OpenAPI export](../reference/native-openapi.md) for canonical requests.
Remote SDK/CLI dependencies come from the `client` extra; the pure compiler does
not require the server, database or identity provider.

A host must not bypass domain services by writing orchestration tables. A provided
transaction is enlisted under the existing UoW contract, while local authorization
and transaction-local tenant context remain mandatory. Services must not capture
a request's actor, database session or token in long-lived state.

The standalone deployment uses Keycloak initially. An embedding host can implement
the provider-neutral verifier/resolver ports, but must preserve verified-token and
explicit local-principal semantics. [Identity and secrets](../operations/identity-and-secrets.md)
explains what is implemented and what has not been verified with another CIAM.

Handle uncertain transport results separately from domain rejection. An SDK retry
cannot prove an external request had no effect. Preserve idempotency keys and
revision expectations, inspect receipts/history and reconcile unknown outcomes.
