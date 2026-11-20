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
// The platform reads and actions behind Settings › Administration: the
// summary's API section, compatibility and its check, telemetry export,
// retention plans and their application, and how long operations records are
// kept. Every answer goes through its typed reader; one that doesn't read is
// a failed read, never half shown.
import {
  unreadableAnswer,
  type AlertsTransport,
} from "../operate/alerts/alerts-api";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0;
const isInstant = (value: unknown): value is string =>
  isText(value) && Number.isFinite(Date.parse(value));
const isCount = (value: unknown): value is number =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const isSeconds = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;
// A bare host name or address with an optional port: never a path, a user or a scheme.
const bareHost = /^[A-Za-z0-9.-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$/;

export interface PlatformFacts {
  version: string;
  /** "sha256:…" or null when the build wasn't recorded. */
  build: string | null;
  uptimeSeconds: number;
  /** When the platform measured the uptime (epoch milliseconds). */
  observedAt: number;
}
/** The summary's API section; null when the summary has none. */
export function readPlatform(summary: unknown): PlatformFacts | null {
  if (!isRecord(summary) || !isRecord(summary["api"])) return null;
  const api = summary["api"];
  const build = api["build"];
  if (
    !isText(api["version"]) ||
    !(build === null || build === undefined || isText(build)) ||
    !isSeconds(api["uptime_seconds"]) ||
    !isInstant(summary["observed_at"])
  )
    return null;
  return {
    version: api["version"],
    build: (build as string | undefined) ?? null,
    uptimeSeconds: api["uptime_seconds"],
    observedAt: Date.parse(summary["observed_at"]),
  };
}

export interface Finding {
  kind: string;
  code: string;
}
export interface Compatibility {
  mode: "ready" | "restricted";
  complete: boolean;
  inspected: number;
  checkedAt: string | null;
  findings: Finding[];
  truncated: boolean;
}
export function readCompatibility(value: unknown): Compatibility | null {
  if (!isRecord(value)) return null;
  const findings = value["findings"];
  const checked = value["checked_at"];
  if (
    !(value["mode"] === "ready" || value["mode"] === "restricted") ||
    typeof value["complete"] !== "boolean" ||
    !isCount(value["inspected"]) ||
    !(checked === null || checked === undefined || isInstant(checked)) ||
    !Array.isArray(findings) ||
    !findings.every(
      (item) => isRecord(item) && isText(item["kind"]) && isText(item["code"]),
    )
  )
    return null;
  return {
    mode: value["mode"],
    complete: value["complete"],
    inspected: value["inspected"],
    checkedAt: (checked as string | undefined) ?? null,
    findings: (findings as Json[]).map((item) => ({
      kind: item["kind"] as string,
      code: item["code"] as string,
    })),
    truncated: value["findings_truncated"] === true,
  };
}

export interface Endpoint {
  configured: boolean;
  /** The host only; null when none or when it isn't a bare host. */
  host: string | null;
}
export interface Telemetry {
  enabled: boolean;
  traces: Endpoint;
  metrics: Endpoint & { interval: number | null };
  dropped: number;
}
function readEndpoint(value: unknown): Endpoint | null {
  if (!isRecord(value) || typeof value["configured"] !== "boolean") return null;
  const host = value["host"];
  if (!(host === null || host === undefined || typeof host === "string"))
    return null;
  return {
    configured: value["configured"],
    host: typeof host === "string" && bareHost.test(host) ? host : null,
  };
}
export function readTelemetry(value: unknown): Telemetry | null {
  if (!isRecord(value) || typeof value["enabled"] !== "boolean") return null;
  const traces = readEndpoint(value["traces"]);
  const metrics = readEndpoint(value["metrics"]);
  const interval = isRecord(value["metrics"])
    ? value["metrics"]["interval_seconds"]
    : undefined;
  if (
    !traces ||
    !metrics ||
    !(interval === null || interval === undefined || isSeconds(interval)) ||
    !isCount(value["dropped"])
  )
    return null;
  return {
    enabled: value["enabled"],
    traces,
    metrics: { ...metrics, interval: (interval as number | undefined) ?? null },
    dropped: value["dropped"],
  };
}

export interface RetentionPlan {
  id: string;
  expiresAt: string;
  complete: boolean;
  candidates: { id: string; reason: "expired_debug" | "referenced" }[];
}
export function readPlan(value: unknown): RetentionPlan | null {
  if (!isRecord(value)) return null;
  const candidates = value["candidates"];
  if (
    !isText(value["id"]) ||
    !isInstant(value["expires_at"]) ||
    typeof value["complete"] !== "boolean" ||
    !Array.isArray(candidates) ||
    !candidates.every(
      (item) =>
        isRecord(item) &&
        isText(item["id"]) &&
        (item["reason"] === "expired_debug" || item["reason"] === "referenced"),
    )
  )
    return null;
  return {
    id: value["id"],
    expiresAt: value["expires_at"],
    complete: value["complete"],
    candidates: (candidates as Json[]).map((item) => ({
      id: item["id"] as string,
      reason: item["reason"] as "expired_debug" | "referenced",
    })),
  };
}
export interface RetentionResult {
  deleted: number;
  blocked: number;
}
export function readApplication(value: unknown): RetentionResult | null {
  if (
    !isRecord(value) ||
    !Array.isArray(value["deleted"]) ||
    !Array.isArray(value["blocked"])
  )
    return null;
  return {
    deleted: value["deleted"].length,
    blocked: value["blocked"].length,
  };
}
export interface Period {
  record: string;
  seconds: number;
}
export function readPeriods(value: unknown): Period[] {
  if (!isRecord(value) || !Array.isArray(value["items"]))
    throw unreadableAnswer();
  return value["items"]
    .filter(
      (item): item is Json =>
        isRecord(item) && isText(item["record"]) && isCount(item["seconds"]),
    )
    .map((item) => ({
      record: item["record"] as string,
      seconds: item["seconds"] as number,
    }));
}

const answer =
  <T>(read: (value: unknown) => T | null) =>
  (value: unknown) => {
    const result = read(value);
    if (result === null) throw unreadableAnswer();
    return result;
  };

export async function readSummaryPlatform(
  api: AlertsTransport,
): Promise<PlatformFacts> {
  return answer(readPlatform)(
    await api.request(`${api.environment}/operations/summary`),
  );
}
export async function readCompatibilityReport(
  api: AlertsTransport,
): Promise<Compatibility> {
  return answer(readCompatibility)(
    await api.request(`${api.project}/operations/compatibility`),
  );
}
export async function runCompatibilityCheck(
  api: AlertsTransport,
): Promise<Compatibility> {
  return answer(readCompatibility)(
    await api.request(`${api.project}/operations/compatibility/check`, "POST"),
  );
}
export async function readTelemetrySettings(
  api: AlertsTransport,
): Promise<Telemetry> {
  return answer(readTelemetry)(
    await api.request(`${api.project}/operations/telemetry`),
  );
}
/** A plan of the expired debug sessions that can be deleted; it deletes nothing. */
export async function makeRetentionPlan(
  api: AlertsTransport,
): Promise<RetentionPlan> {
  return answer(readPlan)(
    await api.request(`${api.project}/operations/retention/plans`, "POST", {
      target: "expired_debug",
      limit: 100,
    }),
  );
}
export async function applyRetentionPlan(
  api: AlertsTransport,
  planId: string,
): Promise<RetentionResult> {
  return answer(readApplication)(
    await api.request(
      `${api.project}/operations/retention/plans/${encodeURIComponent(planId)}/apply`,
      "POST",
    ),
  );
}
export async function readRetentionPeriods(
  api: AlertsTransport,
): Promise<Period[]> {
  return readPeriods(
    await api.request(`${api.environment}/operations/retention-periods`),
  );
}
