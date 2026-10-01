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

# Security policy

Firefly Weave is an alpha project. The [capability matrix](docs/capabilities.md)
describes current verification and limitations. No supported stable-release or
security-response SLA is announced by this source tree.

## Reporting a concern

Do not post credentials, private data or exploit details in a public issue.
Use [GitHub private vulnerability reporting](https://github.com/fireflyframework/firefly-weave/security/advisories/new)
to send a report privately to the repository maintainers. The package author
address is not a designated security response mailbox.

Prepare a minimal, redacted report with the affected source/package version,
configuration and trust boundary, reproduction steps against an owned fixture,
expected/observed behavior and impact. Use synthetic values and redact tokens,
connection strings, provider payloads, user data and local secret paths.

![Verified identity, local grants, and scoped secret resolution](docs/diagrams/security-boundaries.svg)

Use these boundaries to identify the affected component in a private report.
A verified token, a local permission, and a connector secret serve different roles.

[Open diagram at full size](docs/diagrams/security-boundaries.svg)

## Deployment boundaries

Use verified identity links and local scoped grants, separate migration/app/worker
credentials, explicit secret references, TLS and allowed-destination policies.
Never place secrets in workflow literals or ordinary outputs. Tenant RLS is one
layer alongside service authorization, not a replacement for it. Live-provider,
production restore/upgrade and operational guarantees require their own evidence.
See [identity and secrets](docs/operations/identity-and-secrets.md) and
[configuration](docs/operations/configuration.md).
