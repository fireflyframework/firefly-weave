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
