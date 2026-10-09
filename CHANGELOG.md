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

# Changelog

## Unreleased

- No-code HTTP connections accept `http://` base URLs and warn that traffic is not encrypted.
- Add development-only private origins to the Docker development platform:
  `weave platform up --allow-private-origin ORIGIN` (repeatable) lets HTTP
  connector actions and signed webhooks reach a local `http://` test service at
  that exact origin on the installation's own egress network. The API reads the
  approved entries from the read-only file named by `WEAVE_PRIVATE_ORIGINS_FILE`
  and refuses to start when that file is malformed, a symbolic link, or writable
  by other users.
- Parse `WEAVE_HTTP_PRIVATE_NETWORKS`, `WEAVE_MAIL_PRIVATE_NETWORKS`, and the
  PostgreSQL private and plaintext network settings strictly at startup: a CIDR
  with host bits set, or more than 128 networks, in any of them stops the API.
  Before, the HTTP and mail settings accepted a CIDR with host bits set and
  failed the requests that used it, and the HTTP and PostgreSQL settings had no
  limit. Reach is otherwise unchanged.
- Connection test answers carry a new `encrypted` field. CLIs and SDKs older
  than this release cannot read `weave connections test` answers for HTTP
  connections from an upgraded server; upgrade the CLI and SDK together with the
  server.
- Keep an integration event delivery recoverable when Weave refuses one of its
  database transactions for capacity (`WV-OPERATION-CAPACITY`). The delivery
  stays `leased` instead of moving to `retry` with `DELIVERY_FAILED` or to an
  `AUTHORITY_REVOKED` incident. When the lease expires, the attempt is recorded
  as `ACK_UNKNOWN` and the same event ID is delivered again. The attempt still
  counts, so a delivery refused on every attempt ends in a `DELIVERY_EXHAUSTED`
  incident.
- Native connector tasks no longer fail with `HANDLER_FAILED` when Weave refuses
  one of their platform calls for capacity (`WV-OPERATION-CAPACITY` or
  `WV-REQUEST-CAPACITY`). The invocation check before the connector starts is
  sent again while the task's lease is valid; authority and credential checks
  made while the connector runs get up to three attempts within one second.
- Native connector calls no longer share the two execution work slots of API
  requests, so a burst of requests no longer refuses them. They run up to the
  `capacity` configured for each `WEAVE_NATIVE_EXECUTORS` entry.
- `email_receipts.dispatch` answers HTTP 429 (`WV-OPERATION-CAPACITY` or
  `WV-REQUEST-CAPACITY`) when Weave refuses the dispatch for capacity, where it
  used to answer HTTP 200 with state `blocked`. The receipt keeps its state, and
  the next pending scan dispatches it.
- Draw "weave" in the Firefly Weave logo as a wordmark whose w is woven from two
  strands, one amber, instead of typed text, in Studio, on the desktop launch
  page and installer, and in the README banner and social preview. NOTICE names
  the Weave name and logo as trademarks of the Firefly Software Foundation.
- Show the product name, Firefly Weave, at the top of CLI help instead of ASCII
  logo art, and say Weave AI in CLI prompts and in server and worker messages.
  API paths, permissions, roles and error codes keep `lumi`.
- Give the desktop app the Firefly icon, a charcoal DMG background as tall as
  the installer window, and a charcoal launch page in a charcoal window that
  opens without a white flash, and draw exported workflow graphs, the API
  explorer, the README banner, shields and badges with the Firefly identity.
- Runs in progress whose workflow uses a language feature the server does not
  run, for example after rolling back to an earlier release that lists fewer
  language features, now wait for an upgrade instead of being blocked for good.
  The server offers none of their tasks to workers, leaves their deadlines and
  expired attempts pending, records nothing about them, and does not let them
  hold back other runs. After the upgrade they continue, and deadlines that
  passed in the meantime apply then. A server that predates language features
  still blocks such runs permanently, so finish or cancel them before rolling
  back that far.
- Reading such a run, sending it a signal, or reporting a task result for it
  answers HTTP 422 `WV-IR-UNSUPPORTED` with `result.missing_features` instead of
  HTTP 409 `WV-LEGACY-UNAVAILABLE`. Reading a definition version the server does
  not run, or repeating the publish request that created it, answers the same
  way. Run and incident lists still show such runs as unavailable, and runs with
  unavailable legacy evidence still answer `WV-LEGACY-UNAVAILABLE`.
- Catalog list pages can contain a new kind of item for a version the server
  does not run: `unavailable: true`, `reason: ir_unsupported`, and
  `missing_features`. SDKs and CLIs older than this release reject such a page;
  upgrade them together with the server.
- Restyle the documentation site in one dark scheme with the Firefly Weave logo,
  the Firefly favicon and self-hosted Manrope, and recolor every diagram to the
  Firefly light palette on paper panels.
- Call the Studio assistant Weave AI throughout the documentation. Its guide is
  now [Use Weave AI](docs/guides/weave-ai.md); [the previous guide](docs/guides/lumi.md)
  points to it. API paths, permissions and role names keep `lumi`.
- Studio no longer marks a workflow it just opened or created as Unsaved before
  any edit when it is not connected to a platform.

## 0.1.0a14

- Add a detached Docker development platform through `weave platform up`,
  reusing owned databases, identity setup, workspace creation, and user roles.
  Keep the foreground API mode available for existing installations.
- Prepare an explicit Developer ID signing and notarization path for macOS
  releases. Apple credentials are required; ordinary builds remain ad-hoc signed.
- Pin the Agentic and Files 0.1.6 packages to core 0.1.0a14 without changing their
  execution behavior or the database schema.

## 0.1.0a13

- Retry explicit platform capacity rejections while a worker reads its task
  context or credentials, using the current validated lease. Reject late
  responses after cancellation, renewal failure, or expiry; unrelated HTTP
  errors and ambiguous network failures are not replayed.
- Classify an Agentic preparation timeout as not started when its own time
  budget expires before provider execution. Failures after provider execution
  begins retain their ambiguous outcome classification.
- Publish Agentic and Files worker packages 0.1.5 pinned to core 0.1.0a13.
  Files has a dependency update only. The worker protocol and database schema
  remain unchanged from alpha12 (`0030_worker_presence`).

## 0.1.0a12

- Add environment-scoped deployment targets, desired state, observations,
  immutable approval plans, fenced runner jobs, and explicit reconciliation
  for uncertain external effects. Provider credentials remain on an outbound
  runner with local destination and image allowlists.
- Add Operations in Studio and the CLI for reviewed container deployment
  changes. Support trusted Compose deployment and bounded existing Kubernetes
  and Azure Container Apps changes; provisioning cloud clusters is separate.
- Report dated worker contact and capacity, with revision-checked drain and
  resume controls that preserve active leases.
- Let Lumi explain explicitly selected Operations records using bounded,
  permission-checked context. Deployment changes retain their separate plan,
  approval, and application process.
- Reject unsupported component and destination changes before plan approval.
  Mark stopped or stalled compatibility monitors unhealthy so container
  supervisors can replace the affected API process.
- Guide target registration with named application accounts, downloadable
  nonsecret runner setup, observed-resource import, explicit resource capacity,
  and admitted worker release selection.
- Allow reviewed Compose plans to restart stopped workers, checking readiness
  after the restart instead of during the dry run.
- Keep worker lease renewal retrying explicit capacity rejections while the
  current lease remains valid, including while a completed task awaits its
  acknowledgment. Ambiguous transport failures never replay the task handler.
- Keep invalid inspector edits and unapplied AI profile changes visible before
  saving, publishing, or switching views. Edit workflow AI profiles in a dialog,
  with multiline prompts and direct connection-slot selection.
- Organize editor commands into aligned rows and step settings into purposeful
  sections. Keep palette search visible and explain data sources through
  keyboard-accessible information controls. Filter source modes and formulas
  by the receiving field's type while preserving imported expressions for repair.
- Separate reusable project actions from creating a new API action, with a
  direct connection entry point and contextual guidance for local authoring.
- Keep Studio pages and editor assets loading while platform requests are busy,
  and retry temporary session-read capacity rejections within a bounded budget.
- Use standard OpenAI and Anthropic endpoints automatically, retain explicit
  Azure configuration, and group advanced model settings separately. Check
  model-setting combinations with the canonical local validator before applying
  them; configuration validation does not claim live provider readiness.
- Add migrations `0029_deployments` and `0030_worker_presence`, with explicit
  scoped Operations roles. Existing workflow roles gain no deployment authority.
- Publish worker packages 0.1.4 pinned to core 0.1.0a12 and desktop alpha12.
  Keep the verified alpha10 Azure acceptance history separate from this release.

## 0.1.0a11

- Guide AI provider connections, Lumi settings, and workflow AI profiles through
  focused steps with review before creation or application. Keep unfinished
  profile changes local and apply the profile and connection slot atomically.
- Let authors explicitly pass available earlier AI results as structured context
  within one workflow execution, using existing compiler-checked expressions.
- Document the verified alpha10 Azure application-token, AI workflow, Lumi, and
  HTTP acceptance separately from unverified interactive Entra sign-in.
- Correct browser selection tests to wait for the insertion reveal before
  checking pointer reachability.
- Publish worker packages 0.1.3 pinned to core 0.1.0a11. No server behavior or
  database migration changes; the schema remains `0028_files`.

## 0.1.0a10

- Expose Lumi configuration through the paired Studio host and clarify the
  separate administrator, worker-admission, and deployment responsibilities.
- Let explicit canvas selection and structural edits supersede pending initial
  fitting. Keep automatically added empty fields from stealing keyboard focus.
- Use the Lumi mascot in Studio and the illustrated documentation.
- Make compiler scope-parity tests reject inconclusive resource-limit results
  and isolate their timing allowance from production validation budgets.
- Publish worker packages 0.1.2 pinned to core 0.1.0a10. No database migration is
  added; the server still requires schema `0028_files`.

## 0.1.0a9

- Add guided Studio setup for AI provider connections, named connection selection
  for Lumi, and independent workflow model profiles. Keep model credentials in
  operator-managed secret storage and show scoped setup permissions explicitly.
- Keep incomplete LLM profile edits open until required properties are present.
- Add renewable OAuth2 client credentials for independently deployed Agentic
  workers, with bounded token acquisition, expiration-aware caching, scoped API
  origins, cancellation cleanup, and mounted-secret rotation.
- Explain AI workflow execution, Lumi proposal review, and configuration roles
  with step-by-step guides and accessible SVG diagrams.
- Publish worker packages 0.1.1 pinned to core 0.1.0a9. No database migration is
  added; the server still requires schema `0028_files`.

## 0.1.0a8

- Add verified file references, resumable bounded transfers, scoped file roles,
  CLI upload/download commands, Python SDK streaming helpers, worker lease
  checks, and human-task attachments with current claimant/revision checks.
  PostgreSQL stores file chunks separately from run history; retained runs pin
  their files until the run is purged.
- Add independently deployed FTP, FTPS, SFTP, SharePoint/OneDrive and Google
  Drive workers for list, metadata, download, write, move and delete operations.
  Cloud account acceptance remains environment-specific; change-feed triggers
  are not included.
  FTP/FTPS/SFTP require a server-isolated account root. FTP/FTPS write and move
  are disabled by default because the protocol cannot guarantee atomic refusal
  to replace a concurrently created destination; enabling them requires an
  explicit connection policy and a separate worker policy.
- Add contains/not-contains and membership comparisons, versioned decision
  tables, and schema-guided Studio editors for rules and AI steps.
- Add workflow `llmProfiles` and Agentic workers with explicit provider, model,
  reasoning pattern, endpoint policy and budgets. Pin the profile into compiled
  execution, enforce output guards, and reject classified results before values
  or their hashes enter durable state, simulation or reconciliation.
- Add Lumi as a separate environment-configured assistant, using an authenticated
  private gateway. Studio shares only selected context, validates proposed
  definitions, and requires explicit Apply with stale-draft protection and Undo.
- Improve Studio's step configuration, input mapping and human-task authoring;
  render decisions and parallel branches as separate lanes; apply valid field
  edits immediately; keep invalid drafts local; add semantic drag/drop and
  reachable node actions.
- Add schema revisions `0026_decision_tables`, `0027_lumi_configuration`, and
  `0028_files`. Upgrade through the documented maintenance procedure before
  starting the new server. Older grants do not automatically grant file or Lumi
  access.
- Package independent workers separately so Agentic's Python 3.13 requirement
  does not change the server's Python 3.12 support.
- Keep worker completion and failure retries alive after explicit capacity
  rejections while the lease and task deadline remain valid. Direct transport
  calls retain bounded retries; ambiguous outcomes are never retried automatically.

## 0.1.0a7

- Add saved platforms shared by the CLI, Studio, and desktop app: a non-secret
  `profiles.json` in the system configuration folder (`WEAVE_CONFIG_HOME`
  overrides it), written atomically with private permissions. Tokens stay in the
  system credential store, or in an explicit private file.
- Add `weave auth setup SERVER`. It checks the server, shows its sign-in settings
  for review, signs you in through the system browser (PKCE with a loopback
  callback) or a device code, lets you choose a workspace, and says what was
  saved and which command to run next. Explicit flags, `--yes`, and
  `--output json` keep it usable in scripts without prompts.
- Add `weave auth profiles`, `use`, `remove`, `workspace`, and
  `login --switch-account`. `weave auth status` exits 1 when you are not signed in.
- Add the public `GET /api/v1/client-configuration` endpoint, configured with
  `WEAVE_CLIENT_SIGN_IN` and `WEAVE_DISPLAY_NAME`. Clients treat it as a
  proposal: you review it once, the issuer, client, and endpoint origins are
  pinned, and a later change is refused with `WV-PROFILE-CHANGED`.
- Check server addresses before connecting: HTTPS except on loopback, no
  redirects, bounded responses, and no link-local, metadata, NAT64, or 6to4
  destinations.
- Rebuild the Studio connection wizard: work locally or connect to a platform,
  pick a saved platform, check the connection, review the sign-in settings, sign
  in through the system browser or a device code, and choose a workspace. Studio
  reconnects on reopen and never asks for your identity-provider password.
  Local authoring still needs no sign-in.
- Keep desktop pairing across restarts and save Studio exports through the
  system download handler.
- Add no-code REST integrations on the built-in `weave-http@2.0.0` connector:
  `weave connector http-action`, `weave connector import-openapi --target builtin`,
  guided `weave connections create` flags, connector descriptors, and the
  Studio **New API action** builder and OpenAPI import. Publishing now rejects an
  HTTP action whose configuration contradicts its method, with a pointer to
  `/spec/implementation/config`.
- Add `weave platform integrations` and `weave platform secret` so a local
  platform can run built-in HTTP actions with secrets stored by handle.
- Make the **Call an action** step usable end to end in Studio: choose an
  action and a connection, fill inputs through generated forms, map data,
  validate without a platform, save, reopen, and import or export YAML and JSON.
- Redesign the Studio editor: one primary command that follows the lifecycle,
  readable zoom with **Fit all** and **Tidy layout**, Delete with Undo, Decision
  conditions as rule rows with **Otherwise**, problems shown on the steps, an
  inspector with **Apply changes** and **Discard**, and a docked simulation that
  highlights the live path.
- Rework the Studio shell: **Build** and **Operate** navigation, a Home page
  that leads with what needs you, toasts with Undo, drafts kept on this computer
  with autosave, a run detail page for operators, consistent status names, and
  accessibility fixes for contrast, focus, and side panels at narrow widths.
- Register port-free loopback callbacks for the local Keycloak `weave-cli`
  client, and repair existing local platforms on `weave platform start`, so
  browser sign-in works on any loopback port.
- Keep in-process native workers running when they claim work while the
  platform is still opening effects; the dispatcher restarts a failed worker with
  backoff and reports not ready after three consecutive failures.
- Replay in-process native worker claims, renewals, and settlements that the
  platform rejects for capacity, with the same policy as remote workers, so a
  busy database no longer exhausts a task's retries.
- Accept JWKS signing keys that omit `alg`, as Microsoft Entra ID publishes them,
  for the allowed algorithm of their key type only (RSA for `RS256`, P-256 for
  `ES256`).
- Retry Studio reads, and changes that carry an idempotency key, when the
  platform turns them away for capacity (`WV-OPERATION-CAPACITY`,
  `WV-REQUEST-CAPACITY`), honoring `Retry-After` for up to four attempts.
- Show only the logo in the CLI help banner.
- Bump the Python package and browser Studio bundle to `0.1.0a7` and the desktop
  product to `0.1.0-alpha.7`; the native internal installer counter is `0.1.7`.
- Rewrite the documentation around four learning paths, with new guides for
  signing in, no-code REST integrations, and readers coming from BPM tools.

Verified on macOS against a real local Keycloak 26.7.4: CLI sign-in and
workspace selection with a private credential file, a no-code HTTP action
running on a local platform, the Studio sign-in journeys with the macOS
Keychain, and sign-in with a code, reconnection after a restart, and export in
a locally built macOS desktop app. In the desktop app, drafts kept on this
computer last only until the app quits; use **Save to file** to keep a copy.
Microsoft Entra ID and other OpenID
Connect providers are supported through configuration but **not verified**;
Entra-shaped keys are covered by unit tests only.
Credential storage on Windows and Linux is **not verified**. Existing
`--auth-config` connection files keep working. Schema revision remains
`0025_run_lifecycle`; no new database migration is added.

## 0.1.0a6

- Fix the macOS desktop outer application resource seal with explicit ad-hoc
  signing, replacing the damaged alpha5 macOS installers.
- Add strict `codesign` verification of the outer bundle, native executable, and
  frozen host, then repeat it against the application inside the read-only DMG
  before collecting release assets.
- Keep hardened runtime with the PyInstaller library-validation entitlement and
  smoke-test the host after signing, before collecting installers.
- Bump the matching Python host/browser bundle to `0.1.0a6` and desktop product
  to `0.1.0-alpha.6`; the native internal installer counter is `0.1.6`.
- Update installation guidance to use matching alpha6 artifacts and retain the
  browser fallback. Do not use the alpha5 macOS downloads.

macOS bundles are ad-hoc signed, **not Developer ID signed and not notarized**.
Resource-integrity verification does not establish Gatekeeper acceptance or
publisher trust. Manual approval may still be required by macOS and must comply
with local policy; no quarantine-removal workaround is recommended. Windows
publisher signing is also not provided. Schema revision remains
`0025_run_lifecycle`, as introduced by alpha5; no new database migration is added.

## 0.1.0a5

- Add the local Angular Studio with a branded workflow editor, structured canvas,
  source editing, API profiles, run inspection, human tasks, and email conversations.
- Add `weave studio`, browser pairing, a same-origin API bridge, and a versioned,
  checksummed optional asset bundle that runs without Node.js.
- Include the Studio host in the CLI installer's verified dependency closure;
  install the matching browser bundle separately with its published checksum.
- Add native human tasks with scoped assignments, schema-driven decisions, durable
  waits, and explicit operator pause/resume controls.
- Add generic SMTP/IMAP email integration, conversation replies, fenced submissions,
  inbound receipts, and authorized correlation to workflow runs.
- Add identity/workspace discovery and separate task and email permissions.
- Add exact business/correlation-key and status searches across independent runs,
  terminal-run archive/restore, and separately authorized permanent deletion with
  dependency checks, audit receipts, and protection against idempotent recreation.
- Add a guided Studio connection setup with reviewed CIAM configuration, cancellable
  device/PKCE sign-in, connection checks, and authorized workspace selection.
- Add scoped people/access administration through the API, SDK, CLI, and Studio,
  keeping CIAM account management separate from Weave identity links and grants.
- Add typed configuration for every workflow step, recursive expression editing,
  integration action discovery, workflow settings, and a centered Start/End canvas.
- Add a Tauri desktop wrapper, branded drag-to-Applications macOS disk image,
  and native installer build matrix. Desktop artifacts are unsigned.

These changes require the matching server, migrations through 0025, CLI, and Studio
build. Back up the database and follow the upgrade guide. This remains an alpha;
desktop signing/notarization and live CIAM/mail/cloud validation are separate
deployment acceptance steps. Native human tasks use IR v1alpha2; workflows that
do not use them retain the prior IR. The earlier intermittent queue timeout is
not claimed fixed by this release.

## 0.1.0a4

- Add `weave platform` commands for an owned local developer installation:
  prerequisite checks, setup, foreground startup, readiness, first run, token
  refresh, and stop/resume with retained data.
- Show stage progress and terminal-only animation during setup; keep JSON output
  clean and offer `WEAVE_NO_ANIMATION=1` for plain terminal messages.
- Introduce Lumi, the firefly guide, in ASCII CLI help and editable documentation SVGs.
- Export compiled workflow graphs as terminal text, Mermaid, or standalone SVG.
- Add opt-in same-origin Swagger UI at `/docs` and the actual contract at
  `/openapi.json`; authenticated operations retain their existing grants.
- Generate the complete public API reference from the same OpenAPI contract.
- Add step-by-step SDK, YAML/Python workflow, custom inbound/outbound connector,
  API playground, local platform, and remote deployment tutorials.
- Redesign the documentation diagrams with human labels, vector icons, and
  explicit flow explanations; annotate deployment command blocks with their purpose.

This remains an alpha. The local stack is a developer installation, not a
production availability claim. Cloud and live-provider validation remain
specific to the target environment. The earlier intermittent queue timeout
remains unresolved; no retry or runtime scheduling policy is changed here.

## 0.1.0a3

- Render the official woven-W logo as plain ASCII in CLI help, with a compact
  layout for narrow terminals and clean machine-readable output.
- Add `weave init DIRECTORY` for a validated offline starter with a runnable
  simulation and instructions for editing, recompiling, and simulating again.
- Add `weave docs [TOPIC]` and clearer help linking authoring, platform startup,
  worker deployment, and configuration to their guides.
- Publish branded, searchable MkDocs documentation on GitHub Pages with light
  and dark appearances, copyable commands, and complete task-based navigation.
- Explain the platform startup path, service responsibilities, readiness checks,
  first use, shutdown, and restart; distinguish optional container stages.
- Check the documentation site in CI and retain source attribution in stylesheets.

No runtime engine, database migration, authentication, or worker retry policy
changes are introduced. The intermittent queue timeout recorded in 0.1.0a2's
release notes remains an unresolved observation; this release does not claim to fix it.

## 0.1.0a2

- Add a user-local CLI installer with verified release assets, hash-locked client
  dependencies, isolated environments, safe upgrades, and managed uninstall.
- Add ASCII branding, grouped plain-language help, nested command help, and
  `weave --version`, while preserving machine-readable command output.
- Reorganize onboarding around local authoring, an existing API, and running the
  platform; explain authentication, scope, expected results, and recovery steps.
- Add illustrated CLI, Kubernetes, AWS, Azure, and Google Cloud deployment guides.
- Include installer assets, deployment templates, and the complete public
  documentation in the source distribution; verify installation during checks.

No database migrations or workflow-language changes are introduced in this release.

## 0.1.0a1

- Typed workflow schemas, expressions, bounded compilation and canonical artifacts.
- Draft/publication/activation lifecycle, durable PostgreSQL runtime, workers,
  waits, signals, schedules, simulation, history/replay and incident controls.
- Native PyFly API, Python SDK/CLI, provider-neutral identity boundaries and local grants.
- HTTP/webhooks, PostgreSQL, Kafka, integration outbox, connector authoring and provider inbox.
- Teams personal-bot text, WhatsApp Cloud API text/template/status scope, and
  Telegram webhook text integration with local fixture/backend verification.
- OpenAPI 3.1 connector generation with bounded HTTP profiles and offline diagnostics.
- Operational capacity policies, compatibility checks, retention controls and optional telemetry.
- Container packaging, remote worker deployment, and backup/restore guides.
- Editable SVG diagrams and Apache 2.0 source attribution.

See [current capabilities](docs/capabilities.md) for integration scope and
verification boundaries. Messaging connectors have local protocol verification;
live provider account provisioning and message delivery require separate validation.
