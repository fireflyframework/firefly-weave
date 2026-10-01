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

![Troubleshooting from dependencies through external-effect evidence](../diagrams/operations-evidence.svg)

Begin at the first boundary that has not been verified, then follow the corresponding checks below. A successful earlier stage does not prove the later one: readiness does not establish authorization, task completion, or provider delivery.

[Open diagram at full size](../diagrams/operations-evidence.svg)

## Locate the failing stage first

Work through this sequence without rerunning provisioning:

1. **Dependencies:** in the standalone operator terminal, confirm the selected
   PostgreSQL and Keycloak services are running under the expected context/project.
   Then repeat the standalone PostgreSQL health and Keycloak discovery checks.
2. **API startup:** inspect terminal 2 for the safe startup error. Schema rejection
   requires explicit migration diagnosis; database authority rejection requires
   the correct nonowner runtime configuration.
3. **Readiness:** check `/health/ready` on the port of the API you actually started.
   The foreground API uses `WEAVE_API_PORT`; the optional container API uses
   `WEAVE_CONTAINER_API_PORT`. A reserved port is not evidence of a running API.
4. **Identity and scope:** distinguish a token-acquisition failure from a Weave
   denial. Verify the local identity link and grants for the exact scope IDs.
5. **Execution:** when admission succeeds but a run does not complete, inspect the
   run and its pinned release/connection, worker state, and incident history.
6. **External result:** inspect independent receiver/provider receipts before
   deciding whether a timed-out effect is safe to retry.

From the repository root, this read-only command lists only the selected local
supporting services. It needs the original standalone environment; it does not
create a replacement project:

```sh
docker --context "$WEAVE_DOCKER_CONTEXT" compose --project-name "$WEAVE_LAUNCH_ID" \
  --env-file "$WEAVE_WORK_DIR/postgres.env" --env-file "$WEAVE_WORK_DIR/identity.env" \
  -f compose.yaml -f compose.identity.yaml ps
```

For a host-side readiness check, run this from the operator terminal:

```sh
"$WEAVE_PYTHON" - <<'PY'
import os, httpx
url = "http://127.0.0.1:" + os.environ["WEAVE_API_PORT"] + "/health/ready"
try:
    response = httpx.get(url, timeout=5, trust_env=False)
except httpx.HTTPError:
    raise SystemExit("API readiness endpoint is unreachable; check the selected port and API process") from None
print("Readiness HTTP status:", response.status_code)
PY
```

Expected: HTTP 200. An unreachable endpoint points first to startup/routing;
a non-200 response from the intended API calls for its startup/compatibility
diagnostics. Do not publish a full environment dump to explain either case.

## Match the symptom to its boundary

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
| Setup says output already exists | Private work directory and earlier receipt completeness | Scripts refuse replacement; reuse complete existing configuration or investigate the retained attempt |
| Worker container exits after a short successful run | Token lifetime, drain budget, and container exit state | The example has a finite invocation and no automatic restart policy; follow [restart](deployment.md#restart-a-stopped-local-deployment) |
| Worker cannot reach API/receiver | API bind address, receiver terminal, container-visible host address | Host loopback is not container loopback; the generated worker deployment does not add a Linux host-gateway mapping |
| Container runs but no task completes | Admitted release, current scoped worker grant, activation pin, and actual run state | Container liveness is not worker readiness |
| Restart ignores changed env file | Existing container configuration | Docker restart and `up --no-recreate` do not load changed env files |
| Restored installation rejects old source connections | `trial.json`/`restore.json` and selected runtime file | The restore helper deliberately leaves the source fenced; select only the intended restored target |
| Local test backend missing | Fixture prerequisites, explicit context/ports/owned resources | Requested backend gates fail; they must not silently skip |

Consult [Teams](../connectors/teams.md), [WhatsApp](../connectors/whatsapp.md),
[Telegram](../connectors/telegram.md), [Kafka](../connectors/kafka.md),
[PostgreSQL](../connectors/postgresql.md) and [HTTP/webhooks](../reference/http-and-webhooks.md)
for their specific admission and outcome boundaries. Use [backup and restore](backup-restore.md)
for fenced recovery into a separate database; keep the original database and
private recovery evidence until the restored service has been verified.

## Collect a useful incident report

Record the installed artifact version/hash, safe diagnostic code, server-issued
request ID, operation, affected scope/run IDs, approximate time, and which stage
failed. State whether the request was accepted, whether an external effect may
have happened, and which durable evidence you inspected. Keep detailed receipts
and logs in protected storage and share only the authorized scope's information.

Avoid turning uncertainty into a destructive reset. Recreating a realm, deleting
a volume, editing schema markers, or fabricating a successful completion removes
evidence without establishing the cause. Use the linked procedure for the failed
boundary, preserve partial state, and repeat the smallest relevant verification
after the correction.
