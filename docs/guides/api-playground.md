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

The Weave API is the HTTP interface that the CLI, the Python SDK, and Studio
use. Its explorer, Swagger UI, shows the exact request and response shapes of
every operation and lets you send requests from your browser. This guide is for
developers who integrate over HTTP or want to see what the clients send. You
open the explorer, send a request that needs no token, authorize, and compile a
workflow, in about ten minutes.

**Before you start, you need:**

- A running API whose explorer is enabled. The [local platform](local-platform.md)
  enables it for you; for another installation, ask its operator.
- For the authorized requests, a current access token and the UUIDs of your
  tenant and project, as described in [step 3](#3-authorize-and-find-your-ids).

Without a running API, read the [full API reference](../reference/api-explorer.md)
instead. It lists every operation and schema and offers the OpenAPI file for
download.

## 1. Open your installation's explorer

On the [local platform](local-platform.md), run `weave platform status` and
open its `Docs url`. For another installation, append `/docs` to the API origin
its operator gave you, for example `https://weave.example.com/docs`.

Expected: a page titled **Firefly Weave · API explorer** with a filter box and
the list of operations. The explorer belongs to that running API: its requests
go to that same origin, so no cross-origin browser setup is needed, and the
page refuses to send requests anywhere else.

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

This assumes the server's database, identity, and other required settings are
already configured by the [manual server setup](standalone.md). See
[configuration](../operations/configuration.md) for deployment settings.
`WEAVE_DOCS_ENABLED` defaults to `false`. Enabling it publishes only the
documentation page and `/openapi.json` for anonymous reading; API operations
keep their usual authentication and scoped grants. Leave it disabled where
public discovery is unwanted. Proxies mounted beneath a path must set the ASGI
`root_path` correctly.

The browser downloads pinned Swagger UI assets from jsDelivr and checks their
integrity, so it needs network access to that CDN. If those assets cannot load,
`/openapi.json` and the published reference remain available. The explorer does
not send the contract to an online validator.

## 2. Send a request that needs no token

Start with the one operation that anyone may read: the public sign-in settings
that `weave auth setup` and Studio use. It proves the explorer can reach the API
before you deal with tokens. This operation is new in 0.1.0a7; an alpha6 or
earlier API does not have it, so with one, skip to step 3.

1. Type `client_configuration` in the filter box, which filters by tag, then
   expand **GET** `/api/v1/client-configuration`. Its operation ID is
   `client_configuration.read`.
2. Choose **Try it out**, then **Execute**.

Expected: HTTP `200` and a JSON object with `"service": "firefly-weave"`,
`configuration_version`, `api_version`, an optional `display_name`, and a
`sign_in` list. The list is empty when the operator publishes no sign-in
settings. [Publish sign-in settings for people](../operations/identity-and-secrets.md#3-publish-sign-in-settings-for-people)
explains its fields.

## 3. Authorize and find your IDs

Every other operation needs an access token, and most need the UUIDs of your
tenant and project. Tenant and project IDs come from Weave; they are not names
you choose in Swagger UI.

| Your platform | Access token | Tenant and project IDs |
| --- | --- | --- |
| The [local platform](local-platform.md) | Run `weave platform token`, then copy the `access_token` value from `.local/platform/host-token.json` | The `scope` object in `.local/platform/first-run.json` |
| Your team's platform | Obtain one through the approved process your operator gives you | From your operator, or from the `workspace` object of `weave auth status --output json` if you [connected the CLI](connect-to-api.md) |

The CLI never prints the tokens of a saved platform, and the explorer does not
read your saved platforms or your credential store. Keep `host-token.json`
private: it is the local host application's credential.

Choose **Authorize**, select the `bearer` scheme, and paste only the access
token, without the `Bearer ` prefix. Swagger adds the authorization header.
Close the dialog. The token stays in this page's memory; refreshing or closing
the page clears it. The explorer does not refresh expired tokens: obtain a new
one and authorize again.

The token identifies a principal, Weave's record of a caller. That principal
also needs grants in the selected tenant and project. A valid token without
those grants still receives `403`.

## 4. Try a compiler request

Compiling checks a workflow and returns an artifact. It does not publish a
workflow, activate a version, or start a run. You need the `compile` capability
in the chosen project.

1. Type `compiler` in the filter box, then expand **POST**
   `/api/v1/tenants/{tenant}/projects/{project}/compiler/compile`, the operation
   `compiler.compile`.
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
**Schemas** or an operation's request and response models to inspect their
fields. The compiler endpoint needs no environment ID, `Idempotency-Key`, or
`If-Match` header.

| What you see | Why | What to do |
| --- | --- | --- |
| `401` | The token is missing, expired, or issued for another API | Check its issuer, API target, and expiry; authorize again |
| `403` | The principal has no `compile` grant in this tenant and project | Check the principal's identity link and grants for these IDs |
| `422` or compiler diagnostics | The body or the definition does not match its schema | Compare the JSON with the displayed schema and the diagnostic's `path` |
| The explorer does not open | Documentation is disabled, or the address is wrong | Ask the operator whether documentation is enabled; confirm the API origin |
| A browser connection error | The API is not reachable from your browser | Confirm the API is running and inspect the request URL |

## 5. Understand headers before trying write operations

Some operations publish versions, start runs, or change grants. Read each
operation before executing it: these requests change real data on the running
installation.

| Header | When and how to use it |
| --- | --- |
| `Authorization` | Swagger supplies `Bearer <token>` after authorization |
| `Idempotency-Key` | Supply a unique key for a new logical operation when its contract requires one; reuse it with the identical request only when retrying that operation |
| `If-Match` | For revision-sensitive changes, copy the quoted `ETag` returned when reading or creating that resource; a stale revision is rejected |
| `X-Weave-Request-ID` | Returned by Weave; keep it to investigate a failed request without sharing your token |
| `X-Weave-Wire-Version` | Returned by Weave to identify the response contract |

The operation's schema lists required headers, path IDs, success status codes,
and structured errors. Signed webhook and provider ingress use separate
authentication; the bearer dialog does not generate provider signatures. See
[HTTP and webhooks](../reference/http-and-webhooks.md).

Choose **Authorize → Logout** when you finish, or close the tab.

## Next steps

- Publish and run a workflow with the [CLI tutorial](cli-tutorial.md); each
  command maps to one operation you saw here.
- Call the same operations from Python with the [SDK tutorial](sdk-tutorial.md).
- Plan your product's integration with [host integration](host-integration.md).
