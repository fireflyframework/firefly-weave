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
