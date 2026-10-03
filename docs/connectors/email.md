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

# Generic SMTP and IMAP connector

The `weave-email` connector lets a workflow send email and reply within a
conversation over SMTP, and lets incoming mail from an IMAP mailbox start or
signal a workflow. This page is a short overview for the operator who enables it
and the author who decides whether it fits. The
[threaded email guide](../guides/email.md) has the full walkthrough: connection
configuration, authorization, SDK and CLI usage, mailbox baselines, and scoped
routing.

## What it provides

| Part | What it does |
| --- | --- |
| `send` and `reply` Actions | Submit one message, or a reply that keeps the original threading headers. Both are `non_idempotent`, with a durable submission fence: an ambiguous SMTP result ends as `unknown` and is never resent automatically |
| IMAP source | A leased, read-only mailbox poll that records receipts and starts or signals workflows. It ingests new messages only |
| Conversations | Scoped APIs to read threads and queue replies, with separate read and send permissions |

## Before you start

- **A deployed platform.** The [local platform](../guides/local-platform.md)
  never enables connector packages, so it cannot run this connector.
- **The package enabled.** The operator selects
  `firefly-weave:weave-email:firefly_weave.connectors.email:package` in
  `WEAVE_CONNECTOR_PACKAGES` and publishes its Connector, following
  [package publication and activation](authoring.md#from-package-to-an-executable-workflow).
- **A mail account and its secret handles.** Credentials live behind
  `secretRef` handles (`username` and `password`, or `username` and `token`),
  never in configuration.
- **Explicit roles.** `email_reader`, `email_sender`, or `email_manager`, plus the
  existing roles for binding connections and running workflows; see
  [Give people the right access](../guides/people-and-access.md).

## Server-owned network policy

A connection cannot authorize itself to reach a private network, an arbitrary
port, or a plaintext server. The operator sets these server variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `WEAVE_MAIL_PRIVATE_NETWORKS` | JSON array of private CIDRs mail may reach | `[]` (no private networks) |
| `WEAVE_MAIL_ALLOWED_PORTS` | JSON array of permitted SMTP and IMAP ports | `[25,465,587,143,993]` |
| `WEAVE_MAIL_ALLOW_LOCAL_FIXTURE` | `true` permits plaintext to a loopback fixture for owned protocol tests | `false` |

TLS is the default, and certificates and host names are verified. Plaintext
requires the server-enabled local-fixture policy, an explicitly permitted
loopback port, and no credentials.

## What has been verified

Local acceptance exercises an initial mail admission, a durable human approval,
an SMTP reply with the original threading headers, and an authorized follow-up
signal in the same run. Receipt replay does not resend the reply or accept the
signal twice. Live provider certification and OAuth token refresh are separate
deployment work.

## Next steps

- Build the complete flow: [Build an email conversation workflow](../guides/email.md).
- Put a person in the loop before replying: [Human tasks and approvals](../guides/human-tasks.md).
- Handle a send that ended `unknown`: [Incident operations](../reference/incident-operations.md).
