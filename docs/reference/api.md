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

# Use the HTTP API

The API lets your application publish workflows, start executions, read their
progress, and respond to waiting work. The CLI and Python client use this same
HTTP interface. An application written in another language can call it directly.

## Choose how to learn the API

| You want to… | Open | What you will get |
| --- | --- | --- |
| Inspect every request and response | [Full API reference](api-explorer.md) | Searchable operations and schemas, with a downloadable OpenAPI contract |
| Send a request from your browser | [API playground](../guides/api-playground.md) | Steps to open your server's Swagger UI, authorize, and compile a workflow |
| Call the API from a terminal | [First HTTP request below](#make-a-read-only-request-first) | A small `curl` request that checks project access |
| Call it from Python | [Python SDK tutorial](../guides/sdk-tutorial.md) | A complete script that publishes, activates, starts, and reads a run |
| Add workflows to your product | [Host integration](../guides/host-integration.md) | Which responsibilities belong to your application and which belong to Weave |

The published website's API reference is **read-only documentation**. The
interactive Swagger UI is at `/docs` on your own running API when its operator
enables `WEAVE_DOCS_ENABLED`. Its **Try it out** buttons send real requests to
that installation. Use [the playground instructions](../guides/api-playground.md)
for token entry, scope IDs, expected results, and error recovery.

## Understand the three IDs in a request

A **tenant** selects an organization or workspace. A **project** groups its
definitions. An **environment** selects where versions are activated and runs
execute, such as development or production. All three are provisioned UUIDs;
their display names cannot be used in the path.

| Resource | Path after the API origin | Why it lives there |
| --- | --- | --- |
| Catalog and workflow definitions | `/api/v1/tenants/{tenant}/projects/{project}` | Definitions belong to a project and can be used in its environments |
| Activations, connections, and runs | The project path plus `/environments/{environment}` | Execution uses an environment's prepared bindings and permissions |
| Your identity and authorized workspaces | `/api/v1/identity` | Discover your own scopes and grants without entering someone else's IDs |
| Health | `/health/live` and `/health/ready` | Operators check whether the API is running and ready |

For example, append `/catalog` to the project path to read its available
definitions. Append `/runs/{run_id}` to the environment path to inspect one
execution. The [full reference](api-explorer.md) supplies each operation's exact
path, body, headers, and permission requirements.

Every resource request needs a verified bearer **access token** and current local
Weave grants. The token comes from your installation's configured compatible
OIDC/CIAM provider; Keycloak is supplied for the local development recipe only.
The IDs choose resources, while grants determine what you may do with them. A
successful login alone does not grant access to a project.

## Make a read-only request first

Complete [Connect to an API](../guides/connect-to-api.md) using its **supplied-token
option**. Keep `WEAVE_BASE_URL`, `WEAVE_TENANT_ID`, `WEAVE_PROJECT_ID`, and a current
`WEAVE_ACCESS_TOKEN` in the same terminal. The API origin has no `/api/v1` suffix.
Your identity needs `catalog.read` for this project.

```sh
# Read available definitions before attempting to change or execute anything.
# The token proves identity; the server checks your project's catalog.read grant.
curl --fail-with-body --silent --show-error \
  "$WEAVE_BASE_URL/api/v1/tenants/$WEAVE_TENANT_ID/projects/$WEAVE_PROJECT_ID/catalog" \
  -H "Authorization: Bearer $WEAVE_ACCESS_TOKEN"
```

Expected: HTTP 200 and the project's catalog as JSON. A new project's catalog may
be empty. This request does not publish a workflow or create a run. If it fails,
resolve the identity, scope, or connectivity issue before adding write operations.
The browser and SDK do not automatically read the CLI's saved credentials; each
client needs its own configured token source.

<a id="make-one-request-before-reading-the-inventory"></a>

## Compile a workflow without publishing it

Keep the same connection variables from the previous step. Create
`.local/tutorial/echo.workflow.yaml` using the
[quickstart's definition step](../quickstart.md#create-the-definition), then return
to that same working directory. Your identity also needs the project `compile`
capability. Create a JSON request that embeds the YAML file as a string:

```sh
# JSON-encode the YAML so quotes and newlines survive the HTTP request intact.
python3 - <<'PYTHON'
import json
from pathlib import Path
request = {
    "source": Path(".local/tutorial/echo.workflow.yaml").read_text(),
    "format": "yaml",
    "filename": "echo.workflow.yaml",
    "strict": True,
}
Path(".local/tutorial/compiler-request.json").write_text(json.dumps(request))
PYTHON
# Compile checks the source against the project's authorized dependency catalog.
curl --fail-with-body --silent --show-error \
  "$WEAVE_BASE_URL/api/v1/tenants/$WEAVE_TENANT_ID/projects/$WEAVE_PROJECT_ID/compiler/compile" \
  -H "Authorization: Bearer $WEAVE_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @.local/tutorial/compiler-request.json
```

This requires the project `compile` capability. The server uses its authorized
catalog because the body omits `catalog`. Expect a compiler result with `ok: true`,
`partial: false`, and an `artifact`. `filename` is only a diagnostic label: the
server does not open a file with that name. No publication or run is created.

An HTTP success and a successful compilation are separate checks: read the
compiler result's diagnostics and `ok` field as well as the HTTP status. For
publication, send `{"source": "<the YAML text>", "format": "yaml"}` to the
project's `/workflows` collection with an `Idempotency-Key`. The
[SDK lifecycle example](sdk.md#extend-the-host-to-publish-activate-and-run) shows
how its returned version ID and digest feed activation and run creation.

| Request fails with | Check first |
| --- | --- |
| Authentication error | Token expiry, issuer, audience, and the configured API origin |
| Authorization error | Local identity link and current grant for this operation and scope |
| Revision conflict | Read the current revision; reconcile edits before submitting again |
| Invalid request or compiler diagnostic | Exact field spelling, schema, and diagnostic path |
| Timeout or lost response during a mutation | Outcome is unknown; retain the original body and idempotency key |

![HTTP lifecycle requests, response identities, and authorization checks](../diagrams/authoring-host-sequence.svg)

Read down through the lifecycle. Solid arrows send requests and dashed arrows return identities; the API checks each operation separately. Expected revisions protect edits, while idempotency keys identify exact retries of keyed mutations. [Open the diagram at full size](../diagrams/authoring-host-sequence.svg).

## Requests, errors and revisions

New integrations should use the versioned `/api/v1/tenants/...` paths. The older
`/tenants/...` paths remain compatibility routes to the same native endpoints,
service graph, authorization, and application lifespan; they do not redirect.
Administrative `/admin/tenants` and `/admin/grants` keep their separately
authorized paths. Health probes and signed `POST /webhooks/{identifier}` remain
at the root. Signed webhooks verify the exact timestamp and raw request bytes
against their immutable trigger revision; ordinary bearer authentication does
not substitute for the webhook signature.

Provider roles, caller-supplied catalog locks, and compiled artifacts do not grant
execution or publication authority. Scope and identity are checked for each
operation. See [identity and secrets](../operations/identity-and-secrets.md) to
configure the installation's provider and local grants.

Canonical responses include `X-Weave-Wire-Version: weave/api-v1` and `X-Weave-Request-ID`, including early authentication failures. Errors use `application/problem+json` with `status`, stable `code`, safe `message`, `request_id` and `diagnostics`. Where an existing operation has a safe compiler or unavailable result, `result` preserves it. No framework traceback, token or resolved connection credential appears in ordinary resource errors. Worker credential leasing is a separately authorized secret boundary with `Cache-Control: no-store` and `Pragma: no-cache`; its schema describes the actual deliberate `value` field.

Revision responses on canonical paths use quoted positive ETags such as `"2"`. `If-Match` accepts that syntax and the existing bare positive integer syntax. Weak, wildcard, multiple, zero, signed and padded revisions are invalid. Draft stale writes use HTTP 412; debug and incident conflict codes retain their existing operation semantics. The SDK preserves the actual status and code rather than converting every revision conflict to a fabricated status.

Publish, activation and run-start/retry operations require `Idempotency-Key`. A matching key, principal scope and exact request recover the committed result; changed content conflicts. Clients do not automatically retry unsafe requests, including timeouts with an unknown outcome. Caller-chosen retries must preserve the exact body and applicable key. Connection revision creation and debug commands have no fabricated idempotency guarantee.

## Authoring and lifecycle

`compiler/compile` and `compiler/validate` accept `source`, `format`, optional `filename`, optional canonical `catalog`, and `strict`. An explicit lock gives the same diagnostics, source locations, source map and artifact digest as offline compilation. Omitted compile catalog uses the currently authorized server catalog. Omitted validate catalog performs partial validation: `partial`, `validationOk`, `ok`, `artifact` and canonical diagnostics retain their distinct meanings. Without a catalog, `strict=true` is inapplicable and returns the same partial validation result as offline `workflow validate --strict`; it does not promote partial warnings, reject the request or substitute an empty catalog. Partial validation does not establish deployability. Publication always applies current server authority/catalog checks.

Saving a draft creates an append-only document revision. `DELETE drafts/{identifier}` logically retires the draft under `definition.write`, current project grants, a scoped lock and the required latest revision. Its acknowledgment has a new lifecycle revision and the retained document revision. Missing drafts return HTTP 404; stale retirement returns HTTP 412; a matching repeated retirement conflicts instead of claiming new idempotent work. Read/export retain every document revision with retirement metadata, default lists omit retired drafts, and later saves cannot resurrect the ID. Published versions are immutable: changes publish a new version; retirement preserves historical evidence. Connection updates create new immutable IDs/revisions.

Each trigger ID is an immutable configuration revision and has its own signed webhook URL/receipts. Replacement creates a new ID and explicitly disables the previous route. Cutover can have overlap or a gap; no atomic family retargeting is promised. The same event sent to two trigger revisions can start two runs. Coordinate cutover or deduplicate upstream when necessary.

## Discovery, debugging and history

Canonical lists return `{ "items": [...], "next_cursor": null | "opaque" }`, with limits 1–100 and stable ID ordering. Cursors bind tenant/project/environment, resource collection and any run/schedule filter. A cursor from another scope/filter fails validation. Initial pagination includes a valid zero UUID. Compatibility array endpoints retain their prior wire shape. Cursors describe traversal, not a database snapshot; concurrent creation/retirement can change subsequent pages.

Aggregate discovery requires the existing scope-wide capability: connections `connection.manage`, runs `run.read`, workers `status.read`, releases/catalog `catalog.read`, triggers `trigger.manage`, incidents `incident.read`, schedules `run.read`. Current grants are reloaded in the operation transaction. Classification-unavailable resources stay explicit safe placeholders; discovery never expands access by returning unfiltered internal rows.

Debug sessions are project-owned and creator-controlled, with real-time expiration, revision fencing, mock-only actions, breakpoints and virtual time. Their response is a `DebugSession` envelope containing `view`, not the inner view alone. Filenames in source maps are opaque labels. No connector or secret provider executes during simulation. Run history keeps its bounded high-water cursor, pinned source/receipt/version facts and safe omissions. Replay reports `consistent`, `inconsistent` or `incomplete`; prefixes and unavailable/redacted histories cannot be reported as success. Cancellation and task completion can return an unavailable acknowledgment when historical classification or payload comparison cannot be established; task completion includes `payload_match: unavailable` in that case.

Cancellation of a supported historical run whose state exceeds the current processing budget can instead return `CapacityRunAcknowledgment`: `id`, terminal `status`, `accepted_sequence`, `capacity_limited: true`, an explicit `/state` omission with reason `resource_limit`, and `external_effects_may_continue`. The terminal change and complete durable history remain committed; the response does not include the large state or label the run unavailable. Clients must handle this response union rather than assuming every successful cancellation contains a full `RunView`. Cancellation fences further orchestration work but cannot undo an external effect already in progress.

## Native schema export

`firefly_weave.contracts.openapi.export_openapi()` lazily uses the public PyFly `OpenAPIOperation`, `RouteMetadata` and `OpenAPIGenerator` APIs with Weave's canonical schema policy. It needs the optional `openapi` extra and does not start an app, open sockets, acquire services or read secrets. Pure definition/schema export and the builder remain PyFly-free. The operation registry supplies product request/response/auth/header metadata for manual Request/JSONResponse handlers; native PyFly owns schema generation. Security declarations document runtime enforcement and do not replace it.

The following operation inventory is generated from the same explicit product metadata. The contract test in `tests/contracts/test_api_documentation.py` checks every operation, canonical path, and authority label against that registry. GET route HEAD support follows the native HTTP router; HEAD has no response body.

| Operation | Method and canonical path | Required authority |
| --- | --- | --- |
| `compatibility.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/operations/compatibility` | status.read |
| `compatibility.check` | `POST /api/v1/tenants/{tenant}/projects/{project}/operations/compatibility/check` | compatibility.check |
| `retention.plan` | `POST /api/v1/tenants/{tenant}/projects/{project}/operations/retention/plans` | retention.plan |
| `retention.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/operations/retention/plans/{identifier}` | retention.plan |
| `retention.apply` | `POST /api/v1/tenants/{tenant}/projects/{project}/operations/retention/plans/{identifier}/apply` | retention.apply |
| `teams_references.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/teams-references/{identifier}` | trigger.manage |
| `teams_references.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/teams-references` | trigger.manage |
| `teams_references.revoke` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/teams-references/{identifier}/revoke` | trigger.manage + connection.manage + connection.bind |
| `teams_references.reactivate` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/teams-references/{identifier}/reactivate` | trigger.manage + connection.manage + connection.bind |
| `whatsapp_statuses.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{identifier}/whatsapp-statuses` | run.read |
| `whatsapp_statuses.facts` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{identifier}/whatsapp-statuses/{state_id}/facts` | run.read |
| `provider_sources.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources` | trigger.manage + connection.manage + connection.bind + target authority |
| `provider_sources.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{identifier}` | run.read |
| `provider_sources.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources` | run.read |
| `provider_sources.disable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{identifier}/disable` | trigger.manage + connection.bind + target authority |
| `provider_receipts.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-receipts/{identifier}` | run.read |
| `provider_receipts.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-receipts` | run.read |
| `provider_receipts.retry` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-receipts/{identifier}/retry` | run.retry + current source/target authority |
| `provider_ingress.receive` | `POST /provider-ingress/{identifier}` | provider verification |
| `provider_ingress.challenge` | `GET /provider-ingress/{identifier}` | provider challenge verification |
| `human_tasks.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks` | human_task.read |
| `human_tasks.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}` | human_task.read |
| `human_tasks.claim` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/claim` | human_task.claim |
| `human_tasks.release` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/release` | human_task.release |
| `human_tasks.reassign` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/reassign` | human_task.manage |
| `human_tasks.complete` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/complete` | human_task.complete |
| `human_assignments.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-assignments` | assignment.read |
| `human_assignments.put` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-assignments` | assignment.manage |
| `human_groups.put` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-groups` | assignment.manage |
| `runs.lifecycle` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/lifecycle` | run.read |
| `runs.archive` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/archive` | run.archive |
| `runs.restore` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/restore` | run.archive |
| `runs.purge` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/purge` | run.purge |
| `runs.pause` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/pause` | run.pause |
| `runs.resume` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/resume` | run.resume |
| `email_conversations.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/conversations` | email.read |
| `email_conversations.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/conversations/{identifier}` | email.read |
| `email_submissions.send` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/submissions` | email.send |
| `email_submissions.reply` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/conversations/{identifier}/reply` | email.send |
| `email_submissions.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/submissions/{identifier}` | email.read |
| `email_submissions.execute` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/submissions/{identifier}/execute` | email.send |
| `email_receipts.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/receipts` | email.read |
| `email_sources.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/sources` | email.manage |
| `email_sources.poll` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/sources/{identifier}/poll` | email.manage |
| `email_sources.rebaseline` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/sources/{identifier}/rebaseline` | email.manage |
| `email_receipts.correlate` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/receipts/{identifier}/correlate` | email.manage |
| `email_receipts.dispatch` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/receipts/{identifier}/dispatch` | email.manage |
| `email_tokens.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/correlation-tokens` | email.manage |
| `email_tokens.revoke` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/email/correlation-tokens/{identifier}/revoke` | email.manage |
| `principals.list` | `GET /api/v1/admin/principals` | grant.admin |
| `principals.create` | `POST /api/v1/admin/principals` | grant.admin |
| `principals.link` | `POST /api/v1/admin/principals/{identifier}/identity-links` | grant.admin |
| `principals.status` | `POST /api/v1/admin/principals/{identifier}/status` | grant.admin |
| `members.list` | `GET /api/v1/tenants/{tenant}/members` | grant.manage or grant.admin |
| `members.grant` | `POST /api/v1/tenants/{tenant}/members` | grant.manage or grant.admin |
| `members.revoke` | `POST /api/v1/tenants/{tenant}/members/{identifier}/revoke` | grant.manage or grant.admin |
| `identity.read` | `GET /api/v1/identity` | authenticated identity |
| `health.live` | `GET /health/live` | Public probe |
| `health.ready` | `GET /health/ready` | Public probe |
| `admin.tenant` | `POST /admin/tenants` | tenant.create |
| `admin.grant` | `POST /admin/grants` | grant.admin or grant.manage |
| `projects.create` | `POST /api/v1/tenants/{tenant}/projects` | project.manage |
| `environments.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments` | environment.manage |
| `environments.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}` | status.read |
| `compiler.compile` | `POST /api/v1/tenants/{tenant}/projects/{project}/compiler/compile` | compile |
| `compiler.validate` | `POST /api/v1/tenants/{tenant}/projects/{project}/compiler/validate` | compile |
| `catalog.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/catalog` | catalog.read |
| `capabilities.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/capabilities` | catalog.read |
| `schemas.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/schemas` | catalog.read |
| `definitions.publish` | `POST /api/v1/tenants/{tenant}/projects/{project}/{collection}` | definition.publish |
| `definitions.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/{collection}` | catalog.read |
| `definitions.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/{collection}/{identifier}` | catalog.read |
| `definitions.export` | `GET /api/v1/tenants/{tenant}/projects/{project}/{collection}/{identifier}/export` | catalog.read |
| `definitions.retire` | `POST /api/v1/tenants/{tenant}/projects/{project}/{collection}/{identifier}/retire` | release.retire |
| `drafts.save` | `PUT /api/v1/tenants/{tenant}/projects/{project}/drafts/{identifier}` | definition.write |
| `drafts.retire` | `DELETE /api/v1/tenants/{tenant}/projects/{project}/drafts/{identifier}` | definition.write |
| `activations.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/activations` | release.activate |
| `activations.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/activations` | catalog.read |
| `activations.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/activations/{identifier}` | catalog.read |
| `activations.export` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/activations/{identifier}/export` | catalog.read |
| `activations.retire` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/activations/{identifier}/retire` | release.retire |
| `connections.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connections` | connection.manage |
| `connections.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connections` | connection.manage |
| `connections.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connections/{identifier}` | connection.manage |
| `connections.test` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connections/{identifier}/test` | connection.manage |
| `runs.start` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs` | run.start |
| `runs.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs` | run.read |
| `runs.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}` | run.read |
| `runs.signal` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/signals` | run.signal |
| `runs.cancel` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/cancel` | run.cancel |
| `runs.retry` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/retry` | run.retry |
| `runs.history` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/history` | run.read |
| `runs.export` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/export` | run.read |
| `runs.replay` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/replay` | run.read |
| `incidents.run_list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{identifier}/incidents` | incident.read |
| `incidents.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/incidents` | incident.read |
| `incidents.resolve` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/incidents/{identifier}/resolve` | incident.resolve |
| `releases.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/worker-releases` | release.activate |
| `releases.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/worker-releases` | catalog.read |
| `releases.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/worker-releases/{identifier}` | catalog.read |
| `workers.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers` | worker.register |
| `workers.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers` | status.read |
| `workers.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers/{identifier}` | status.read |
| `workers.revoke` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers/{identifier}/revoke` | release.retire |
| `workers.grant` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/worker-connection-grants` | connection.manage |
| `tasks.claim` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/claim` | task.claim |
| `tasks.heartbeat` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/heartbeat` | task.heartbeat |
| `tasks.complete` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/complete` | task.complete |
| `tasks.fail` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/fail` | task.complete |
| `tasks.credentials` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/credentials` | credential.lease |
| `subscriptions.save` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/subscriptions` | subscription.manage and connection.manage and connection.bind and source authority |
| `subscriptions.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/subscriptions` | delivery.read |
| `subscriptions.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/subscriptions/{identifier}` | delivery.read |
| `subscriptions.disable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/subscriptions/{identifier}/disable` | subscription.manage and source authority |
| `deliveries.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deliveries` | delivery.read |
| `deliveries.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deliveries/{identifier}` | delivery.read |
| `deliveries.retry` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deliveries/{identifier}/retry` | delivery.retry and current source authority |
| `deliveries.history` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deliveries/{identifier}/attempts` | delivery.read |
| `source_bindings.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connection-source-bindings` | connection.manage |
| `source_bindings.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connection-source-bindings/{identifier}` | connection.manage |
| `source_bindings.revoke` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/connection-source-bindings/{identifier}/revoke` | connection.manage |
| `broker_triggers.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers` | trigger.manage and connection.manage and connection.bind and target authority |
| `broker_triggers.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers` | trigger.manage |
| `broker_triggers.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers/{identifier}` | trigger.manage |
| `broker_triggers.disable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers/{identifier}/disable` | trigger.manage |
| `broker_triggers.retry` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers/{identifier}/retry` | trigger.manage and current source authority |
| `broker_triggers.incidents` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-incidents` | trigger.manage |
| `triggers.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/triggers` | trigger.manage |
| `triggers.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/triggers` | trigger.manage |
| `triggers.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/triggers/{identifier}` | trigger.manage |
| `triggers.disable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/triggers/{identifier}/disable` | trigger.manage |
| `webhooks.receive` | `POST /webhooks/{identifier}` | signed webhook and pinned principal authority |
| `schedules.save` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules` | trigger.manage and run.start |
| `schedules.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules` | run.read |
| `schedules.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules/{identifier}` | run.read |
| `schedules.history` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules/{identifier}/occurrences` | run.read |
| `schedules.enable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules/{identifier}/enable` | trigger.manage |
| `schedules.disable` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules/{identifier}/disable` | trigger.manage |
| `schedules.delete` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/schedules/{identifier}/delete` | trigger.manage |
| `debug.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/debug/sessions` | simulate |
| `debug.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/debug/sessions/{identifier}` | simulate |
| `debug.command` | `POST /api/v1/tenants/{tenant}/projects/{project}/debug/sessions/{identifier}/commands` | simulate |


Readiness failure keeps HTTP 503 at the exact root `/health/ready`, and now uses
the same safe Problem envelope as other API errors. Clients should inspect
HTTP 503 or `Problem.status` for unavailability.
Healthy readiness remains `{"status":"ready"}` and liveness remains
`{"status":"up"}`. Failure details, database URLs and credentials are omitted;
request IDs are server-generated UUIDs. No health prefix exemption is added.

## Compatibility and retention authority

A project viewer can read the scoped compatibility report with `status.read`.
Starting a fresh compatibility check requires `compatibility.check`. Reports omit
other projects' findings. Both operations reload current local grants.

Retention plans are immutable server-selected sets. Reading or applying a plan
requires its creator and the corresponding current project capability. An
environment-only or resource-constrained grant does not grant project maintenance
authority. Another creator's plan returns the same not-found response as an
unknown plan. Applying a plan rechecks current authorization and server-owned
eligibility; the client cannot replace its candidate IDs.
