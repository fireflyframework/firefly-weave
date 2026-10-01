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

# Troubleshooting

Start with the safe diagnostic code, request ID and current scope. Do not paste
provider bodies, tokens, connection strings or private environment files into
logs or reports. Use [history/replay](../reference/history-and-replay.md) and
[incident operations](../reference/incident-operations.md) for durable evidence.

| Symptom | Check | Interpretation |
| --- | --- | --- |
| Validation succeeds but no artifact exists | `partial`, catalog input and compiler diagnostics | Partial validation without a catalog is expected |
| Native OpenAPI/remote client import fails | Installed `openapi` or `client` extra and locked framework provenance | The base compiler intentionally omits runtime dependencies |
| Server rejects schema | Installed packaged migrations and explicit migration authority | Startup does not migrate or repair an incompatible database |
| Readiness stays restricted | Scheduler catalog credentials, compatibility report, policy fingerprint, and retained requirements | Disabling the scheduler does not bypass startup inventory; see [upgrades](upgrades.md) |
| Work admission reaches capacity | Authorized usage/capabilities response, retained records, and configured limits | Admission limits are separate from physical disk space; inspect [retention](retention.md) before maintenance |
| Collector receives no telemetry | Explicit `WEAVE_TELEMETRY`, exact signal URL, collector TLS/authentication, and network namespace | Export is disabled by default; see [observability](observability.md) |
| Verified token receives denial | Exact issuer/audience/client/token purpose, local identity link and active scope grants | Authentication and authorization are separate |
| Keycloak template change has no effect | Existing realm/client configuration | Realm import skips retained realms; do not reset them |
| Workflow cannot activate | Connection revisions, worker releases, grants and pinned catalog | Compilation does not provision execution resources |
| Stale worker completion | Lease generation, heartbeat/deadline and recovery history | Do not retry completion with fabricated authority |
| Provider receipt ignored/conflicting | Source identity, recognized update class and receipt facts | Deduplication is source-local; conflicting facts are not a new event |
| Provider send timeout | Whether dispatch occurred and connector outcome contract | Effect may be unknown; automatic retry can duplicate it |
| Replay reports incomplete | Redaction/omitted historical payloads and retained event prefix | Incomplete evidence must not be converted into successful replay |
| Local test backend missing | Fixture prerequisites, explicit context/ports/owned resources | Requested backend gates fail; they must not silently skip |

Consult [Teams](../connectors/teams.md), [WhatsApp](../connectors/whatsapp.md),
[Telegram](../connectors/telegram.md), [Kafka](../connectors/kafka.md),
[PostgreSQL](../connectors/postgresql.md) and [HTTP/webhooks](../reference/http-and-webhooks.md)
for their specific admission and outcome boundaries. Use [backup and restore](backup-restore.md)
for fenced recovery into a separate database; keep the original database and
private recovery evidence until the restored service has been verified.
