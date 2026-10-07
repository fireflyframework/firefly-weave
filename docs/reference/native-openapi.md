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

# Export Weave's OpenAPI document

An OpenAPI document describes an HTTP API's paths, request bodies, responses,
and security requirements, so that client generators and API tools can use it.
This page shows integration developers how to get the document that describes
**Weave's own API**, for example to generate a client in another language. It
takes about five minutes and needs no running platform.

**Looking for the opposite direction?** To call another company's API from a
workflow, you import *its* OpenAPI document instead; see
[Call a REST API without code](../connectors/http-without-code.md#import-operations-from-an-openapi-document).

## How the document is built

![Native Weave OpenAPI export compared with external OpenAPI metadata import](../diagrams/authoring-openapi-directions.svg)

Follow the top EXPORT row from left to right: that is this page. The IMPORT
row below it turns another service's OpenAPI document into reviewed Actions,
either on the built-in HTTP connector ([Call a REST API without code](../connectors/http-without-code.md))
or as a connector package ([Import an OpenAPI document as a connector package](../connectors/metadata-import.md)).
[Open diagram at full size](../diagrams/authoring-openapi-directions.svg).

- The [operation inventory](../../src/firefly_weave/contracts/surface.py)
  declares each operation's method, path, request, success and error models,
  status, headers, and security. The platform checks its routes against this
  registry at startup, and a contract test checks the
  [HTTP API inventory](api.md#operation-inventory) against it.
- [The exporter](../../src/firefly_weave/contracts/openapi.py) turns that
  inventory into PyFly `RouteMetadata` and calls PyFly's public
  `OpenAPIGenerator`. There is no second, documentation-only web stack.
- When the platform starts, it checks that every route is covered and that no
  two collide. The versioned `/api/v1` paths and the older compatibility paths
  share the same handlers.
- Security entries in the document describe how the platform enforces access;
  they do not replace that enforcement. Administrative roots, health probes,
  and signed or provider ingress keep their own authentication rules.

## Get the document

Choose the source that fits:

| You want | Where | Notes |
| --- | --- | --- |
| To read it without installing anything | The [full API reference](api-explorer.md) on the documentation site | Every operation and schema, plus a JSON download |
| The document of a running platform | `/openapi.json` and the Swagger UI at `/docs` on that platform | Only when its operator sets `WEAVE_DOCS_ENABLED=true` (the default is `false`); API calls still need a token and grants. See the [API playground](../guides/api-playground.md) |
| A file generated from your checkout | The steps below | Matches exactly the source you build from |

## Export it from a source checkout

1. **Install the `openapi` extra.** Run this from the root of a locked source
    checkout. Generating the document starts no application and opens no
    socket; it needs no database, credentials, or network access to a provider.
    `uv sync` removes packages that the extras you name do not need, so add
    every other extra this checkout uses (for example `--extra client`) to the
    same command.

    ```sh
    # Add PyFly's OpenAPI generator to the checkout's environment.
    uv sync --locked --no-editable --extra openapi
    ```

2. **Write the document to a file.** `.local` is ignored by Git:

    ```sh
    # Generate the native OpenAPI document and save it with sorted keys.
    mkdir -p .local/openapi
    uv run --locked --no-editable --extra openapi python -c 'import json; from firefly_weave.contracts.openapi import export_openapi; print(json.dumps(export_openapi(), sort_keys=True, indent=2))' > .local/openapi/weave-openapi.json
    ```

    Expected: `.local/openapi/weave-openapi.json` holds an OpenAPI 3.1.0 document
    with `paths` and `components.schemas`.

3. **Inspect the operation you need.** Check its operation ID and responses
    before you generate a client:

    ```sh
    # Print the compiler operation's ID and the HTTP statuses it can return.
    uv run --locked --no-editable --extra openapi python - <<'PYTHON'
    import json
    from pathlib import Path
    api = json.loads(Path(".local/openapi/weave-openapi.json").read_text())
    path = "/api/v1/tenants/{tenant}/projects/{project}/compiler/compile"
    operation = api["paths"][path]["post"]
    print(operation["operationId"])
    print(sorted(operation["responses"]))
    PYTHON
    ```

    Expected: `compiler.compile`, then
    `['200', '401', '403', '404', '409', '412', '413', '422', '500']`. The
    request schema includes `source` and `format`, and the responses describe
    both the compiler result and the errors. Each operation's `description`
    names its required capability.

**A generated client is only a starting point.** Every call still needs a
current access token, grants in the scope, revision headers, and idempotency
keys where the operation requires them. [Use the HTTP API](api.md) explains
each of these.

## Versions and what the document proves

- The document's `info.version` is the API version, `1`. It is separate from
  the package version (`0.1.0a14`) and from the workflow language version
  (`weave/v1alpha1`).
- The exporter uses PyFly 26.9.15. The exact URL, SHA-256, and upstream
  provenance are in [project metadata](../../pyproject.toml) and the lockfile.
- The language and schema exporter (`weave schema export`) and the workflow
  builder do not need PyFly.
- Connection test, definition retirement, and activation retirement accept an
  empty body, so their request bodies are optional in the document. Every other
  operation uses the requiredness declared in the inventory.
- Contract tests check the wire fixtures, schema and discriminator resolution,
  operation coverage, and route parity. Generating the document does not prove
  how the platform authorizes or answers; the [HTTP API](api.md),
  [SDK](sdk.md), and [CLI](cli.md) references describe that behavior.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| `ImportError: OpenAPI generation requires the optional firefly-weave[openapi] dependency` | PyFly's generator is not installed in this environment | Run step 1, or add `--extra openapi` to `uv run` |
| `/openapi.json` or `/docs` answers 401 on a running platform (404 when you send a valid token) | The operator has not enabled the documentation pages, so they do not exist there | Ask the operator to set `WEAVE_DOCS_ENABLED=true`, or export the document locally |

## Next steps

- Make a first request by hand with [Use the HTTP API](api.md).
- Browse operations and schemas in the [full API reference](api-explorer.md).
- Use the typed [Python SDK](sdk.md) instead of generating a Python client.
