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

# CLI reference

Start with [CLI installation](../installation.md). The installer includes local
authoring, API access, authentication, OpenAPI import, and the Studio host. Its browser assets are a
matching optional ZIP installed explicitly by digest. An advanced installation
of only the base `firefly-weave` wheel supports local authoring; remote commands
need the `client` extra. Server and worker extras are unnecessary for either path.
The offline commands listed below do not initialize PyFly, read application
configuration/credentials, discover providers, or connect to a service. They read
only explicitly supplied source/catalog paths and write only requested exports or
starter files. `docs` prints a link; only its explicit `--open` option requests
a browser.

For a guided command sequence from authoring to durable execution, follow the
[CLI tutorial](../guides/cli-tutorial.md). This reference explains the individual
commands, their files, and their result contracts.

## Pick the command family

| Goal | Commands | Prerequisite |
| --- | --- | --- |
| Create a local starter or find a guide | `init DIRECTORY`, `docs [TOPIC]` | Base package; no platform required |
| Check or simulate a local definition | `workflow validate`, `compile`, `explain`, `simulate`, `graph` | Base package and explicit files |
| Export language schemas | `schema export` | Base package |
| Call a running API | `remote`, `definitions`, `runs`, other remote families | `client` extra, token, scope, local grants |
| Run durable workflows for the first time | `platform doctor/setup/start/status/demo/token/stop` | Matching checkout, uv, and local Docker; [walkthrough](../guides/local-platform.md) |
| Draw and inspect a workflow | `studio`, `studio configure`, `studio install` | Matching alpha6 browser bundle and `studio` extra; [Studio setup](../guides/studio.md) |
| Claim or complete a human task | `human-tasks`, `human-assignments`, `human-groups` | Authorized API profile; [human task walkthrough](../guides/human-tasks.md) |
| Read or reply to a conversation | `email` | Authorized API and email connection; [email walkthrough](../connectors/email.md) |

Studio, human tasks, email, and membership administration are included in alpha6.
Alpha4 predates these additions. Studio starts a loopback server and compiler;
connecting a profile also enables authenticated platform calls.

From a locked source checkout, prefix commands with
`uv run --locked --no-editable` (and `--extra client` for remote commands).
Use `weave --help`, then the selected command's `--help`, to inspect arguments.
Bare `weave` shows the same grouped help and ASCII banner and exits successfully.
`weave help workflow compile` opens nested help without executing the operation;
`weave --version` prints the installed version. Banners appear only in root help,
so `weave version --output json` and remote JSON results remain machine-readable.

![Offline CLI validation and compilation stages](../diagrams/authoring-diagnostic-loop.svg)

Read down through the same checks used by the CLI. With no catalog, a successful validate exits 0, but compile and explain exit 1 because no artifact exists. An explicitly empty catalog selects complete compilation. [Open the diagram at full size](../diagrams/authoring-diagnostic-loop.svg).

## Create an offline project

`weave init DIRECTORY [--output text|json]` creates a starter workflow in the
selected directory. It works from any working directory with the base package;
you do not need the source checkout, Docker, a token, or an API. Choose a new
project path, then enter it before running its relative-path commands:

```sh
weave init my-first-workflow
cd my-first-workflow
weave workflow validate workflow.yaml --catalog catalog.lock.json --strict
weave workflow compile workflow.yaml --catalog catalog.lock.json --strict --directory build
weave workflow simulate simulation.json --output json
```

Expected: validation and compilation succeed; simulation returns the sample
message `Hello from Firefly Weave!`. These commands do not start services or save
a durable run. For that, follow [platform setup](../guides/platform-overview.md).

| Starter file | Purpose |
| --- | --- |
| `workflow.yaml` | `hello-weave@1.0.0`, which returns its message input |
| `catalog.lock.json` | An explicitly empty catalog for complete compilation |
| `input.json` | Sample message input |
| `simulation.json` | Initial compiled artifact, sample input, empty mocks, and fixed virtual clock |
| `README.md` | Commands to run the starter and refresh simulation after edits |

Simulation reads `simulation.json`, not the current YAML or input file. After
editing either, recompile and refresh the simulation request as shown in the
generated README. Compilation into `build` alone does not rewrite that request.

Initialization refuses conflicting starter filenames and unsafe symlink targets;
it preserves unrelated files and offers no `--force` option. Use a different
directory for another starter. JSON success contains `ok: true` and the created
filenames. Local creation failures exit 2 with a `WV-CLI-INIT` diagnostic. The
[export safety rules](#export-safety-and-contracts) describe file publication.

## Find documentation from the CLI

`weave docs [TOPIC] [--open] [--output text|json]` prints a URL under the
[published documentation website](https://fireflyframework.github.io/firefly-weave/).
Without a topic it selects the documentation home. Printing a URL requires no
network connection; viewing the hosted page requires access to the website.

```sh
weave docs
weave docs platform
weave docs cli --output json
weave docs quickstart --open
```

Only `--open` asks the default browser to open. JSON output contains a `url` field;
a browser launch failure exits 2, so rerun without `--open` to obtain the link.

| Topic | Destination |
| --- | --- |
| `quickstart` | [Offline quickstart](../quickstart.md) |
| `platform` | [Deploy, start, and use the platform](../guides/platform-overview.md) |
| `cli` | This reference |
| `workers` | [Worker authoring](../guides/workers.md) |
| `deploy` | [Local worker and container deployment](../operations/deployment.md) |
| `configuration` | [Runtime configuration](../operations/configuration.md) |
| `local` | [Local platform](../guides/local-platform.md) |
| `api` | [API playground](../guides/api-playground.md) |
| `sdk` | [Python SDK tutorial](../guides/sdk-tutorial.md) |
| `connectors` | [Custom connectors](../guides/custom-connectors-tutorial.md) |
| `graphs` | [Workflow graphs](../guides/workflow-graphs.md) |

The links lead to the published documentation site, which may describe a newer
release than your installed client. Check `weave --version` and the release
selected by the installation guide when following server setup commands.

## Local platform commands

`weave platform [--directory PATH] COMMAND` operates a retained local development
installation. The default directory is `.local/platform` relative to your working
directory. Reuse the same absolute directory when using different terminals.
Follow [the complete local guide](../guides/local-platform.md) before setup.

| Command | Effect |
| --- | --- |
| `doctor [--source REPO] [--context NAME]` | Read-only check of source version, uv, and local Docker |
| `setup [--source REPO] [--context NAME]` | Build and install an isolated server, start owned dependencies, bootstrap identity |
| `start` | Resume dependencies and run the API in the foreground |
| `status` | Inspect saved stage, readiness, URLs, and first-run receipt |
| `demo` | Create one authorized demo run; reuse its complete saved receipt on repeats |
| `token` | Refresh the verified token into its private file; print only the path |
| `stop` | Stop the owned dependencies after the foreground API exits; retain data |

All except foreground `start` accept `--output json`. Setup displays current-stage
progress with a spinner only in an interactive terminal. Set
`WEAVE_NO_ANIMATION=1` to keep plain progress messages. Redirected output has no
animation; JSON output has no progress messages. There are no reset/delete
operations in this local command family.

## Workflow graph exports

`weave workflow graph ARTIFACT [--format text|mermaid|svg] [--directory PATH]`
reads a compiled Workflow artifact and checks its integrity before drawing.
Use the [graph tutorial](../guides/workflow-graphs.md) for a complete example.
The default is a text graph on stdout; `--directory` exports `workflow.txt`,
`workflow.mmd`, or `workflow.svg`. Replacing an existing regular file requires
`--force`. `--output json` returns `ok`, `format`, `content`, and `files`.
This shows static possible paths, not live run state. No action is executed.

## Offline workflow commands

The [authoring guide](../guides/workflow-authoring.md) creates these files under
`.local/tutorial`. Run the following from the checkout root after that guide's
YAML and empty catalog steps:

```sh
weave version --output json
weave workflow validate .local/tutorial/echo.workflow.yaml --output json
weave workflow compile .local/tutorial/echo.workflow.yaml \
  --catalog .local/tutorial/empty-catalog.json --strict --output json
weave workflow explain .local/tutorial/echo.workflow.yaml \
  --catalog .local/tutorial/empty-catalog.json
weave workflow compile .local/tutorial/echo.workflow.yaml \
  --catalog .local/tutorial/empty-catalog.json --directory .local/tutorial/compiled
weave schema export --directory .local/tutorial/schemas
```

`validate` reports partial success; `compile` returns an artifact because an
explicit catalog was supplied. `explain` shows the plan without executing it.
The last two commands create files and refuse existing targets by default. Choose
a new output directory when repeating an export. To execute the echo plan without
services, build the request described in [simulation](simulation.md) and run
`weave workflow simulate .local/tutorial/simulation-request.json --output json`.

`workflow validate`, `compile`, and `explain` accept `SOURCE`, optional
`--catalog LOCK`, `--output text|json` (default `text`), and `--strict`. A `.json`
source uses strict JSON; other extensions use the restricted YAML reader.
Catalog locks always use strict JSON. Source and catalog reads are bounded at
1,048,576 bytes plus one overflow sentinel; the parser enforces depth/document
bounds. An oversized source is invalid; an oversized or malformed catalog is a
local configuration error.

| Status | Exit | Meaning |
| --- | ---: | --- |
| Successful complete compilation, or successful partial `validate` | 0 | The requested checks passed |
| Invalid definition, strict warning, or partial `compile`/`explain` | 1 | No requested executable result |
| Invocation, file, malformed catalog, or export failure | 2 | Fix local arguments/configuration |
| Remote service or transport failure | 3 | Authenticated remote commands |

Without a catalog, every workflow command runs `validate_source`; it returns
`partial: true`, `validationOk` describing those checks, `ok: false`, and
`artifact: null`. Successful partial validation exits 0 only for `validate`.
`compile` and `explain` exit 1 because they require an executable. Text explicitly
labels partial results. An explicitly supplied empty catalog requests complete
compilation; it is not equivalent to an absent catalog. Partial validation does
not perform dependency/type/dominance analysis, and `--strict` cannot add those
checks. With a catalog, `--strict` promotes complete-analysis warnings to errors.

JSON workflow output is exactly `CompileResult.to_bytes()` plus a newline for
all three commands. It contains retained diagnostics, complete truncation
metadata, status and the optional artifact envelope; there is no progress text.
Handled invocation, file, catalog, and export errors use the same envelope with
one `WV-CLI-*` diagnostic and exit 2. Raw input values and exception strings are
not included in these errors. `schema export --output json` reports the explicit
directory and filenames; `version --output json` reports package/API/IR versions.

Artifacts necessarily contain author literals and dependency declarations.
Do not embed credentials in source or catalogs; runtime connections and secret
providers belong to later application stages. Offline compilation never resolves
secret references or copies environment credentials into output. Human explain
shows resolved dependency names/digests, node and edge flow, source positions,
expression reads, and runtime guard purposes/schema references. It does not
print literal payloads. JSON explain retains the identical compiler envelope.

## Export safety and contracts

`compile --directory DIR` writes `compiled-artifact.json`, `executable.json`, and
`catalog.lock.json` only after complete successful compilation. The artifact
contains source maps and diagnostics; the executable alone contains semantic IR.
The lock is the supplied validated JSON catalog, in canonical form, including
unused resources so that it can be reused for other local definitions.

`schema export --directory DIR` writes one `<name>.schema.json` file per published
contract, including definitions, artifacts, catalogs, worker releases and leases,
completion acknowledgments, provider sources, and operational responses.
The [schema profile](schema-profile.md) describes validation boundaries.
Catalog-lock describes definitions plus normalized-document SHA-256 digests,
task capabilities, adapter names, and bundled schemas. `CatalogLock` and
`CatalogSnapshot.from_lock` enforce catalog identity/digest invariants; a generic
JSON Schema validator cannot establish content hashes.

Every export preflights the full set of target paths before writing. Existing
files are refused unless `--force` is explicit; unrelated files are preserved.
Destination symlinks, target symlinks, and directory/file collisions are refused,
even with `--force`. Each file is published from a temporary sibling without
following the target symlink. The set is not a filesystem transaction: disk or
permission failures during publication can leave a subset of complete files.
No files are emitted for partial or invalid compilation.

## Language boundaries

The [schema profile](schema-profile.md) lists the exact accepted and rejected
Draft vocabulary, local-only references, four enforced formats, regex subset,
and resource budgets. [Compiler contracts](compiler.md) specify expressions,
branching and source/semantic identity. No raw code, remote schemas, environment
or filesystem expressions are supported. Numbers are finite IEEE-754 values;
integers must be in `[-9007199254740991, 9007199254740991]`. NaN, infinities,
invalid Unicode scalars, duplicate mapping keys and YAML aliases are rejected.
Money/exact decimals should use validated strings. Typed integer fields reject
floating representations, while embedded JSON Schema retains Draft integer
semantics (`1.0` is an integer). Successful offline compilation proves neither
provider availability nor deployment authorization; publication must recompile
against the server's authorized catalog.

Recorded history is available through `weave run history` and `weave run export`.
`weave run replay --artifact pinned-artifact.json --events history.json` verifies
recorded facts locally without executors. See [history and replay](history-and-replay.md)
for typed contracts, authorization, exact source references and completeness limits.

## Remote authoring and operations

Install the `client` extra for authenticated remote operations. The public command families are `remote`, `definitions` (including `drafts` and `activations`), `connections`, `runs` (including `incidents` and `debug`), `workers` (including `releases`), and `triggers` (including `schedules`). Each operation is a thin adapter over the shared typed SDK and uses canonical `/api/v1/tenants/...` paths. Existing singular operator commands remain compatibility commands.

All public remote commands take `--base-url`, `--tenant`, applicable `--project`/`--environment`, and `--output json`. Their matching environment variables are `WEAVE_BASE_URL`, `WEAVE_TENANT_ID`, `WEAVE_PROJECT_ID` and `WEAVE_ENVIRONMENT_ID`. A request body is an exact JSON DTO supplied with `--request FILE`; resource IDs are positional, revisions use `--revision`, and keyed mutations require `--idempotency-key`. Discovery uses `--limit` and `--cursor`. Use each command's `--help` for its exact required arguments. The [API inventory](api.md) defines the corresponding operation IDs and request/response schemas.

![Remote lifecycle request and response sequence](../diagrams/authoring-host-sequence.svg)

Follow the same sequence with remote CLI commands: exact JSON request files go out, resource identities come back. Preserve the relevant revision or idempotency key rather than treating every command as a fresh request. [Open the diagram at full size](../diagrams/authoring-host-sequence.svg).

### Construct a remote request file

Use [an existing API](../guides/connect-to-api.md) or complete the
[standalone tutorial](../guides/standalone.md) and retain its
`WEAVE_BASE_URL`, `WEAVE_TENANT_ID`, `WEAVE_PROJECT_ID`, and
`WEAVE_ENVIRONMENT_ID` values. Supply a current `WEAVE_ACCESS_TOKEN`, or use the
login configuration below. Remote commands do not read server environment files
or bootstrap missing identities.

For a first remote compile, create `.local/tutorial/compiler-request.json` using
the [API example](api.md#compile-a-workflow-without-publishing-it), then run:

```sh
weave remote compile --request .local/tutorial/compiler-request.json --output json
```

Expect `ok: true` and an artifact. The CLI sends the exact JSON request; it does
not infer source from a YAML path in a JSON field. To publish instead, create a
separate request containing only `source` and `format` (no compiler-only fields):

```sh
python3 - <<'PYTHON'
import json
from pathlib import Path
request = {"source": Path(".local/tutorial/echo.workflow.yaml").read_text(),
           "format": "yaml"}
Path(".local/tutorial/publication.json").write_text(json.dumps(request))
PYTHON
weave definitions publish --collection workflows \
  --request .local/tutorial/publication.json \
  --idempotency-key tutorial-echo-publish-1 --output json
```

This mutates the server and requires `definition.publish`. Expect a version ID,
name, version, and digest. Reuse that key only for an exact retry of this request;
use a new workflow version and key when changing its source. The
[SDK example](sdk.md#extend-the-host-to-publish-activate-and-run) explains how that
version becomes an activation and run. Python methods build typed request objects;
the CLI expects you to supply those exact wire objects in files.

### Other operation shapes

The following are lookup examples, **not a script to run in order**. Each request
file must contain the corresponding operation's JSON body, each variable must be
an actual resource ID from your scope, and each principal needs that operation's
grant. Use the [API inventory](api.md) and [native OpenAPI export](native-openapi.md)
to find those body schemas:


```sh
weave remote catalog --output json
weave remote compile --request compiler-request.json --output json
weave definitions drafts save "$DRAFT_ID" --request draft-request.json --output json
weave definitions drafts save "$DRAFT_ID" --revision 1 --request changed-draft.json --output json
weave definitions publish --collection actions --request publication.json \
  --idempotency-key action-publication-1 --output json
weave connections create --request connection.json --output json
weave runs history "$RUN_ID" --limit 50 --output json
weave runs replay "$RUN_ID" --output json
weave runs debug command "$SESSION_ID" --revision 1 --request debug-command.json --output json
```

Remote JSON stdout contains one result/problem and no banners; diagnostics go to stderr. Exit 0 means the requested operation/check succeeded, exit 1 means invalid source, denied/domain operation or incomplete/inconsistent replay, exit 2 means local configuration/invocation error, and exit 3 means remote service/transport/contract failure. Unsafe requests are not retried. Compiler responses retain exact canonical source locations; `filename` is a caller-supplied label, not a server path to open.

## Login and secure persistence

`WEAVE_ACCESS_TOKEN` supports host-managed access tokens. Alternatively every public remote command accepts `--auth-config FILE` plus the same store options as `auth`. Credentials are acquired independently of server configuration.

A login configuration identifies `provider_id`, exact `issuer`, public `client_id`, exact API-origin `target`, local `account`, requested `scopes`, and optional explicitly trusted endpoint origins. HTTPS is required except with the explicit `allow_loopback_http: true` development setting. The default scopes may need adjustment for a provider's registered client; the retained minimal Weave Keycloak client uses `["openid"]`.

```sh
weave auth login --auth-config login.json --flow auto --output json
weave auth status --auth-config login.json --output json
weave auth logout --auth-config login.json --revoke --output json
```

The native credential store is the default and fails closed if unavailable. To select the explicit POSIX fallback, first create an absolute caller-owned 0700 directory, then pass `--credential-store file --credential-file /absolute/private/session.json` on every invocation. An existing record must be a caller-owned 0600 regular file; insecure modes and symlink/hardlink paths are rejected. The fallback is unsupported on Windows. Neither path prints persisted tokens. Device instructions (verification URI/user code) go to stderr; token, refresh token and PKCE verifier do not.

`--flow auto` selects advertised device authorization; `--flow pkce` launches the browser with an already bound ephemeral loopback callback. Cancellation/expiry stores no active credentials. Fresh lifecycle UUIDs and the shared cross-process lock fence refresh, logout and late login completion as described in [SDK credential lifecycle](sdk.md#device-login-pkce-and-stores).

For pinned Keycloak 26.7.4, registering exact loopback callback entries with explicit default port 80 (`http://127.0.0.1:80/callback`, `http://[::1]:80/callback`) enables its special arbitrary-port matching while keeping the path exact. The template retains the older fixed callback too, enables S256/device authorization, disables password grants and sets refresh revocation with max reuse 0. Realm import skips existing realms: apply these settings additively through trusted administration rather than resetting a retained realm. The test harness's localhost-cookie adjustment models a browser only; product token clients never relax cookie/TLS policy.

## Trusted connector authoring

`weave connector init TARGET --name NAME` and `weave connector validate METADATA`
work in the base installation without executing package code.
`weave connector test IDENTITY [--mode fixtures|native]` executes an explicitly
selected installed native package and its optional fixture suite.
`weave connector package PROJECT --directory OUTPUT` builds an explicit trusted
local project. All accept `--output text|json`; rejected operations exit 1 and
invalid CLI syntax exits 2. These deployment tools add no tenant HTTP API.
See [connector authoring](../connectors/authoring.md) for trust boundaries, exact
identities, dependency requirements, schema checks, and executable examples.

## Compatibility and retention

After configuring the API origin, tenant, project, and authenticated identity as
above, `weave compatibility read --output json` reads the current scoped report
under `status.read`. `weave compatibility check --output json` requests a fresh
check and requires `compatibility.check`.

Retention application removes eligible retained data. Use this operational
command only when you intend to apply the plan in the selected project; it is
not part of the echo tutorial. Create a retention request file containing `{"target":"expired_debug","limit":100}`,
then capture the returned immutable plan ID:

```sh
weave retention plan --request retention-request.json --output json > retention-plan.json
PLAN_ID="$(python -c 'import json; print(json.load(open("retention-plan.json"))["id"])')"
weave retention apply --plan-id "$PLAN_ID" --output json
```

The apply command first reads and displays that exact stored plan as JSON on
stderr, then applies it without another confirmation. Stdout contains only the
final result or problem. A failed plan read prevents apply. The existing
positional plan ID remains supported; conflicting positional and `--plan-id`
values are rejected before contacting the API. Only the plan creator with current
project maintenance grants can read or apply it.
