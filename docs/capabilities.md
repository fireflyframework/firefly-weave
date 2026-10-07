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

# Capabilities and verification

Use this page to decide whether Weave already does what you need, and how much
of it has been proven. It is written for anyone evaluating the platform:
architects, administrators, and teams planning an integration. Each row links to
the guide with the prerequisites and the exact supported shapes. To get something
running instead, start with the [local platform](guides/local-platform.md).

![Implementation, local checks, and live-provider evidence](diagrams/capability-evidence.svg)

Read the three columns as three separate questions: does the behavior exist,
what was exercised locally, and did it work against a real provider account. A
"yes" in one column never implies the next one.

[Open diagram at full size](diagrams/capability-evidence.svg)

## How to read the matrix

Read each row from left to right:

- **Implementation** says what code exists, and in which release.
- **Local verification** names the evidence exercised against fixtures, simulated
  providers, or services the tests own, such as a local PostgreSQL or Keycloak.
- **Live-provider verification** concerns a real external account or service. It
  is a separate claim; "Not run" means exactly that.
- **Main boundary** is the limit to keep in mind before you rely on the row.

"Not applicable" means the capability involves no external provider; it does not
mean that every possible destination or deployment was tested. For example, the
Telegram row supports planning a text integration, but bot registration, chat
permissions, and real delivery still need checking in your deployment.

## Which release this page describes

Weave is an alpha workflow and integration platform. This documentation targets
**`0.1.0a14`**, with PyFly 26.9.15 and database schema revision
`0030_worker_presence`, unchanged from alpha12. Alpha14 adds the detached Docker
development launcher and a credential-gated macOS notarization path. Actual Apple
notarization requires a Developer ID certificate and an accepted submission.
Alpha13 corrects worker context
and credential admission retries. The alpha13 checks and historical alpha12
delivery evidence below are separate. Download packages and checksums from
[GitHub Releases](https://github.com/fireflyframework/firefly-weave/releases).
It includes [Studio](guides/studio.md), [human tasks and operator
pause and resume](guides/human-tasks.md), [email conversations and
triggers](connectors/email.md), [people and access administration](guides/people-and-access.md),
and [execution management](guides/execution-management.md).

Rows marked **new in 0.1.0a7** describe features that alpha6 and earlier
releases do not have. Alpha8 adds three migrations; see the
[upgrade procedure](operations/upgrades.md).

## Alpha14 local development verification

The Docker launcher was exercised on October 7, 2026, on macOS Apple Silicon
with Docker Desktop. This is a separate local environment from the Azure
deployment recorded below.

| Verification | Verified scope | Limit |
| --- | --- | --- |
| Docker platform lifecycle | Built the retained server image, started the API and identity services, created a person and a successful saved run, then stopped and resumed the installation; account, run, and unrelated container identities were unchanged | One Docker Desktop context on one computer; no production high-availability or cloud provisioning claim |
| CLI sign-in | Installed CLI completed browser PKCE S256 against real local Keycloak, checked the authenticated person's workspace, and revoked and removed its isolated test credential | The private file credential store was used; the user's existing profile was not changed |
| Studio sign-in | Browser tests completed setup, real Keycloak sign-in, workspace selection, run and workflow reads, silent recovery after a Studio host restart, and sign-out with revocation; native test credentials were removed | Browser Studio on macOS with the system credential store; this does not establish the same interaction in every native desktop installer |
| Signing preparation | Unit tests exercise release-tag guards, credential cleanup, signature and ticket requirements, and mounted DMG verification | No Apple Developer ID identity or notarization submission was available; ordinary macOS installers remain ad-hoc signed |

## Alpha13 verification

The published tag is [`v0.1.0a13`](https://github.com/fireflyframework/firefly-weave/releases/tag/v0.1.0a13),
commit `2434ba5386e4f1557e07474245805833543965c3`. The following checks
were completed on October 7, 2026:

| Verification | Verified scope | Limit |
| --- | --- | --- |
| Published packages and desktop installers | All 32 release assets were downloaded again and hash-checked: 14 core assets and 18 native assets, including six installers across Apple Silicon macOS, Intel macOS, Windows x64 and Linux x64; all five checksum manifests verified | The [exact-tag desktop build](https://github.com/fireflyframework/firefly-weave/actions/runs/37609308896) passed all four targets; macOS is ad-hoc signed and not notarized, Windows and Linux are unsigned, and packaging checks do not establish interactive sign-in on every platform |
| Remote release checks | [Main Checks](https://github.com/fireflyframework/firefly-weave/actions/runs/37609221831), [tag Checks](https://github.com/fireflyframework/firefly-weave/actions/runs/37609308918) and [Documentation](https://github.com/fireflyframework/firefly-weave/actions/runs/37609221845) completed successfully for the exact commit above | Remote checks do not establish Azure deployment or paid model acceptance |
| Core and installed packages | All 17 normal check stages passed, including 3,654 unit/contract cases, 76 Agentic worker cases, 29 Files worker cases, and installed-artifact checks | These counts describe the alpha13 source and packages; they do not repeat the historical alpha12 browser or desktop acceptance |
| Affected backend paths | All 94 selected integration cases passed without skips, including native HTTP, PostgreSQL and Kafka execution in fresh alpha13 images | This is affected-case verification, not a fresh full integration, process-recovery or queue-load run |
| Installed worker interoperability | Installed Agentic 0.1.5 with the alpha13 SDK completed a task against the unchanged alpha12 API: claim, context, credentials, heartbeat and completion; four controlled capacity rejections recovered; an unauthorized connection was denied | One simulated model response and no paid provider call; this proves the tested package combination, not arbitrary mixed-version compatibility |
| Schema and protocol | The tested core artifacts have identical API, worker-contract and persistence payloads; schema remains `0030_worker_presence` | Installing a client or worker does not migrate the API or database |
| Azure AI acceptance | Agentic 0.1.5 with the alpha13 SDK completed one new workflow against the alpha12 API using Azure OpenAI `gpt-4o-mini` through `azure-chat`; exactly one model request returned the expected `{"ok": true}` result; replay was consistent through sequence 2 | Replay reported `authorization_verified: false`; this is one tested deployment and model, not general mixed-version or provider certification |

This accepted deployment combines the alpha13 SDK and Agentic 0.1.5 with the
alpha12 API, Lumi and Operations services; its database remains at schema 0030.
Only the workflow's Agentic release pin changed. The new successful run did not
replace the suspended alpha12 AI run or alter retained histories. The existing
Lumi configuration and acceptance were preserved, with no new Lumi call. The
successful HTTP, Lumi and Operations receipts below remain historical evidence.

## Alpha12 delivery and acceptance

This historical delivery used tag `v0.1.0a12`, commit
`230a3aba17a4da76a5197edeec15fb4ceb6428c9`. Publication and environment
acceptance have separate evidence:

| Delivery | Verified scope | Limit |
| --- | --- | --- |
| API, CLI, SDK, worker packages, dependency locks and Studio bundle | 14 published assets downloaded again and checked against their hashes | Installing clients does not migrate a server |
| Desktop installers | Six installers across Apple Silicon macOS, Intel macOS, Windows x64 and Linux x64; all 18 native assets downloaded and checked | macOS is ad-hoc signed and not notarized; Windows installers are unsigned; packaging checks do not establish interactive sign-in on every platform |
| Core and installed packages | All 17 normal check stages passed, including 3,622 unit/contract cases and installed-artifact checks | Local fixtures do not certify external provider accounts |
| Backend acceptance | Exact combined coverage of 629 integration and 69 process cases; four fresh queue cases completed 400 tasks with 400 effects and zero task retries | Coverage combines retained passing cases with reviewed affected-case reruns; it is not one successful full release command |
| Studio | 914 browser cases passed | Six opt-in authentication cases were skipped in this run |
| Azure preproduction | Alpha12 API migrated to schema 0030; real HTTP run and replay passed; separate Lumi request passed; the Operations runner observed three applications and applied a reviewed same-image Lumi adoption | Alpha12 AI workflow acceptance found a credential-capacity retry defect; its suspended run was retained. The corrected worker's separate alpha13 acceptance is recorded above |

The [exact-tag desktop build](https://github.com/fireflyframework/firefly-weave/actions/runs/37595716933)
verified all four target builds and their installer manifests. Both downloaded
macOS apps passed nested and outer bundle seal checks. None of this extends the
earlier AI or human identity evidence to later versions. The alpha12 AI failure
is retained as evidence; it is not counted as a successful model run.

## Capability matrix

| Capability | Implementation | Local verification | Live-provider verification | Main boundary |
| --- | --- | --- | --- | --- |
| [Files](guides/files.md) | New in alpha8: scoped, resumable transfer API, SDK/CLI helpers, Studio fields and human-task attachments | PostgreSQL lease/claim/retention checks; checksum and chunk tests; Studio browser tests | Not applicable to platform storage | 25 MiB per file; content stored in PostgreSQL; no automatic abandoned-upload cleanup |
| [File integrations](guides/file-connectors.md) | FTP, FTPS, SFTP, SharePoint/OneDrive and Google Drive workers | Real local FTP/FTPS/SFTP servers; simulated Graph and Drive responses | Microsoft and Google accounts not exercised | Six explicit operations; no standalone change-feed trigger yet |
| [Decision tables](reference/decision-tables.md) | Versioned decision definitions and a dedicated workflow step | Compiler, API and PostgreSQL execution tests | Not applicable | Deterministic rule evaluation; no arbitrary expression execution |
| [AI tasks](guides/ai-workers.md) | Per-workflow model profiles executed by an independent Agentic worker | Real Agentic library with controlled model transport; PostgreSQL completion/replay and secret classification | Agentic 0.1.5 with the alpha13 SDK completed one Azure OpenAI workflow against the alpha12 API; accepted result and consistent replay checked | Replay did not verify authorization; exact operator model/endpoint policy applies, and external requests can be ambiguous |
| [Lumi assistant](guides/lumi.md) | Separate environment profile and private gateway; opt-in context and reviewed Studio draft proposals | API authorization tests; 7-size Studio browser checks | Azure OpenAI response through the separate Lumi gateway on alpha12; installed alpha10 Studio configuration readback against Azure preproduction | No autonomous publish, activation, or execution; conversation stays in memory |
| [Compiler and schemas](reference/compiler.md) | Implemented | Compiler, schema, expression, and canonical-artifact suites | Not applicable | Validation without a catalog is partial and produces no artifact |
| [Definition lifecycle](reference/api.md) | Implemented | Publication, activation, revision, and scoped API suites | Not applicable | Compiling is separate from authorization and admission |
| [Durable runtime](reference/schedules-and-timers.md) | Implemented | PostgreSQL, lease, wait, signal, parallel, schedule, and recovery scenarios | Not applicable | External effects can repeat after a crash |
| [Human tasks](guides/human-tasks.md) | Implemented | PostgreSQL scenarios for assignment, claim, decision, and expiry; Studio browser tests | Not applicable | Each environment must bind assignments; expiry times out the whole run |
| [Execution management](guides/execution-management.md) | Implemented | Run lifecycle scenarios: search by business key, archive, restore, and purge | Not applicable | Purge needs an archived, finished run and a separate permission |
| [Workers](reference/worker-protocol.md) | Implemented | Native and remote worker protocol scenarios | Not applicable | Release identity and scoped grants must be provisioned |
| [Simulation](reference/simulation.md), [history and replay](reference/history-and-replay.md), [incidents](reference/incident-operations.md) | Implemented | Mock simulation and durable evidence and control scenarios | Not applicable | Replay can report incomplete evidence; simulation never calls providers |
| [Studio](guides/studio.md) in the browser | Implemented; the connection steps, saved platforms, and the redesigned editor are **new in 0.1.0a7** | Unit tests; browser tests against a mocked platform API and a real local host; an opt-in browser test against a local platform and its Keycloak on macOS (connect, sign in, renew, switch account, sign out, and a REST call built in Studio) | Installed alpha10 AI setup against Azure preproduction using an application identity; interactive Entra human sign-in not exercised | Studio is a client: it grants nothing, and pairing is not a sign-in |
| [Studio desktop](guides/desktop.md) | Implemented for macOS, Windows, and Linux targets | Native shell and host unit tests; exports checked in a macOS WebKit test harness; a locally built macOS app against the local Keycloak 26.7.4: sign-in with a code, reconnection after a restart, and **Save to file** | Browser sign-in from the app and the DMG not verified; not run on Windows or Linux | macOS bundles are ad-hoc signed, not notarized; Windows installers are unsigned |
| [Saved platforms and CLI sign-in](guides/connect-to-api.md) | **New in 0.1.0a7**: `weave auth setup`, saved platforms shared by the CLI, Studio, and desktop app | A real run against the local Keycloak, keeping tokens in a private credential file; Studio's opt-in browser test and the locally built macOS app used the macOS Keychain | Windows Credential Locker, Secret Service, and KWallet were not exercised end to end; they are covered only by unit tests with simulated backends | Saved platforms hold no secrets; a sign-in alone grants no access |
| [Keycloak identity](operations/identity-and-secrets.md#local-keycloak-development) | Implemented, including published sign-in settings for the CLI and Studio | Owned Keycloak 26.7.4: CLI browser sign-in with PKCE and device sign-in, renewal with refresh-token rotation, revocation, unlinked and no-access accounts, local authorization scenarios | No customer realm certification; in the desktop app, only sign-in with a code was verified | Explicit identity links and local grants remain required |
| [Generic OIDC / Entra ID](operations/identity-and-secrets.md#provider-notes) | Configurable token verification and published sign-in settings; provider-neutral ports and claim mapping present | Local contract coverage with simulated providers | Real Entra application tokens verified for a host application and independent Agentic worker in Azure preproduction; Entra human browser/device sign-in and other CIAM remain unverified | No automatic provisioning, group-overage expansion, or provider portability guarantee |
| [REST calls without code](connectors/http-without-code.md) | **New in 0.1.0a7**: `weave connector http-action`, `import-openapi --target builtin`, guided `weave connections create`, and Studio's API action builder, on the built-in `weave-http@2.0.0` | Offline Action and import checks, compile-time profile checks, and the guide's local platform example; the guide states what ran end to end | No commercial API account contacted | One JSON operation over HTTPS per Action; only the connection's listed origins; private networks refused unless an operator allows them |
| [Local platform](guides/local-platform.md) (`weave platform`) | Implemented; `user`, `integrations`, and `secret` are **new in 0.1.0a7** | Runs on macOS with a local Docker engine and Keycloak 26.7.4 | Not applicable | Development only, on macOS or Linux with a Unix-socket Docker context |
| [HTTP and webhooks](reference/http-and-webhooks.md) | Implemented | Bounded transport, signed ingress, and owned endpoint scenarios | Not applicable to arbitrary destinations | Egress and scoped credential policy apply |
| [OpenAPI import](connectors/metadata-import.md) and [HTTP profiles](connectors/http-profiles.md) | Implemented: connector packages; Actions on the built-in connector are **new in 0.1.0a7** | Offline importer, generated package, and owned TLS integration scenarios | Not run | Supported operations only; do not infer that an arbitrary OpenAPI document runs |
| [PostgreSQL](connectors/postgresql.md) | Implemented | Owned PostgreSQL connector scenarios | No customer database certification | Explicit SQL policy and typed parameters |
| [Kafka](connectors/kafka.md) | Implemented | Owned broker and database delivery and recovery scenarios | No hosted-broker certification | Database receipts and broker acknowledgments have different boundaries |
| [Integration events and outbox](reference/integration-events.md) | Implemented | Transactional delivery and recovery scenarios | No arbitrary subscriber certification | At-least-once delivery requires an idempotent receiver |
| [Connector authoring](connectors/authoring.md), [provider sources](reference/provider-sources.md) | Implemented | Package and conformance, source authority, and inbox scenarios | Not applicable to third-party packages | Installing trusted code is separate from validating metadata |
| [Email conversations](connectors/email.md) | Implemented | Local acceptance: mail admission, a durable approval, a threaded SMTP reply, and a follow-up signal | Not run | Live provider certification and OAuth token refresh are deployment work |
| [Teams personal bot](connectors/teams.md) | Implemented | Wire fixtures and owned PostgreSQL; composed provider gate passed | Not run | Personal text scope; provider acceptance is not delivery |
| [WhatsApp Cloud API](connectors/whatsapp.md) | Implemented | Wire fixtures, status history, and owned PostgreSQL; composed provider gate passed | Not run | Account policies, templates, and live delivery need separate verification |
| [Telegram webhook text](connectors/telegram.md) | Implemented | Local TLS fixtures and owned PostgreSQL; composed provider gate passed | Not run | Source-local deduplication; bot and group permissions are provider prerequisites |
| [Operational limits](operations/configuration.md), [telemetry](operations/observability.md), [retention](operations/retention.md), and [compatibility](operations/upgrades.md) | Implemented | Policy, accounting, contention, telemetry, retention, and upgrade suites | Not applicable | Logical accounting is separate from physical storage; compatibility checks gate readiness |
| [Container deployment Operations](operations/cluster-management.md) | New in alpha12: scoped targets, observations, immutable reviewed plans and outbound runners | Local Compose lifecycle, adapter policy tests, authorization and reconciliation scenarios | Azure alpha12: complete three-application observation and reviewed same-image Lumi adoption; preserved configuration and verified all historical replicas | Azure target and runner policies keep `scale_workers` disabled; active-revision observations do not prove historical replicas have stopped |
| [Deployment](operations/deployment.md), [backup and restore](operations/backup-restore.md), and distribution | Implemented | Installed-package, container, process recovery, restore, and queue suites | Not applicable | Restore ownership, grants, schema compatibility, and external systems must be checked in each environment |
| [AWS, Azure, and GCP deployment recipes](operations/cloud-deployment.md) | Reference guides and Kubernetes manifests | Source review and offline Kubernetes object-shape validation | A separate Azure Container Apps deployment has been exercised; these Kubernetes recipes and AWS/GCP remain unverified | Operator-provisioned infrastructure; database migrations, identity, networking, and recovery need acceptance in the target |
| Slack and Salesforce | Deferred | Not run | Not run | Not part of this delivery |

## What is not included

- **BPMN.** Weave does not import or export BPMN files. Subprocesses, loops,
  compensation, and error boundary events are not part of the workflow language;
  [Coming from BPM/BPMN](concepts.md#coming-from-bpmbpmn) lists the alternatives.
- **Named enterprise adapters.** SAP and Oracle are not offered as executable
  named adapters. Reaching a system through a generic connector does not certify
  that system's authentication, schema, rate limits, or business semantics.
- **Production proof.** Local verification includes offline contracts, owned
  PostgreSQL and Keycloak services, and synthetic HTTP and TLS fixtures. It does
  not establish production availability, provider account provisioning, or real
  message delivery.

## Next steps

- [Start here](guides/learning-path.md): choose the path for your role.
- [Architecture](architecture.md): see how the modules fit together.
- [Security policy](../SECURITY.md): report a vulnerability.
- [Documentation home](README.md): find the guide for your task.
