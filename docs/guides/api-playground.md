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

# Explore the API and make your first request

The API is the HTTP interface used by the CLI and Python SDK. Swagger UI lets you
inspect its exact request and response shapes and send a request from your browser.
The [full API reference](../reference/api-explorer.md) is available without running
Weave. It includes every operation and schema, plus a downloadable OpenAPI file.

## Open your installation's explorer

Start the [local platform](local-platform.md), then open the `/docs` address printed by
the platform command. For an existing installation, use the API origin supplied
by its operator and append `/docs`. The explorer belongs to that running API;
requests go to that same origin, with no cross-origin browser setup.

Operators running the API directly can enable the explorer before starting it:

```sh
# After restoring the manual guide's private session and runtime settings:
# WEAVE_PYTHON selects its installed server environment.
# WEAVE_API_PORT is the unused loopback port selected during setup.
export WEAVE_DOCS_ENABLED=true
env -u WEAVE_MIGRATION_DATABASE_URL \
  "${WEAVE_PYTHON:?Restore the manual server session}" -I -m uvicorn \
  firefly_weave.main:create_application --factory --host 127.0.0.1 \
  --port "${WEAVE_API_PORT:?Set the selected API port}"
```

This assumes the server's database, identity and other required settings are
already configured by the [manual server setup](standalone.md). See
[configuration](../operations/configuration.md) for deployment settings.
The setting defaults to `false`. Enabling it publishes only the documentation
page and `/openapi.json` for anonymous reading. API operations retain their usual
authentication and scoped grants. Leave it disabled where public discovery is
unwanted. Proxies mounted beneath a path must set the ASGI `root_path` correctly.

The browser downloads pinned Swagger UI assets from jsDelivr and checks their
integrity. It needs network access to that CDN. If those assets cannot load,
`/openapi.json` and the published reference remain available. The explorer does
not send the contract to an online validator.

## Supply an access token and your project IDs

For an existing server, your operator supplies the API origin, tenant UUID,
project UUID and an approved way to obtain a current access token. Follow
[Connect to an API](connect-to-api.md) for those prerequisites. For the local
platform, follow its printed connection instructions. Tenant and project IDs
come from Weave provisioning; they are not names you choose in Swagger UI.

Choose **Authorize**, select the `bearer` scheme and paste just the current access
token, without the `Bearer ` prefix. Swagger adds the authorization header.
Close the dialog. Tokens remain in this page's memory; refreshing or closing it
clears that authorization. The browser explorer does not read your CLI credential
store or refresh expired tokens. Obtain a new token through the approved login
flow when needed, then authorize again.

The token identifies a principal. That principal also needs grants in the selected
tenant/project. A valid token without those grants still receives `403`.

## Try a compiler request

Compiling checks a workflow and returns an artifact. It does not publish a
workflow, activate a version or start a run. You need the `compile` capability in
the chosen project.

1. Filter for `compiler.compile`, then expand **POST**
   `/api/v1/tenants/{tenant}/projects/{project}/compiler/compile`.
2. Choose **Try it out** and enter the tenant and project UUIDs.
3. Replace the example request body with this JSON, then choose **Execute**:

```json
{
  "format": "object",
  "source": {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "browser-echo", "version": "1.0.0"},
    "spec": {
      "inputSchema": {"type": "object"},
      "outputSchema": {"type": "object"},
      "steps": [
        {"id": "echo", "kind": "transform", "value": {"ref": "/input"}}
      ],
      "output": {"ref": "/steps/echo/output"}
    }
  }
}
```

Expected: HTTP `200`, `ok: true`, an `artifact`, and no error diagnostics. Expand
**Schemas** or an operation's request/response model to inspect its fields. The
compiler endpoint needs no environment ID, `Idempotency-Key` or `If-Match` header.

| Result | What to do next |
| --- | --- |
| `401` | Check the token's issuer, API target and expiry; authorize again |
| `403` | Check the principal's identity link and `compile` grant for these IDs |
| `422` or compiler diagnostics | Check the JSON body and definition against the displayed schema |
| Explorer unavailable | Ask the operator whether documentation is enabled and confirm the API origin |
| Browser connection error | Confirm the API is reachable and inspect the request URL |

## Understand headers before trying write operations

Some endpoints publish versions, start runs, or modify grants. Read each operation
before executing it: these requests act on the running installation.

| Header | When and how to use it |
| --- | --- |
| `Authorization` | Swagger supplies `Bearer <token>` after authorization |
| `Idempotency-Key` | Supply a unique key for a new logical operation when its contract requires one; reuse it with the identical request only when retrying that operation |
| `If-Match` | For revision-sensitive changes, copy the quoted `ETag` returned when reading or creating that resource; a stale revision is rejected |
| `X-Weave-Request-ID` | Returned by Weave; retain it to investigate a failed request without sharing your token |
| `X-Weave-Wire-Version` | Returned by Weave to identify the response contract |

The operation's schema lists required headers, path IDs, success status codes and
structured errors. Signed webhook/provider ingress uses separate authentication;
the bearer dialog does not generate provider signatures. See
[HTTP and webhooks](../reference/http-and-webhooks.md).

Choose **Authorize → Logout** when finished, or close the tab. Continue with the
[CLI tutorial](cli-tutorial.md) to publish and run your workflow, or the
[Python SDK reference](../reference/sdk.md) to integrate an application.
