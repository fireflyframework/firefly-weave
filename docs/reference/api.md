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

The HTTP API is how every client talks to a Weave platform: your application
publishes workflows, starts runs, reads their progress, and answers waiting work
through it. The CLI, Studio's local host, and the Python SDK use this same API,
so an application written in any language can do what they do.

This page is for integration developers. In about 15 minutes you make two
read-only requests and one compile request with `curl`, and you learn the rules
every request follows: scope IDs, errors, revisions, and retry keys. You need:

- a running platform: one your team operates, or the
  [local platform](../guides/local-platform.md) on your computer;
- a current access token for that platform and a person or application that
  holds grants in a workspace (see [Get an access token](#get-an-access-token));
- `curl` and Python 3 in a terminal.

## Choose how to learn the API

| You want to… | Open | What you will get |
| --- | --- | --- |
| Make a first request from a terminal | [Make a read-only request first](#make-a-read-only-request-first) | Two `curl` requests that check your identity and project access |
| Look up one operation's exact contract | [Full API reference](api-explorer.md) | Every operation and schema, with a downloadable OpenAPI document |
| Send requests from your browser | [API playground](../guides/api-playground.md) | Your server's Swagger UI, how to authorize it, and a compiler request |
| Call the API from Python | [Python SDK tutorial](../guides/sdk-tutorial.md) | A script that publishes, activates, starts, and reads a run |
| Add workflows to your own product | [Host integration](../guides/host-integration.md) | Which responsibilities belong to your application and which to Weave |

**The published API reference is documentation only.** The interactive
Swagger UI lives at `/docs` on your own platform when its operator sets
`WEAVE_DOCS_ENABLED=true`. Its **Try it out** buttons send real requests to that
platform.

## Understand the three IDs in a request

Every workspace has three levels, and each one is a UUID in the request path:

- A **tenant** is an organization or team.
- A **project** inside it groups definitions: workflows, Actions, and Connectors.
- An **environment** inside the project, such as development or production, is
  where versions are activated and runs execute.

Display names cannot replace the UUIDs. The first request below lists the ones
you can use.

| Resource | Path after the API origin | Why it lives there |
| --- | --- | --- |
| Catalog, definitions, drafts, compiler | `/api/v1/tenants/{tenant}/projects/{project}` | Definitions belong to a project and can be used in all its environments |
| Activations, connections, runs, human tasks | The project path plus `/environments/{environment}` | Execution uses one environment's bindings and permissions |
| Your identity and the workspaces you can use | `/api/v1/identity` | Discover your own scopes without knowing any ID in advance |
| Published sign-in settings | `/api/v1/client-configuration` | Public, new in 0.1.0a7: how people sign in; `weave auth setup` and Studio read it |
| Health | `/health/live` and `/health/ready` | Public: operators check that the API runs and is ready |

For example, append `/catalog` to the project path to read the definitions you
can use, or `/runs/{run_id}` to the environment path to read one run. The
[full reference](api-explorer.md) gives each operation's exact path, body,
headers, and required capability.

**A token proves who you are; grants decide what you may do.** Every request
except the public ones needs a verified bearer access token from your
platform's identity provider. The platform then looks up the Weave person or
application linked to that sign-in and checks its current grants for the
requested scope. A successful sign-in alone grants nothing. See
[Give people the right access](../guides/people-and-access.md) and
[roles and lifecycle](../guides/roles-and-lifecycle.md).

## Get an access token

The CLI, Studio, and the desktop app never print the tokens they keep, so a
`curl` session needs a token from another source:

| Your platform | Where the token comes from |
| --- | --- |
| Operated by your team | Your operator's approved method, such as a CI secret or your identity provider's tooling. Follow [Use a supplied access token](../guides/connect-to-api.md#use-a-supplied-access-token) |
| The [local platform](../guides/local-platform.md) | `weave platform token` refreshes the local host token in the private file `.local/platform/host-token.json` (it prints the path, never the token) |
| A Python application | The [Python SDK](sdk.md#start-with-an-existing-api) can reuse your saved platform's sign-in (new in 0.1.0a7) or call your own token logic; no copying needed |

Keep the values in the same terminal. The API origin has no `/api/v1` suffix; on
the local platform, `weave platform status` shows it as `Api url`:

```sh
# Ask for the API origin without a path, such as https://weave.example.com.
printf 'Weave API origin: '; read -r WEAVE_BASE_URL
export WEAVE_BASE_URL
# Prompt for the token without echoing it or keeping it in shell history.
export WEAVE_ACCESS_TOKEN="$(python3 -c 'import getpass; print(getpass.getpass("Access token: "))')"
```

On the local platform, read the token file instead of typing the token:

```sh
# Refresh the local host token; the command prints only the file's path.
weave platform token
# Read the token from that private file without printing it.
export WEAVE_ACCESS_TOKEN="$(python3 -c 'import json; print(json.load(open(".local/platform/host-token.json"))["access_token"])')"
```

Expected: `Token file: …/host-token.json` from the first command and no output
from the second. Run both from the folder that holds `.local/platform`; with
`weave platform --directory PATH`, read `PATH/host-token.json` instead. Tokens
expire: when a request returns HTTP 401, get a new one the same way.

## Make a read-only request first

Start with requests that change nothing, so that a failure can only mean an
identity, scope, or network problem.

1. **Ask who you are.** This request needs no IDs, and its answer lists the
    workspaces you can use:

    ```sh
    # Read your identity, your grants, and the tenants, projects, and environments you can use.
    curl --fail-with-body --silent --show-error \
      "$WEAVE_BASE_URL/api/v1/identity" \
      -H "Authorization: Bearer $WEAVE_ACCESS_TOKEN"
    ```

    Expected: HTTP 200 and a JSON object with `principal_id`, `kind` (`human`,
    `application`, or `worker`), `grants`, and `workspaces`. Each workspace is a
    tenant with its `projects`, and each project lists its `environments`, all
    with `id` and `name`. HTTP 401 means the token is invalid or expired, or the
    platform does not know this sign-in yet; see
    [When a request fails](#when-a-request-fails).

2. **Keep the IDs of one workspace.** Copy the tenant, project, and
    environment `id` values from that answer:

    ```sh
    # Paste the UUIDs from the identity answer; display names do not work in paths.
    printf 'Tenant UUID: '; read -r WEAVE_TENANT_ID
    printf 'Project UUID: '; read -r WEAVE_PROJECT_ID
    printf 'Environment UUID: '; read -r WEAVE_ENVIRONMENT_ID
    export WEAVE_TENANT_ID WEAVE_PROJECT_ID WEAVE_ENVIRONMENT_ID
    ```

3. **Read the project catalog.** Your identity needs the `catalog.read`
    capability in that project:

    ```sh
    # Read the definitions this project can use before changing or running anything.
    curl --fail-with-body --silent --show-error \
      "$WEAVE_BASE_URL/api/v1/tenants/$WEAVE_TENANT_ID/projects/$WEAVE_PROJECT_ID/catalog" \
      -H "Authorization: Bearer $WEAVE_ACCESS_TOKEN"
    ```

    Expected: HTTP 200 and the catalog as JSON with `definitions`, `tasks`,
    `adapters`, and `schemas`; a new project's lists can be empty. HTTP 403 with
    `WV-FORBIDDEN` means you are known but hold no grant for this project.

<a id="make-one-request-before-reading-the-inventory"></a>

## Compile a workflow without publishing it

Compiling checks a workflow against the project's catalog and returns the
compiled artifact. It saves nothing, publishes nothing, and starts no run. Your
identity needs the `compile` capability in the project.

1. **Create a workflow file.** Write `.local/tutorial/echo.workflow.yaml` as in
    the [quickstart's definition step](../quickstart.md#create-the-definition),
    then return to the folder that contains `.local`.

2. **Wrap the YAML in a JSON request.** The API takes the source as a string,
    so encode it rather than pasting it:

    ```sh
    # JSON-encode the YAML so its quotes and newlines survive the HTTP request intact.
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
    ```

3. **Send it to the compiler.** Leaving out `catalog` makes the server use the
    project's current authorized catalog:

    ```sh
    # Compile against the project's catalog; nothing is saved or executed.
    curl --fail-with-body --silent --show-error \
      "$WEAVE_BASE_URL/api/v1/tenants/$WEAVE_TENANT_ID/projects/$WEAVE_PROJECT_ID/compiler/compile" \
      -H "Authorization: Bearer $WEAVE_ACCESS_TOKEN" \
      -H 'Content-Type: application/json' \
      --data-binary @.local/tutorial/compiler-request.json
    ```

    Expected: a compiler result with `ok: true`, `partial: false`, and an
    `artifact`. `filename` is only a label for diagnostics; the server never
    opens a file with that name.

**HTTP success and compiler success are separate checks.** A 200 response can
still carry `ok: false` with diagnostics: read `ok` and `diagnostics` as well as
the status.

**To publish instead,** send `{"source": "<the YAML text>", "format": "yaml"}`
to the project's `/workflows` collection with an `Idempotency-Key` header. The
[SDK lifecycle example](sdk.md#extend-the-host-to-publish-activate-and-run)
shows how the returned version ID and digest feed activation and the first run.

## When a request fails

Errors arrive as `application/problem+json` with `status`, a stable `code`, a
safe `message`, a `request_id`, and `diagnostics`. Quote the `request_id`
(also in the `X-Weave-Request-ID` header) when you ask an operator for help.

| What you see | Why | What to do |
| --- | --- | --- |
| HTTP 401, `WV-UNAUTHENTICATED` | The token is missing, malformed, or expired, was issued for another API, or its sign-in is not linked to a Weave person or application | Get a new token. If it still fails, ask an administrator to link your identity ([People and access](../guides/people-and-access.md)) |
| HTTP 403, `WV-FORBIDDEN` | You are known but hold no current grant with the required capability in that scope | Check the IDs in the path, then ask for the role you need |
| HTTP 404, `WV-NOT-FOUND` | No resource with that ID exists in the scope of the path | Check every ID in the path, including the environment |
| HTTP 409, `WV-IDEMPOTENCY-CONFLICT` | You reused an `Idempotency-Key` with a different request body | Use a new key for a new request; reuse a key only for an exact retry |
| HTTP 412, `WV-ETAG` | Your `If-Match` revision is no longer the current one | Read the resource again, merge your change, and resend with the new revision |
| HTTP 422, `WV-VALIDATION`, `WV-IDEMPOTENCY`, or `WV-ETAG` | The body does not match the contract, a required `Idempotency-Key` is missing, or `If-Match` is not a positive revision | Compare field names and types with the [full reference](api-explorer.md); send the revision as `"2"` or `2` |
| A timeout or lost connection during a change | The outcome is unknown: the change may or may not have happened | Keep the original body and key; read the resource, and retry only with the same key |

## Request rules: paths, errors, revisions, and retries

These rules hold for every operation. Read them before you send your first
change.

### Paths

- New code uses the versioned `/api/v1/tenants/...` paths. The older
  `/tenants/...` paths remain as compatibility routes to the same handlers,
  authorization, and data; they do not redirect.
- `/admin/tenants` and `/admin/grants` keep their own, separately authorized
  paths.
- Health probes, signed webhooks (`POST /webhooks/{identifier}`), and provider
  ingress (`/provider-ingress/{identifier}`) stay at the root. A webhook is
  checked against its trigger's signature over the exact timestamp and raw
  bytes; a bearer token never replaces that signature.
- Nothing in a request can raise your authority: identity-provider roles, a
  catalog lock you send, or a compiled artifact grant no execution or
  publication right. Each operation checks your identity and scope again.

### Headers and errors

- Canonical responses carry `X-Weave-Wire-Version: weave/api-v1` and
  `X-Weave-Request-ID`, even when authentication fails early.
- Errors use `application/problem+json` with `status`, `code`, `message`,
  `request_id`, and `diagnostics`. When an operation has a safe compiler result
  or an "unavailable" result, `result` keeps it.
- Errors never contain a framework traceback, a token, or a resolved
  connection credential.
- Worker credential leasing is the one deliberate secret boundary: it answers
  with `Cache-Control: no-store` and `Pragma: no-cache`, and its schema declares
  the secret `value` field. See the [worker protocol](worker-protocol.md).

### Revisions (ETags)

Resources that change, such as drafts, carry a **revision**: a positive number
that grows with each change.

- Responses send it as a quoted ETag, such as `"2"`.
- Send it back in `If-Match` to say "change this only if it is still revision
  2". Both `"2"` and the bare `2` are accepted. Weak (`W/"2"`), wildcard,
  multiple, zero, signed, and zero-padded values are invalid.
- A stale draft write answers HTTP 412 (`WV-ETAG`). Debug sessions and
  incidents keep their own conflict codes.
- The SDK reports the real status and code instead of converting every
  revision conflict to one error.

### Idempotency keys

An **idempotency key** is a name you choose for one intended change, so that a
retry cannot apply it twice. These changes require an `Idempotency-Key` header
of 1 to 200 characters:

- publishing and retiring definitions, and creating and retiring activations;
- starting, retrying, pausing, resuming, archiving, restoring, and purging
  runs;
- claiming, releasing, reassigning, and completing human tasks, and saving
  human assignments and groups.

The [full reference](api-explorer.md) shows the header on every operation that
accepts it. The rules for using a key:

- Sending the same key, from the same principal and scope, with the exact same
  body returns the result that was already committed. The same key with a
  different body fails with HTTP 409.
- Clients never retry a change on their own after a timeout or another
  outcome they cannot know. If you retry, send the exact same body and key. The
  one exception is a capacity rejection: HTTP 429 with `WV-OPERATION-CAPACITY`
  or `WV-REQUEST-CAPACITY` means the platform did not admit the request, so
  Studio sends a read, or a change that carries a key, again with the same key,
  honoring `Retry-After`, for at most four attempts in all.
- Creating a connection revision accepts an optional key since 0.1.0a7: with
  one, a retry returns the revision already created; without one, every request
  creates a new revision. Debug commands
  have no idempotency guarantee.

### Health probes

`GET /health/live` answers `{"status":"up"}` while the process runs, and
`GET /health/ready` answers `{"status":"ready"}` when it can serve requests.
When the API is not ready, `/health/ready` answers HTTP 503 with the same safe
problem envelope as other errors, so check the HTTP status or the `status`
field. Failure details, database URLs, and credentials are never included, and
the request ID is generated by the server. Only these exact paths are public.

## Authoring and lifecycle

![HTTP lifecycle requests, response identities, and authorization checks](../diagrams/authoring-host-sequence.svg)

Read down: solid arrows are your requests and dashed arrows return the IDs that
the next request needs. The API checks your grant at every step; revisions
protect edits, and idempotency keys make retries safe.
[Open diagram at full size](../diagrams/authoring-host-sequence.svg).

**Compile and validate.** `compiler/compile` and `compiler/validate` accept
`source`, `format`, an optional `filename`, an optional canonical `catalog`, and
`strict`.

- With an explicit catalog lock, the diagnostics, source locations, source map,
  and artifact digest are the same as in [local compilation](cli.md#local-workflow-commands).
- Without `catalog`, **compile** uses the project's current authorized catalog.
- Without `catalog`, **validate** performs partial validation: `partial`,
  `validationOk`, `ok`, `artifact`, and the diagnostics keep their separate
  meanings. `strict=true` then has no effect, exactly like
  `weave workflow validate --strict` without a catalog: it neither promotes
  warnings, rejects the request, nor substitutes an empty catalog.
- Partial validation does not prove a workflow can be deployed. Publication
  always repeats the server's authority and catalog checks.

**Drafts.** Saving a draft appends a new document revision.
`DELETE drafts/{identifier}` retires a draft; it needs `definition.write`, the
current grants, and the latest revision.

- The answer carries a new lifecycle revision and the retained document
  revision.
- A missing draft answers HTTP 404, a stale revision HTTP 412, and repeating a
  retirement that already happened is a conflict rather than new work.
- Reads and exports keep every revision with its retirement metadata. Lists
  leave retired drafts out by default, and a later save cannot bring the ID
  back.

**Published versions and connections never change.** A change publishes a new
version; retiring a version keeps its history. Updating a connection creates a
new connection revision with a new ID.

**Triggers.** Each trigger ID is one immutable configuration revision with its
own signed webhook URL and receipts. Replacing a trigger creates a new ID and
disables the previous route. The switch is not atomic: there can be an overlap
or a gap, and one event sent to both revisions can start two runs. Coordinate
the switch with the sender, or deduplicate upstream.

**Installed connectors.** Since 0.1.0a7,
`GET …/projects/{project}/connector-descriptors` lists the connectors installed
on the platform, and `…/connector-descriptors/{adapter}` reads one. Each
descriptor holds the exact Connector manifest to publish (`source`), its
capabilities, bindings, Actions, and connection settings, plus
`published_version_id` once it is published in the project. Both need
`catalog.read`. `weave connector descriptor ADAPTER` prints the same view.

## Discovery, debugging, and history

**Lists.** Canonical lists return `{ "items": [...], "next_cursor": null | "opaque" }`.

- `limit` is 1 to 100 (50 by default), and items come in stable ID order.
- Pass `next_cursor` back as `cursor` for the next page. A cursor is bound to
  its tenant, project, environment, collection, and any run or schedule filter;
  a cursor from another scope or filter is rejected.
- A cursor describes a position, not a snapshot: items created or retired
  meanwhile can change later pages.
- Compatibility endpoints that return plain arrays keep that shape.

**Who may list what.** Listing a whole collection needs the scope-wide
capability: connections `connection.manage`, runs and schedules `run.read`,
workers `status.read`, releases and the catalog `catalog.read`, triggers
`trigger.manage`, incidents `incident.read`. Grants are reloaded in the same
transaction. A resource whose classification cannot be checked appears as an
explicit safe placeholder; a list never widens access.

**Debug sessions** belong to a project and to the person who created them.
They expire in real time, use revisions, run Actions only against mocks, and
support breakpoints and virtual time. The response is a `DebugSession` envelope
that contains a `view`. Source-map file names are labels only. No connector or
secret provider runs during a simulation.

**History and replay.** Run history pages use a bounded high-water cursor and
keep the pinned source, receipt, and version facts, with explicit omissions.
Replay reports `consistent`, `inconsistent`, or `incomplete`: a history prefix,
or a history with unavailable or redacted parts, is never reported as success.
See [history and replay](history-and-replay.md).

**Answers that say "unavailable".** Cancellation and task completion can return
an unavailable acknowledgment when the platform cannot classify the historical
data or compare payloads; task completion then includes
`payload_match: unavailable`.

**Large runs.** Cancelling a supported historical run whose state is larger than
the current processing budget can return `CapacityRunAcknowledgment` instead of
a full `RunView`. It holds `id`, the terminal `status`, `accepted_sequence`,
`capacity_limited: true`, an explicit omission of `/state` with reason
`resource_limit`, and `external_effects_may_continue`. The cancellation and its
complete history are committed; only the large state is left out of the
answer. Handle both shapes. Cancellation stops further orchestration, but it
cannot undo an external call that is already in progress.

## Operation inventory

The table lists every operation with its canonical path and the capability it
requires. "Public probe" marks the endpoints that need no token. The contract
test in `tests/contracts/test_api_documentation.py` checks every row against the
platform's operation registry, so the table matches the code. GET routes also
answer HEAD, without a body. For request and response schemas, open the
operation in the [full API reference](api-explorer.md); to generate a client,
[export the OpenAPI document](native-openapi.md).

| Operation | Method and canonical path | Required authority |
| --- | --- | --- |
| `human_files.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/files/create` | human_task.complete |
| `human_files.chunk` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/files/chunk` | human_task.complete |
| `human_files.finish` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/files/finish` | human_task.complete |
| `human_files.read` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/files/read` | human_task.read |
| `human_files.download` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/human-tasks/{identifier}/files/download` | human_task.read |
| `files.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files` | file.manage |
| `files.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files` | file.read |
| `files.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files/{identifier}` | file.read |
| `files.chunk` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files/{identifier}/chunks` | file.manage |
| `files.finish` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files/{identifier}/finish` | file.manage |
| `files.download` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files/{identifier}/download` | file.read |
| `files.delete` | `DELETE /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/files/{identifier}` | file.manage |
| `task_files.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/files/create` | task.claim |
| `task_files.chunk` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/files/chunk` | task.claim |
| `task_files.finish` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/files/finish` | task.claim |
| `task_files.read` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/files/read` | task.claim |
| `task_files.download` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/files/download` | task.claim |
| `lumi.configuration.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/lumi/configuration` | lumi.manage |
| `lumi.configuration.write` | `PUT /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/lumi/configuration` | lumi.manage |
| `lumi.status` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/lumi/status` | lumi.use |
| `lumi.ask` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/lumi/ask` | lumi.use |
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
| `client_configuration.read` | `GET /api/v1/client-configuration` | Public probe |
| `health.live` | `GET /health/live` | Public probe |
| `health.ready` | `GET /health/ready` | Public probe |
| `admin.tenant` | `POST /admin/tenants` | tenant.create |
| `admin.grant` | `POST /admin/grants` | grant.admin or grant.manage |
| `projects.create` | `POST /api/v1/tenants/{tenant}/projects` | project.manage |
| `environments.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments` | environment.manage |
| `environments.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}` | status.read |
| `compiler.compile` | `POST /api/v1/tenants/{tenant}/projects/{project}/compiler/compile` | compile |
| `compiler.validate` | `POST /api/v1/tenants/{tenant}/projects/{project}/compiler/validate` | compile |
| `compiler.evaluate_decision` | `POST /api/v1/tenants/{tenant}/projects/{project}/compiler/evaluate-decision` | compile |
| `catalog.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/catalog` | catalog.read |
| `capabilities.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/capabilities` | catalog.read |
| `language.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/language` | catalog.read |
| `schemas.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/schemas` | catalog.read |
| `connector_descriptors.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/connector-descriptors` | catalog.read |
| `connector_descriptors.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/connector-descriptors/{adapter}` | catalog.read |
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
| `tasks.context` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/tasks/context` | task.claim |
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

| `deployment_plans.approval` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans/{identifier}/approval` | deployment.read |
| `deployment_jobs.reconcile` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-jobs/{identifier}/reconcile` | deployment.apply and deployment.approve |
| `deployment_targets.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-targets` | deployment.read |
| `deployment_targets.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-targets/{identifier}` | deployment.read |
| `deployments.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployments` | deployment.read |
| `deployments.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployments/{identifier}` | deployment.read |
| `deployment_observations.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-observations` | deployment.read |
| `deployment_observations.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-observations/{identifier}` | deployment.read |
| `deployment_plans.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans` | deployment.read |
| `deployment_plans.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans/{identifier}` | deployment.read |
| `deployment_jobs.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-jobs` | deployment.read |
| `deployment_jobs.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-jobs/{identifier}` | deployment.read |
| `deployment_runners.list` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners` | deployment.read |
| `deployment_runners.read` | `GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners/{identifier}` | deployment.read |
| `deployment_targets.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-targets` | target.manage |
| `deployments.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployments` | target.manage |
| `deployment_observations.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-observations` | deployment.plan |
| `deployment_plans.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans` | deployment.plan |
| `deployment_targets.update` | `PUT /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-targets/{identifier}` | target.manage |
| `deployments.update` | `PUT /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployments/{identifier}` | target.manage |
| `deployment_plans.approve` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans/{identifier}/approve` | deployment.approve |
| `deployment_plans.apply` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-plans/{identifier}/apply` | deployment.apply |
| `deployment_jobs.cancel` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-jobs/{identifier}/cancel` | deployment.cancel |
| `deployment_runners.create` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners` | runner.register |
| `deployment_runners.claim` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners/claim` | runner.claim |
| `deployment_runners.renew` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners/renew` | runner.renew |
| `deployment_runners.report` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners/report` | runner.report |
| `deployment_runners.revoke` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/deployment-runners/{identifier}/revoke` | target.manage |
| `workers.drain` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers/{identifier}/drain` | worker.drain |
| `workers.resume` | `POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/workers/{identifier}/resume` | worker.drain |

## Deployment Operations authority

Follow [Manage container deployments](../operations/cluster-management.md)
for the complete Studio and CLI sequence.

Deployment targets, desired deployments, observations, plans, approvals, jobs,
and runner registrations belong to one environment. Child-resource reads check
the target grant. Lists filter authorized target IDs before pagination; use
`target_id` to select a target, and `deployment_id` to narrow plan history.
An observation is a dated snapshot, not a promise of current health.

The server creates an immutable plan from the desired revision and a fresh,
complete observation. Approval records its exact digest. Applying checks the
current target and desired revisions, freshness, and the approver's current
authority again. `deployment_plans.approval` returns the recorded approval or
`null`; a recorded approval does not bypass those final checks.

| Role | Responsibility |
| --- | --- |
| `deployment_reader` | Read authorized target inventory and evidence |
| `deployment_planner` | Register target metadata, edit desired deployment, observe, and plan |
| `deployment_approver` | Approve the exact immutable plan |
| `deployment_operator` | Apply or cancel work |
| `deployment_runner` | Dedicated application's registration, claims, renewals, and reports |
| `worker_operator` | Drain or resume a specific worker instance |

Provider credentials and configuration templates stay on the outbound runner.
The API accepts typed intent, immutable image digests, and local configuration
aliases; it does not accept commands or cloud credentials. API/scheduler
components require exactly one instance. Worker components require an admitted
release. Available actions are bounded by both the registered target and the
runner's local adapter policy.

Only one job may be active per target. Losing authority after external changes
begin requires reconciliation, never automatic re-execution. An operator with
both apply and approve capabilities must acknowledge that the external
operation stopped and select a new complete, settled observation to close the
ambiguous job. Closing it does not reverse changes or apply another plan.
Worker drain prevents new claims and lets existing leases settle; scaling a
provider replica count to zero is a different operation.

## Compatibility and retention authority

**Compatibility.** A project viewer with `status.read` can read the project's
compatibility report; starting a fresh check needs `compatibility.check`.
Reports leave out other projects' findings, and both operations reload your
current grants.

**Retention.** A retention plan is an immutable set of items chosen by the
server.

- Only the plan's creator, with the matching current project capability, can
  read or apply it. An environment-only or resource-limited grant is not enough.
- Another person's plan answers exactly like an unknown plan (not found).
- Applying a plan checks authorization and eligibility again; a client cannot
  replace the items in it.

The [CLI retention commands](cli.md#compatibility-and-retention) show the
complete plan-then-apply sequence.

## Next steps

- Look up any operation's full contract in the [full API reference](api-explorer.md).
- Publish, activate, and start a run from Python with the
  [SDK tutorial](../guides/sdk-tutorial.md) and the [SDK reference](sdk.md).
- Run the same requests from a terminal with the [CLI](cli.md#remote-authoring-and-operations),
  which signs in for you and remembers your workspace.
- Generate a client in another language from the
  [native OpenAPI export](native-openapi.md).
