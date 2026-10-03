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

# Full API reference

Look up any HTTP operation of the Weave platform: its path, parameters,
security, request body, responses, headers, and the schemas they use. This page
is for integration developers who already know which operation they need. It is
read-only: nothing here sends a request.

**New to the API?** Start with [Use the HTTP API](api.md). It explains the
tenant, project, and environment IDs in every path, how to get an access token,
and walks you through a first read-only request. To send requests from your
browser, use your installation's [API playground](../guides/api-playground.md).

**How to find what you need.** Operations are grouped by area, such as
**Compiler**, **Runs**, or **Human Tasks**. Open one to see:

- its method, path, and operation ID, such as `compiler.compile`; the same IDs
  appear in the [operation inventory](api.md#operation-inventory), the
  [CLI](cli.md) help, and the [native OpenAPI export](native-openapi.md);
- a **Required capability** line: the grant your identity needs in that scope
  (public endpoints, such as health and published sign-in settings, need none);
- the complete contract as JSON, with every schema reference linked to its
  definition under **Schemas** at the end of the page.

The published documentation builds this reference from the same contract as the
running API, so it matches the release the site describes. If you are reading
this Markdown on GitHub, the list below is empty: open the
[published API reference](https://fireflyframework.github.io/firefly-weave/reference/api-explorer/)
or [export the same OpenAPI document locally](native-openapi.md).

<!-- WEAVE_API_REFERENCE -->
