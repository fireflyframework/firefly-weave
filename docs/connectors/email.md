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

The `weave-email` package provides non-idempotent `send` and `reply` actions with
a durable submission fence, plus a leased IMAP source. See the
[threaded email guide](../guides/email.md) for connection configuration, server-owned
egress policy, authorization, SDK/CLI usage, mailbox baselines, and scoped routing.

The server policy environment variables are `WEAVE_MAIL_PRIVATE_NETWORKS`,
`WEAVE_MAIL_ALLOWED_PORTS`, and `WEAVE_MAIL_ALLOW_LOCAL_FIXTURE`. A connection cannot
self-authorize a private CIDR, arbitrary port, or plaintext transport. TLS is the
default; private-network and local-fixture access require explicit deployment policy.

Local acceptance exercises an initial mail admission, durable human approval,
SMTP reply with original threading headers, and an authorized follow-up signal in
the same run. Receipt replay does not resend the reply or accept the signal twice.
Live provider certification and OAuth refresh are separate deployment work.
