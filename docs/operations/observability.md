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

# Metrics and tracing

Weave owns its OpenTelemetry providers inside each native PyFly application.
Export is disabled by default. Starting the compiler, using the SDK, or setting
ambient `OTEL_EXPORTER_*` variables does not enable Weave exports. No provider is
registered as a process global.

![Diagnostic stages and the evidence appropriate to each boundary](../diagrams/operations-evidence.svg)

Read downward through dependency, readiness, authorization, runtime, and external-effect checks. Metrics and traces are a parallel observation channel: use the stored run and independent external receipts to establish an outcome, including when export is incomplete.

[Open diagram at full size](../diagrams/operations-evidence.svg)

## Choose the evidence you need

| Question | Start here | What it can establish |
| --- | --- | --- |
| Can this replica accept work? | `/health/ready` and the scoped compatibility report | Runtime readiness and blocking requirements |
| How is the service behaving over time? | Collector metrics and traces | Bounded operational observations, subject to export loss |
| Did a run or effect complete? | Authorized run/history and receiver/provider receipts | Durable recorded outcome and external acceptance evidence |
| Why was access allowed or denied? | Protected authorization logs and access audit | Authorization decision context, subject to configured log retention |

Metrics and traces complement the stored execution history. They are useful for
trends, queue age, and failure investigation, but an absent span does not prove an
operation never occurred. See [local runtime](../reference/local-runtime.md) for
authorization logging and [history/replay](../reference/history-and-replay.md) for
run evidence.

## Enable a collector

The repository's local Compose setup does not start an OpenTelemetry collector.
Supply your own collector and confirm that its HTTP/protobuf receiver is enabled
before changing Weave. The example below assumes it is already listening on the
host's loopback port 4318.

Prerequisites: a running Weave API, a collector accepting OTLP over HTTP/protobuf,
and an endpoint reachable from the API process. Use HTTPS outside loopback
local development. Configure exact signal URLs; Weave does not append paths or
discover a collector. Endpoints cannot include URL credentials, queries, or fragments.

For the standalone foreground API, stop it with Ctrl-C in terminal 2, then set
`WEAVE_TELEMETRY` in that same shell before running its normal startup command.
For a managed deployment, place the equivalent value in the API's protected
environment configuration. A shell `export` in terminal 1 does not change an API
already running in terminal 2 or inside a container:

```sh
export WEAVE_TELEMETRY='{"enabled":true,"traces_endpoint":"http://127.0.0.1:4318/v1/traces","metrics_endpoint":"http://127.0.0.1:4318/v1/metrics","timeout_seconds":5,"metrics_interval_seconds":30}'
```

This example targets a collector running on the same host as the API. Container
loopback refers to that container. For other deployment topologies, configure
explicit HTTPS collector URLs. Either signal endpoint may be omitted. At least
one endpoint is required when `enabled` is true.

Collectors requiring authentication may use the optional `authorization` field.
Supply its value through a protected deployment secret/environment file, not a
command argument, source file, URL, or workflow definition. Do not print the
configuration. Ambient OTLP headers, proxy settings, resource detectors and
credential-provider hooks are not used by the owned exporters.

Restart the owned API with its normal startup command, check its readiness, then
exercise an authorized request, such as reading the first-run ID through the CLI.
Wait at least the configured metrics interval (30 seconds in this example) before
checking the collector's metric data. Look for resource `service.name=firefly-weave`,
instrumentation scope `firefly.weave`, and fixed `weave.*` metric/span names in the
collector. Export failure does not change workflow state or grant execution
authority. Use collector health and Weave's readiness independently: a healthy
API is not proof that an external collector received data.

### Check that export actually arrived

1. Confirm the collector sees traffic from the correct API process or container.
   Check its OTLP receiver status separately from any downstream dashboard.
2. Find the service resource and fixed instrumentation scope listed above. Filter
   by a fixed operation/status attribute rather than expecting a run ID label.
3. Compare the request you exercised with the corresponding operation observation.
   For a workflow transition, confirm the run's durable state through the API too.
4. If nothing arrives, use the table below before increasing logging. Export is
   best effort and deliberately does not retry failed requests indefinitely.

## Data and bounds

Metric attributes are limited to `operation`, `status`, and `error_code`.
Operations come from the public operation manifest and fixed internal operation
names; status and error codes use bounded allowlists. Unknown values map to
`other` before they reach OpenTelemetry. Tenant, principal, workflow, run, task,
connection, source and message identifiers are not metric labels. Request paths,
queries, phone numbers, payloads, credentials and exception messages are excluded.

Each instrument has at most 256 dimension combinations; excess combinations are
aggregated into a fixed `other` series. Counter, histogram and gauge names are
fixed. Duration and queue-age histograms use seconds. Unknown legacy enqueue time
is counted separately; it is not replaced with migration time. An incomplete
inventory sweep does not replace the last complete inventory gauge. Interpret
inventory and freshness together, and use **max**, not sum, for duplicate
per-replica observations of the same global inventory.

Authoritative transition counters are queued in the owning transaction and
emitted only after successful commit. Rollbacks discard those callbacks. A full
callback buffer drops telemetry, not business state. These counters are
best-effort operational observations; durable receipts and audit records remain
the authoritative evidence.

The trace queue holds at most 256 waiting spans and sends at most 64 per batch.
There is one exporter thread per enabled trace provider. Full queues drop new
spans and increment the bounded drop counter; they never create an unbounded
retry spool. Export requests are limited to 1 MiB. The configured HTTP timeout
is at most five seconds; there are no automatic retries or redirects, and
response bodies are not retained. Collector delays can cause telemetry loss.
Stopping the API closes its owned providers; remaining queued spans may be
counted as dropped rather than delaying shutdown indefinitely.

Owned spans use fixed operation names. They do not automatically record exception
events or raw exception descriptions. Only a validated W3C version-00 traceparent
is propagated; baggage and tracestate are discarded. Native durable boundaries
can link up to eight validated parent contexts. Existing remote worker-v1 leases
do not carry tracing context; server claim/completion observations do not imply
end-to-end remote-worker trace continuity.

## Disable or troubleshoot

Set `WEAVE_TELEMETRY='{"enabled":false}'` and restart the owned API to disable
export. This does not disable authorization audit records or delete workflow
history. Stop only the API/collector services you own; no volume deletion is
needed.

| Symptom | First check | Next step |
| --- | --- | --- |
| No export traffic | `WEAVE_TELEMETRY.enabled` on the actual API process | Restart/redeploy with explicit configuration; ambient OTLP variables are insufficient |
| Connection refused | Collector process and endpoint namespace | Host loopback works only for a host API; use a reachable trusted URL for a container API |
| Collector rejects requests | HTTP/protobuf receiver, exact `/v1/traces` or `/v1/metrics` path, TLS, authorization | Correct the receiver/configuration; do not put credentials in the URL |
| Metrics seem doubled | Multiple replicas report the same inventory | Use max for duplicate global inventory observations and inspect freshness |
| Drop counter increases | Collector latency or queue pressure | Repair the export path; durable workflow state is not dropped with telemetry |
| A run ID cannot be found in labels | Bounded attribute policy | Inspect the run through authorized history instead |

If exports are absent, verify the exact endpoint, TLS trust, collector
HTTP/protobuf support, private authorization value and network reachability from
the API's own namespace. An increased `weave.telemetry_dropped` count means
observations were lost; inspect the collector independently. Do not enable raw
HTTP request logging or attach payloads to spans to diagnose a credentialed
connection. Generic PyFly request logging, global tracing filters and generic
server metrics stay disabled in Weave's packaged configuration so they cannot
bypass the owned attribute policy.

See [configuration](configuration.md), [identity and secrets](identity-and-secrets.md),
and [troubleshooting](troubleshooting.md) for the surrounding operator setup.
