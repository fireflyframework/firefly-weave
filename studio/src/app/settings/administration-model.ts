/*
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
*/
// What Settings › Administration says about the platform: its uptime,
// build and compatibility, a retention plan's counts and what applying it
// deletes, where telemetry goes (by host only), how long each operations
// record is kept, and whether a domain can be allowed. Pure.
import type {
  Compatibility,
  Finding,
  PlatformFacts,
  RetentionPlan,
  Telemetry,
} from "./administration-api";

const plural = (count: number, one: string, many = `${one}s`) =>
  `${count.toLocaleString("en-US")} ${count === 1 ? one : many}`;

/** "3 days 4 h", "5 h 12 min", "12 min": counted on from when it was observed. */
export function uptimeText(platform: PlatformFacts, now: number): string {
  const seconds = Math.max(
    0,
    Math.floor(platform.uptimeSeconds + (now - platform.observedAt) / 1000),
  );
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days >= 1)
    return hours % 24
      ? `${plural(days, "day")} ${hours % 24} h`
      : plural(days, "day");
  if (hours >= 1)
    return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  if (minutes >= 1) return `${minutes} min`;
  return `${seconds} s`;
}
/** The build digest's first 8 characters: "26ab04e1". */
export function buildText(build: string | null): string {
  if (!build) return "Not recorded";
  return build.replace(/^sha256:/, "").slice(0, 8);
}

export function compatibilityText(report: Compatibility): {
  text: string;
  tone: "success" | "warning";
} {
  const findings = plural(report.findings.length, "finding");
  return report.mode === "ready" && !report.findings.length
    ? { text: `Ready · ${findings}`, tone: "success" }
    : {
        text: `${report.mode === "ready" ? "Ready" : "Restricted"} · ${findings}`,
        tone: "warning",
      };
}
const kinds: Record<string, string> = {
  run: "A run",
  activation: "An activation",
  provider: "A provider",
  worker: "A worker",
  connection: "A connection",
  broker: "A broker",
  subscription: "A subscription",
  inventory: "The inventory",
};
const codes: Record<string, string> = {
  ir_unsupported: "uses a workflow format this version can't run",
  artifact_invalid: "has an artifact this version can't read",
  action_unavailable: "uses an action this platform doesn't have",
  connector_unsupported: "uses a connector this version doesn't support",
  worker_protocol_unsupported:
    "speaks a worker protocol this version doesn't support",
  provider_requirement_unsupported:
    "needs a provider feature this version doesn't support",
  inventory_incomplete: "couldn't be read completely",
  authority_missing: "lacks the access it needs",
  legacy_policy_blocked: "is held back by an older policy",
  operational_capacity_blocked: "is held back by operational limits",
  capacity_absent: "has no capacity to run",
  policy_mismatch: "doesn't match the platform's policy",
};
/** "A worker speaks a worker protocol this version doesn't support." */
export function findingText(finding: Finding): string {
  const who = kinds[finding.kind] ?? "A record";
  return Object.hasOwn(codes, finding.code)
    ? `${who} ${codes[finding.code]}.`
    : `${who}: ${finding.code}.`;
}

const clock = (value: string, timeZone?: string) =>
  new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(new Date(value));
/** A plan's counts, its expiry, and what Apply asks before it deletes. */
export function planWords(plan: RetentionPlan, timeZone?: string) {
  const deletable = plan.candidates.filter(
    (item) => item.reason === "expired_debug",
  ).length;
  const kept = plan.candidates.length - deletable;
  const sessions = plural(deletable, "expired debug session");
  return {
    deletable,
    deletableText: deletable ? sessions : "No expired debug sessions",
    keptText: kept
      ? `${plural(kept, "debug session")} something still uses`
      : "",
    expires: `Plan expires at ${clock(plan.expiresAt, timeZone)}`,
    confirm: `Delete ${sessions}? This can't be undone. Run histories and the other records stay.`,
    apply: `Delete ${deletable.toLocaleString("en-US")}`,
  };
}

/** "On · traces to otel.example · metrics to otel.example every 60 s · 0 dropped", or "Off". */
export function telemetryText(telemetry: Telemetry): string {
  if (!telemetry.enabled) return "Off";
  const where = (endpoint: { configured: boolean; host: string | null }) =>
    endpoint.host ?? "a configured endpoint";
  const parts = ["On"];
  if (telemetry.traces.configured)
    parts.push(`traces to ${where(telemetry.traces)}`);
  if (telemetry.metrics.configured)
    parts.push(
      `metrics to ${where(telemetry.metrics)}${telemetry.metrics.interval ? ` every ${telemetry.metrics.interval} s` : ""}`,
    );
  parts.push(`${telemetry.dropped.toLocaleString("en-US")} dropped`);
  return parts.join(" · ");
}

const records: Record<string, string> = {
  run_statistics_5m: "Run statistics, 5-minute buckets",
  run_statistics_1h: "Run statistics, hourly",
  run_statistics_1d: "Run statistics, daily",
  samples_1m: "Samples, per minute",
  samples_1h: "Samples, hourly",
  change_requests: "Change requests",
  alerts: "Alerts",
  alert_events: "Alert history",
  execution_logs: "Log entries",
  log_snapshots: "Container log snapshots",
};
/** "2 days", "24 hours", "90 minutes": how long a record is kept. */
function keptFor(seconds: number): string {
  if (seconds >= 2 * 86_400 && seconds % 86_400 === 0)
    return plural(seconds / 86_400, "day");
  if (seconds >= 2 * 3600 && seconds % 3600 === 0)
    return plural(seconds / 3600, "hour");
  if (seconds % 60 === 0) return plural(seconds / 60, "minute");
  return plural(seconds, "second");
}
/** Each record and how long it's kept, in the platform's order. */
export function periodRows(
  periods: { record: string; seconds: number }[],
): [string, string][] {
  return periods.map((period) => [
    records[period.record] ?? period.record,
    keptFor(period.seconds),
  ]);
}

const DOMAIN_LIMIT = 20;
const domain =
  /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
/** The domain to allow, lower-cased, or why it can't be. */
export function domainProblem(
  text: string,
  allowed: readonly string[],
): { domain: string } | { problem: string } {
  const value = text.trim().toLowerCase();
  if (!domain.test(value) || value.length > 253)
    return { problem: "Enter a domain only, such as example.org." };
  if (allowed.includes(value))
    return { problem: `${value} is already allowed.` };
  if (allowed.length >= DOMAIN_LIMIT)
    return { problem: `Allow up to ${DOMAIN_LIMIT} domains.` };
  return { domain: value };
}
