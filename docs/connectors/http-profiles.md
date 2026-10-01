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

# HTTP profiles v2

Use this reference when a workflow must call an HTTP operation such as reading
an inventory item. For a guided first result, [import the inventory example](metadata-import.md#try-the-local-inventory-example),
then return here to configure its connection and Action. This page explains the
supported request/response policy; it does not create an API account or start a worker.

![Fixed HTTP connection and Action policy with bounded invocation values](../diagrams/integrations-http-profile.svg)

**How to read this diagram:** Read from the three inputs into the native execution gate. Invocation values fill declared fields; the connection and published Action retain authority over origin, authentication, effect, and response policy.

## Build a bounded HTTP operation

Use this profile when a Workflow Action should call one fixed HTTP operation,
such as reading an inventory item. The **Connection** supplies the origin and
authentication; the **Action** supplies method/path/status rules; each invocation
supplies only declared parameter values and body. Use an [existing API](../guides/connect-to-api.md) or [standalone setup](../guides/standalone.md),
then the [publication and worker sequence](authoring.md#from-package-to-an-executable-workflow)
before trying a real call.

For a first offline example, run [the OpenAPI importer](metadata-import.md#try-the-local-inventory-example).
It generates a GET Action for `/v1/items/{id}`. Its invocation input is
`{"path":{"id":"item-123"}}`; a successful illustrative output is
`{"status":200,"body":{"name":"Widget"}}`. The generated response schema requires
`name`; an arbitrary successful JSON response is not enough.

These are the complete connection and operation objects for the same anonymous
profile (the operation object belongs under Action `spec.implementation.config`):

```json
{
  "name": "inventory",
  "connector_version_id": "00000000-0000-4000-8000-000000000001",
  "config": {"baseUrl": "https://inventory.example.test", "auth": {"kind": "none"}},
  "secretRef": {},
  "allowed_destinations": ["https://inventory.example.test"]
}
```

```json
{
  "profileVersion": "2.0.0",
  "method": "GET",
  "path": "/v1/items/{id}",
  "sideEffect": "read_only",
  "parameters": [{"name": "id", "location": "path", "type": "string", "required": true}],
  "statuses": [200],
  "emptyStatuses": []
}
```

Replace the UUID with the published Connector version ID. The `.test` origin is
an offline example, not a deployed service. For an imported package, copy its
exact generated connection config and Action rather than changing these objects
independently of their immutable schemas.

If admission fails, compare auth kind/secret slot names and allowed origins first.
If a run is queued, check native worker release admission and capacity. If a call
fails after dispatch, inspect the run incident's outcome: an `unknown` write may
already have happened. [Incident operations](../reference/incident-operations.md)
explains reconciliation and permitted retry.


`weave-http@2.0.0` uses adapter identity `weave-http-v2` and task/implementation version `2.0.0`. The original `weave-http@1.0.0` descriptor, adapter and active pins retain their behavior. The v2 identity is additive because the registry binds one descriptor to one adapter identity. Generated custom packages use the same native v2 executor with exact operation-specific schemas and capabilities.

The native `HttpProfileConnector` injects the bounded HTTP port, operator `HttpPolicy` and shared `MachineTokenService`. `register_http_profile_services(context)` registers code-owned native classes only and preserves an existing machine-token singleton. Hosts supply the existing bounded port and policy; no manually constructed adapter graph is needed. Core composition and explicitly invoked installed authoring conformance use this seam. Offline import does not import these runtime dependencies.

Connection configuration contains a fixed HTTPS origin in `baseUrl` and an `auth` profile. Action configuration fixes `profileVersion: "2.0.0"`, method, relative template, effect, declared parameters, success statuses and empty-body statuses. An Action input cannot change origin, authentication, template, headers or status policy. Each operation's full configuration is part of its immutable pin.

| Auth kind | Nonsecret profile | Exact secretRef slots |
| --- | --- | --- |
| none | kind only | none |
| api-key | kind and header name | api_key |
| basic | kind | username, password |
| bearer | kind | token |
| machine-token | kind, client_id, endpoint, scopes, authentication | client_secret |

Machine authentication is `client_secret_post` or `client_secret_basic`. The endpoint and exact scope set are immutable; the shared PyFly-backed machine service owns OAuth parsing, bounds and cleanup. Tokens are opaque, single-use and never cached across invocations. There is no separate audience/resource parameter or interactive/refresh-token implementation. Basic username cannot contain a colon; all secret header values are bounded and cannot contain controls. Missing/surplus slots fail. Credential values never belong in config, Action input/output or logs.

Every call validates input/config, checks current authority, resolves only the selected slots and rechecks authority immediately before dispatch. Paths encode each substituted segment once; slash, percent, query and fragment delimiters cannot replace the origin. Empty/dot/dot-dot path values fail. Query arrays use repeated encoded keys. Protected/auth/transport headers cannot be Action parameters. Request byte accounting includes URL, headers and body. Normal JSON schema validation remains authoritative alongside transport budgets.

The existing pinned-DNS, pre-write peer, destination and TLS policies apply. Private networks require explicit host policy. Ambient proxies are disabled. All v2 redirects are rejected, including read redirects; no credential is forwarded to a redirect. There are no hidden retries. Acquisition, I/O and response processing share the Action deadline; cancellation reaches existing worker settlement. A non-idempotent call whose acknowledgment is lost, invalid, oversized or sensitive returns an unknown outcome after dispatch.

Outputs contain only status and validated JSON body (or null for declared empty responses). Response headers and raw error bodies are not returned. Credential/token echoes in keys or nested values are withheld. The shared diagnostic guard contains owned HTTP-library records; operators still control reverse proxies and arbitrary instrumentation.

`test_connection` checks configuration/slot policy without acquiring credentials or sending requests. Its `ok` means that local configuration passed, not that a provider authenticated or a destination is reachable. Installed `connector test --mode native` likewise tests composition, not a remote account.

Verification catalog: v2 serialization, auth-slot/authority boundaries, response/outcome rules and generated native package TLS execution are covered by local contract and TLS integration tests. No external account was contacted and no live compatibility is claimed. See [offline metadata import](metadata-import.md) for the supported OpenAPI subset and explicit review/build/install flow.
