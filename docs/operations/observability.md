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

## Enable a collector

Prerequisites: a running Weave API, a collector accepting OTLP over HTTP/protobuf,
and an endpoint reachable from the API process. Use HTTPS outside loopback
local development. Configure exact signal URLs; Weave does not append paths or
discover a collector. Endpoints cannot include URL credentials, queries, or fragments.

Set `WEAVE_TELEMETRY` in the API's private environment file before starting it:

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
exercise an authorized request. Look for resource `service.name=firefly-weave`,
instrumentation scope `firefly.weave`, and fixed `weave.*` metric/span names in the
collector. Export failure does not change workflow state or grant execution
authority. Use collector health and Weave's readiness independently: a healthy
API is not proof that an external collector received data.

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
