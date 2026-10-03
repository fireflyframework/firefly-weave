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

# HTTP profile v2 reference

This reference explains the exact request and response policy that every Action
on the built-in `weave-http@2.0.0` connector follows, and every check that
enforces it. Use it when you review an Action, design a connection, or read a
failure code. It is for integration developers, reviewers, and operators.

For a guided first result, follow
[Call a REST API without code](http-without-code.md) instead: it builds an Action
from command-line flags or an OpenAPI document and runs it on the local platform.
This page does not create an API account or start an executor.

![Fixed HTTP connection and Action policy with bounded invocation values](../diagrams/integrations-http-profile.svg)

**How to read this diagram:** Read down from the connection and Action policy to invocation values, bounded execution, and outcome. Input fills declared fields; the connection and published Action retain control of origin, authentication, effect, and response policy.

[Open diagram at full size](../diagrams/integrations-http-profile.svg)

## How a profile call is put together

Three objects decide every call, and each one has a different owner:

| Object | Owner | What it fixes |
| --- | --- | --- |
| **Integration connection** | A person with `connection.manage`, per environment | The HTTPS origin (`baseUrl`), the authentication kind, the secret handles, and the allowed destinations |
| **Action** | The author, published once | The method, the path template, declared parameters, the side effect, accepted and empty statuses, schemas, and timeout |
| **Invocation input** | The workflow, on each run | Only values for the declared path, query, and header parameters, and the JSON body |

Before a real call, the environment also needs the published connector, an
executor release, and its grants. On the
[local platform](../guides/local-platform.md), `weave platform integrations
enable` prepares them; elsewhere, follow the
[operator steps](http-without-code.md#prepare-an-environment-once-operator) and the
[publication and worker sequence](authoring.md#from-package-to-an-executable-workflow).

`weave connector http-action` and `weave connector import-openapi --target builtin`
produce Actions for you, and `weave connections create --api-url` builds the
connection with its allowed destinations. For a first offline example of the
package target, run [the OpenAPI importer](metadata-import.md#try-the-local-inventory-example).
It generates a GET Action for `/v1/items/{id}`. Its invocation input is
`{"path":{"id":"item-123"}}`; a successful illustrative output is
`{"status":200,"body":{"name":"Widget"}}`. The generated response schema requires
`name`; an arbitrary successful JSON response is not enough.

These are the complete connection request and operation object for the same
anonymous profile. The operation object belongs under the Action's
`spec.implementation.config`:

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

## Profile rules

### Identity and versions

`weave-http@2.0.0` uses adapter identity `weave-http-v2` and task and
implementation version `2.0.0`. The original `weave-http@1.0.0` descriptor,
adapter, and active pins keep their behavior. The v2 identity is additive because
the registry binds one descriptor to one adapter identity. Generated custom
packages use the same native v2 executor with exact operation-specific schemas
and capabilities.

The Action configuration fixes `profileVersion: "2.0.0"`, the method, a path
template that starts with `/` and is appended to the connection's origin, the
effect, declared parameters, success statuses, and empty-body statuses. Each operation's full configuration is part of its immutable pin. An
Action input cannot change the origin, authentication, template, headers, or
status policy.

### Connection and authentication

The connection configuration contains a fixed HTTPS origin in `baseUrl` and an
`auth` profile. Each authentication kind needs exactly these secret slots in
`secretRef`; missing or surplus slots fail:

| Auth kind | Nonsecret profile | Exact `secretRef` slots |
| --- | --- | --- |
| `none` | kind only | none |
| `api-key` | kind and header name | `api_key` |
| `basic` | kind | `username`, `password` |
| `bearer` | kind | `token` |
| `machine-token` | kind, `client_id`, `endpoint`, `scopes`, `authentication` | `client_secret` |

- **Machine tokens.** Client authentication is `client_secret_post` or
  `client_secret_basic`. The endpoint and the exact scope set are immutable; the
  shared PyFly-backed machine service owns OAuth parsing, bounds, and cleanup.
  Tokens are opaque, single-use, and never cached across invocations. There is no
  separate audience or resource parameter, and no interactive or refresh-token
  implementation.
- **Secret values.** A basic user name cannot contain a colon. Every secret
  header value is bounded and cannot contain control characters. Credential
  values never belong in config, Action input or output, or logs.

### Request shaping

Every call validates input and configuration, checks current authority, resolves
only the selected slots, and checks authority again immediately before dispatch.

- **Paths.** Each substituted segment is encoded once, so slash, percent, query,
  and fragment delimiters cannot replace the origin. Empty, `.`, and `..` path
  values fail.
- **Query arrays.** Sent as repeated encoded keys.
- **Headers.** Protected, authentication, and transport headers cannot be Action
  parameters.
- **Size.** Request byte accounting includes the URL, headers, and body. Normal
  JSON Schema validation remains authoritative alongside the transport budgets.

### Network and transport

- The existing pinned-DNS, pre-write peer, destination, and TLS policies apply.
  Private and loopback destinations require an operator-approved range in
  `WEAVE_HTTP_PRIVATE_NETWORKS` (see [Configuration](../operations/configuration.md));
  link-local, metadata, and Kubernetes service destinations are always refused.
  Ambient proxies are disabled.
- All v2 redirects are rejected, including read redirects; no credential is
  forwarded to a redirect.
- There are no hidden retries. Acquisition, I/O, and response processing share
  the Action deadline; cancellation reaches the existing worker settlement.
- A non-idempotent call whose acknowledgment is lost, invalid, oversized, or
  sensitive returns an `unknown` outcome after dispatch.

### Outputs

Outputs contain only the status and the validated JSON body, or `null` for a
declared empty response. Response headers and raw error bodies are not returned.
Credential and token echoes in keys or nested values are withheld. The shared
diagnostic guard contains owned HTTP-library records; operators still control
reverse proxies and arbitrary instrumentation.

### Connection tests

`test_connection` checks the configuration and slot policy without acquiring
credentials or sending requests. Its `ok` means that the local configuration
passed, not that a provider authenticated or a destination is reachable.
`weave connections test ID` runs it for a saved revision; its request file is
optional. Installed `connector test --mode native` likewise tests composition,
not a remote account.

## Checks before a run

**These checks are new in 0.1.0a7.** An alpha6 or earlier CLI or platform does
not run them.

The same rules are checked at three points, so a mistake surfaces before a run
reaches the executor.

### While you build

`weave connector http-action` and the importer's built-in target check each Action
offline against the descriptor bundled with the CLI, and compile it against a
catalog holding only that descriptor. Problems are `WV-HTTP-ACTION-*` diagnostics
with a pointer into the request:

- errors such as `PATH` for an invalid template, `PARAMETER`,
  `PROTECTED_HEADER`, `BODY` for a body on `GET` or `HEAD`, and `STATUS` for an
  empty status that is not also a declared status;
- informational notes such as `PATH_PARAMETER_ADDED` (an undeclared placeholder
  became a required string) and `UNTYPED_RESPONSE`;
- the warning `SAMPLE_TRUNCATED` when a sample exceeded the inference budget;
- `WV-HTTP-ACTION-IO` for a file that is missing, malformed, or already exists.

`weave connector descriptor weave-http-v2` prints the installed descriptor from
the selected platform, or the copy bundled with the CLI (`"provenance":
"local-copy"`) when no platform is selected or with `--local`. Always publish the
manifest the platform reports.

### When you publish

The platform runs the descriptor's Action checks during `definitions.publish` and
when it compiles or validates with its catalog. A violation fails publication
with `WV-COMPILE` and one of these diagnostics, each pointing into the Action
document:

| Code | Rule |
| --- | --- |
| `WV-COMP-CONFIG_CONTRACT` | The config fits the profile field by field; `read` sends only `GET` or `HEAD`; `write` uses `non_idempotent`; path placeholders and required path parameters match both ways; protected headers and status rules hold |
| `WV-COMP-SIDE_EFFECT_CONTRACT` | `spec.sideEffect` equals the config's side effect |
| `WV-COMP-INPUT_CONTRACT` | Input groups are only `path`, `query`, `headers`, and `body`, declare and require every required parameter with a compatible type, name no undeclared parameter, and have no `body` on `GET` or `HEAD` |
| `WV-COMP-OUTPUT_CONTRACT` | Every accepted status passes `properties.status`, empty statuses accept a `null` body, and only `status` and `body` are required |

The checks apply to the Action being compiled, not to Actions a workflow merely
depends on. Schemas that use local `$ref` pointers are left to runtime
validation. Offline `weave workflow` compilation does not run these checks.

### When you create a connection

The guided `weave connections create` flags check the request locally before any
network call; a rejection exits 2 with `WV-CONNECTION-INPUT` and
`WV-HTTP-CONNECTION-*` diagnostics.

The platform rejects a connection with HTTP 422 `WV-CONNECTION` and a
`diagnostics` array. Its codes are `WV-CONNECTION-CONNECTOR`, `-CONFIG`, `-AUTH`,
`-SECRET`, or `-DESTINATION`, with pointers such as `/connector_version_id`,
`/config/baseUrl`, `/secretRef/<slot>`, or `/allowed_destinations/<index>`. An
unavailable handle always reads "This secret handle is not available in this
environment." and never names other handles.

`connections.create` accepts an optional `Idempotency-Key`: the same key and body
replay the original revision, and the same key with another body returns 409.

## Failure codes

A task that fails at run time reports one of these codes and an outcome. Before
the request is sent the outcome is `not_started`. After it is sent, a read that
fails is `failed`, and a write is `unknown`: it may have taken effect.

| Code | Cause |
| --- | --- |
| `HTTP_PROFILE_CONFIG` | The operation or connection configuration is outside the profile, or the action and side effect disagree; always `not_started` |
| `HTTP_PROFILE_AUTH` | The secret slots differ from the authentication kind, or a credential value is empty, too long, or has invalid characters |
| `HTTP_PROFILE_INPUT` | The run input fails the Action's input schema or the parameter rules, the resulting URL exceeds 16 KiB, or another check fails before the request is sent, such as current authority or a credential grant |
| `HTTP_PROFILE_LIMIT` | The headers, the whole request, or the response exceed their byte limits, or the response used an unsupported content encoding |
| `HTTP_PROFILE_TIMEOUT` | The Action deadline passed |
| `HTTP_PROFILE_UNAVAILABLE` | The connection to the destination could not be opened; always `not_started` |
| `HTTP_PROFILE_DESTINATION` | The destination is not allowed or resolves to a refused address |
| `HTTP_PROFILE_STATUS` | The status is not one of the Action's accepted statuses, including every redirect |
| `HTTP_PROFILE_OUTPUT` | The body is not JSON, an empty status carried a body, or the result fails the output schema |
| `HTTP_PROFILE_SENSITIVE` | The response echoed a credential, so it was withheld |
| `HTTP_PROFILE_FAILED` | Another failure after the request was sent |

If admission fails, compare the auth kind, the secret slot names, and the allowed
origins first. If a run is queued, check native executor release admission and
capacity. If a call fails after dispatch, inspect the run incident's outcome: an
`unknown` write may already have happened.
[Incident operations](../reference/incident-operations.md) explains
reconciliation and permitted retry.

## Verification

V2 serialization, auth-slot and authority boundaries, response and outcome rules,
and generated native package TLS execution are covered by local contract and TLS
integration tests. On the local platform, a `GET /todos/{id}` Action ran to
`succeeded` against the public demo API `https://jsonplaceholder.typicode.com`,
both when built with `weave connector http-action` and when built with Studio's
**New API action** builder; see
[Call a REST API without code](http-without-code.md). No commercial API account
was contacted and no live compatibility with any provider is claimed.

## Next steps

- Build and run a first Action: [Call a REST API without code](http-without-code.md).
- Generate a named connector package from OpenAPI instead:
  [OpenAPI connector import](metadata-import.md).
- Write custom code when the profile is not enough:
  [Author a trusted connector package](authoring.md).
