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
// Overview: the environment summary (operations_summary.read) and what the
// page says about it. Mirrors src/firefly_weave/contracts/operations_summary.py;
// wire fields are snake_case. A section the person may not read is
// "forbidden" and one the platform could not compute in time "unavailable":
// neither is ever shown as zero. Pure: the page renders it, tests check it.
import type { StudioApi } from "../../api";
import { durationText, relativeTime, shortId } from "../../format";
import { statusLabel } from "../../status-labels";
import {
  clustersRecordPath,
  clustersTabPath,
  viewPath,
} from "../operate-routes";

export const healthStates = ["ok", "degraded", "down", "disabled"] as const;
export type HealthState = (typeof healthStates)[number];
export const healthComponents = [
  "database",
  "recovery_loop",
  "schedules",
  "outbox_loop",
  "provider_loop",
  "kafka_loop",
  "operations_loop",
  "alert_evaluator",
  "native_executors",
  "telemetry",
] as const;
export type HealthComponentName = (typeof healthComponents)[number];
export const attentionKinds = [
  "job",
  "approval",
  "worker",
  "incident",
  "runner",
  "compatibility",
  "run",
] as const;
export type AttentionKind = (typeof attentionKinds)[number];

export interface ForbiddenSection {
  state: "forbidden";
}
export interface UnavailableSection {
  state: "unavailable";
}
export interface ApiSection {
  version: string;
  /** The server image's digest, when the server knows it. */
  build: string | null;
  /** The process that answered; new each time it starts. */
  instance: string;
  uptime_seconds: number;
  compatibility: { mode: "ready" | "restricted"; findings: number };
}
/** Times and counts are present only when the component reports them. */
export interface HealthComponent {
  component: HealthComponentName;
  state: HealthState;
  last_started_at?: string;
  last_ok_at?: string;
  consecutive_failures?: number;
  recent_failures?: number;
  complete_until?: string;
  latency_ms?: number;
}
export interface LoadSection {
  runs_active: number;
  tasks_ready: number;
  tasks_leased: number;
  oldest_ready_seconds: number | null;
  waits_active: number;
  waits_overdue: number;
  oldest_overdue_seconds: number | null;
  outbox_pending: number;
  outbox_oldest_seconds: number | null;
  provider_pending: number;
  sources_enabled: number;
}
export interface WorkersSection {
  online: number;
  offline: number;
  draining: number;
  capacity: number;
  active_leases: number;
}
export interface TargetsSection {
  total: number;
  runner_online: number;
  busy: number;
  reconciliation_required: number;
  awaiting_approval: number;
}
export interface IncidentsSection {
  active: number;
  opened_24h: number;
}
export interface AttentionItem {
  kind: AttentionKind;
  /** A record ID, or "compatibility" for the platform's verdict. */
  id: string;
  title: string;
  /** Approvals only: when the plan can no longer be approved. */
  expires_at?: string;
}
export interface OperationsSummary {
  observed_at: string;
  api: ApiSection;
  health: HealthComponent[];
  load: LoadSection | UnavailableSection;
  workers: WorkersSection | UnavailableSection;
  targets: TargetsSection | ForbiddenSection | UnavailableSection;
  incidents: IncidentsSection | ForbiddenSection | UnavailableSection;
  attention: AttentionItem[] | UnavailableSection;
  /** Servers with run statistics only; test runs left out, failed counts timed-out runs. */
  runs_24h?: RunsDaySection | UnavailableSection;
}
export interface RunsDaySection {
  started: number;
  succeeded: number;
  failed: number;
  /** A fraction from 0 to 1; null when no run ended. */
  failure_rate: number | null;
}

/** The environment summary for the environment Studio is in. */
export function readSummary(api: Pick<StudioApi, "request" | "environment">) {
  return api.request<OperationsSummary>(
    `${api.environment}/operations/summary`,
  );
}

/** A section as the page shows it: its figures, or why there are none. */
export type Section<T> =
  | { kind: "ready"; value: T }
  | { kind: "forbidden" }
  | { kind: "unavailable" };
export function sectionOf<T extends object>(
  value: T | ForbiddenSection | UnavailableSection,
): Section<T> {
  if (Array.isArray(value)) return { kind: "ready", value };
  if ("state" in value) {
    const state = (value as { state: unknown }).state;
    if (state === "forbidden") return { kind: "forbidden" };
    if (state === "unavailable") return { kind: "unavailable" };
  }
  return { kind: "ready", value: value as T };
}

/**
 * How far the server's clock is from this computer's, in milliseconds to add
 * to local time: ages and countdowns tick locally on the server's clock.
 */
export function clockOffset(observedAt: string, receivedAt: number): number {
  const observed = Date.parse(observedAt);
  return Number.isFinite(observed) ? observed - receivedAt : 0;
}

/** How long the answering process has run by `now` (the server's clock). */
export function uptimeSeconds(summary: OperationsSummary, now: Date): number {
  const since = (now.getTime() - Date.parse(summary.observed_at)) / 1000;
  return Math.max(
    0,
    Math.floor(summary.api.uptime_seconds + Math.max(0, since)),
  );
}

const componentLabels: Record<HealthComponentName, string> = {
  database: "Database",
  recovery_loop: "Recovery",
  schedules: "Schedules",
  outbox_loop: "Outbox",
  provider_loop: "Providers",
  kafka_loop: "Kafka",
  operations_loop: "Operations",
  alert_evaluator: "Alerts",
  native_executors: "Native executors",
  telemetry: "Telemetry export",
};
/** The loops the Scheduler tile stands for. */
const schedulerLoops: HealthComponentName[] = [
  "recovery_loop",
  "schedules",
  "provider_loop",
  "kafka_loop",
  "operations_loop",
  "alert_evaluator",
];
const severity: Record<HealthState, number> = {
  disabled: 0,
  ok: 1,
  degraded: 2,
  down: 3,
};

export type TileId =
  | "api"
  | "database"
  | "scheduler"
  | "outbox"
  | "compatibility"
  | "telemetry";
/** One loop in a tile's Details: its last success and failures in a row. */
export interface LoopRow {
  label: string;
  state: HealthState;
  /** "2 min ago", "Never" before a first success, "" when switched off. */
  lastSuccess: string;
  lastSuccessAt: string | null;
  failures: number;
}
export interface HealthTile {
  id: TileId;
  label: string;
  state: HealthState;
  /** The tile's one figure. */
  figure: string;
  /** Details: facts, as label and value. */
  facts: [string, string][];
  /** Details: the loops behind the tile. */
  rows: LoopRow[];
}

function loopRow(component: HealthComponent, now: Date): LoopRow {
  const at = component.last_ok_at ?? null;
  return {
    label: componentLabels[component.component],
    state: component.state,
    lastSuccess:
      component.state === "disabled" && !at
        ? ""
        : at
          ? relativeTime(at, now) || "Unknown"
          : "Never",
    lastSuccessAt: at,
    failures: component.consecutive_failures ?? 0,
  };
}

/** "3 of 3 loops running", "1 of 3 loops down", or off on an API-only server. */
function loopsTile(
  components: HealthComponent[],
): Pick<HealthTile, "state" | "figure"> {
  const enabled = components.filter((item) => item.state !== "disabled");
  if (!enabled.length)
    return { state: "disabled", figure: "Off on this server" };
  const state = enabled.reduce<HealthState>(
    (worst, item) =>
      severity[item.state] > severity[worst] ? item.state : worst,
    "ok",
  );
  const count = enabled.filter((item) => item.state === state).length;
  const word =
    state === "ok" ? "running" : statusLabel("health", state).toLowerCase();
  return {
    state,
    figure: `${count} of ${enabled.length} ${enabled.length === 1 ? "loop" : "loops"} ${word}`,
  };
}

/**
 * The health strip: API, Database, Scheduler, Outbox, Compatibility and
 * Telemetry export. `now` is the server's clock.
 */
export function healthTiles(
  summary: OperationsSummary,
  now: Date,
): HealthTile[] {
  const byName = new Map(summary.health.map((item) => [item.component, item]));
  const component = (name: HealthComponentName): HealthComponent =>
    byName.get(name) ?? { component: name, state: "down" };
  const native = component("native_executors");
  const database = component("database");
  const outbox = component("outbox_loop");
  const telemetry = component("telemetry");
  const loops = schedulerLoops.map(component);
  const { mode, findings } = summary.api.compatibility;
  const uptime = durationText(uptimeSeconds(summary, now) * 1000);
  const outboxLast = outbox.last_ok_at
    ? relativeTime(outbox.last_ok_at, now)
    : "";
  return [
    {
      id: "api",
      label: "API",
      // The API answered; executors it cannot reach make it degraded.
      state:
        native.state === "degraded" || native.state === "down"
          ? "degraded"
          : "ok",
      figure: `Up ${uptime}`,
      facts: [
        ["Version", summary.api.version],
        [
          "Build",
          summary.api.build
            ? shortId(summary.api.build.replace(/^sha256:/, ""))
            : "Not reported",
        ],
        ["Instance", shortId(summary.api.instance)],
        ["Native executors", statusLabel("health", native.state)],
      ],
      rows: [],
    },
    {
      id: "database",
      label: "Database",
      state: database.state,
      figure:
        database.latency_ms === undefined
          ? "Probe timed out"
          : `Latency ${Math.round(database.latency_ms)} ms`,
      facts:
        database.latency_ms === undefined
          ? [["Latency", "Probe timed out"]]
          : [["Latency", `${database.latency_ms} ms`]],
      rows: [],
    },
    {
      id: "scheduler",
      label: "Scheduler",
      ...loopsTile(loops),
      facts: [],
      rows: loops.map((loop) => loopRow(loop, now)),
    },
    {
      id: "outbox",
      label: "Outbox",
      state: outbox.state,
      figure:
        outbox.state === "disabled"
          ? "Off on this server"
          : outboxLast
            ? `Last success ${outboxLast}`
            : "No success yet",
      facts: [],
      rows: [loopRow(outbox, now)],
    },
    {
      id: "compatibility",
      label: "Compatibility",
      state: mode === "ready" ? "ok" : "degraded",
      figure: findings
        ? `${countText(findings)} ${findings === 1 ? "finding" : "findings"}`
        : "No findings",
      facts: [
        ["Mode", mode === "ready" ? "Ready" : "Restricted"],
        ["Findings in this project", countText(findings)],
      ],
      rows: [],
    },
    {
      id: "telemetry",
      label: "Telemetry export",
      state: telemetry.state,
      figure:
        telemetry.state === "disabled"
          ? "Export is off"
          : telemetry.state === "ok"
            ? "Exporting"
            : "Dropping telemetry",
      facts: [["State", statusLabel("health", telemetry.state)]],
      rows: [],
    },
  ];
}

/** A count with digit grouping, for tabular figures: "1,204". */
export function countText(value: number): string {
  return value.toLocaleString("en-US");
}
const seconds = (value: number) => durationText(value * 1000);

export interface LoadFigure {
  id: keyof LoadSection;
  label: string;
  value: number;
  detail: string;
}
/** The Load band: five figures and what each one waits on. */
export function loadFigures(load: LoadSection): LoadFigure[] {
  const sources = load.sources_enabled;
  return [
    {
      id: "runs_active",
      label: "Active runs",
      value: load.runs_active,
      detail: "Test runs left out",
    },
    {
      id: "tasks_ready",
      label: "Ready tasks",
      value: load.tasks_ready,
      detail:
        load.oldest_ready_seconds === null
          ? "Nothing waiting"
          : `Oldest waits ${seconds(load.oldest_ready_seconds)}`,
    },
    {
      id: "waits_active",
      label: "Waits",
      value: load.waits_active,
      detail: load.waits_overdue
        ? `${countText(load.waits_overdue)} overdue` +
          (load.oldest_overdue_seconds === null
            ? ""
            : `, oldest by ${seconds(load.oldest_overdue_seconds)}`)
        : "None overdue",
    },
    {
      id: "outbox_pending",
      label: "Outbox backlog",
      value: load.outbox_pending,
      detail:
        load.outbox_oldest_seconds === null
          ? "Nothing due"
          : `Oldest waits ${seconds(load.outbox_oldest_seconds)}`,
    },
    {
      id: "provider_pending",
      label: "Provider backlog",
      value: load.provider_pending,
      detail: `${countText(sources)} ${sources === 1 ? "source" : "sources"} enabled`,
    },
  ];
}

/** Free task slots against ready tasks: the fill is ready ÷ free, at most full. */
export interface SlotMeter {
  free: number;
  ready: number | null;
  fill: number;
  over: boolean;
  text: string;
}
export function slotMeter(
  workers: WorkersSection,
  load: LoadSection | null,
): SlotMeter {
  const free = Math.max(0, workers.capacity - workers.active_leases);
  const slots = `${countText(free)} free ${free === 1 ? "slot" : "slots"}`;
  if (!load) return { free, ready: null, fill: 0, over: false, text: slots };
  const ready = load.tasks_ready;
  const over = ready > free;
  return {
    free,
    ready,
    fill: over ? 1 : free ? ready / free : 0,
    over,
    text: `${countText(ready)} ready, ${slots}`,
  };
}

/** Where an attention item's record opens; null when it has no record. */
export function attentionLink(item: AttentionItem): string | null {
  switch (item.kind) {
    case "job":
      return clustersRecordPath("jobs", item.id);
    case "approval":
      return clustersRecordPath("plans", item.id);
    case "worker":
      return viewPath("workers", item.id);
    case "incident":
      return `${viewPath("incidents")}?incident=${encodeURIComponent(item.id)}`;
    case "runner":
      return clustersTabPath("runners");
    case "run":
      return viewPath("runs", item.id);
    default:
      return null;
  }
}

/** The icon and the short word that lead an attention item. */
export const attentionLabels: Record<AttentionKind, [string, string]> = {
  job: ["Job", "warning"],
  approval: ["Approval", "approvals"],
  worker: ["Worker", "workers"],
  incident: ["Incident", "incidents"],
  runner: ["Runner", "clusters"],
  compatibility: ["Compatibility", "degraded"],
  run: ["Failed run", "failCircle"],
};

/** Nothing running, waiting or needing attention in what the person may read. */
export function isQuiet(summary: OperationsSummary): boolean {
  const load = sectionOf(summary.load);
  const attention = sectionOf(summary.attention);
  if (load.kind !== "ready" || attention.kind !== "ready") return false;
  const figures = load.value;
  return (
    attention.value.length === 0 &&
    figures.runs_active === 0 &&
    figures.tasks_ready === 0 &&
    figures.tasks_leased === 0 &&
    figures.waits_active === 0 &&
    figures.outbox_pending === 0 &&
    figures.provider_pending === 0
  );
}

// Run statistics on Overview: the hour's lines under the Load figures and the
// Last 24 h band. A server that keeps statistics says so with runs_24h in its
// summary; any other server sees no statistics request at all.

const second = (at: number) =>
  new Date(Math.floor(at / 1000) * 1000).toISOString().replace(".000Z", "Z");
function seriesQuery(
  now: number,
  spanMs: number,
  signals: string[],
  extra: Record<string, string> = {},
): string {
  const to = Math.floor(now / 1000) * 1000;
  const params = new URLSearchParams({
    from: second(to - spanMs),
    to: second(to),
  });
  for (const signal of signals) params.append("signals", signal);
  for (const [name, value] of Object.entries(extra)) params.set(name, value);
  return params.toString();
}

/** The last hour of the five Load figures; the server answers in five-minute buckets. */
export function loadSeriesQuery(now: number, offset = 0): string {
  return seriesQuery(now + offset, 3_600_000, [
    "runs_active",
    "tasks_ready",
    "waits_active",
    "outbox_pending",
    "provider_pending",
  ]);
}

/** The last 24 hours of outcomes, runs started and the failure rate, hour by hour. */
export function dayQuery(now: number, offset = 0): string {
  return seriesQuery(
    now + offset,
    24 * 3_600_000,
    [
      "runs_succeeded",
      "runs_failed",
      "runs_cancelled",
      "runs_started",
      "failure_rate",
    ],
    { granularity: "1h" },
  );
}

interface SeriesLine {
  signal: string;
  labels: Record<string, string>;
  points: [string, number | null][];
}
/** Each Load figure's line for the hour, keyed by the figure's ID (its signal). */
export function loadSparklines(
  answer: { series: SeriesLine[] } | null,
): Map<string, Array<number | null>> {
  const lines = new Map<string, Array<number | null>>();
  for (const item of answer?.series ?? [])
    if (!Object.keys(item.labels).length)
      lines.set(
        item.signal,
        item.points.map(([, value]) => value),
      );
  return lines;
}

/** Whether this server keeps run statistics: its summary has a runs_24h section. */
export function keepsStatistics(summary: OperationsSummary): boolean {
  return summary.runs_24h !== undefined && summary.runs_24h !== null;
}

const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});
/** "240 runs · 6 failed · 2.5% failure rate"; no rate when no run ended. */
export function dayHeadline(day: RunsDaySection): string {
  const runs = `${countText(day.started)} ${day.started === 1 ? "run" : "runs"}`;
  const parts = [runs, `${countText(day.failed)} failed`];
  if (day.failure_rate !== null)
    parts.push(`${percent.format(day.failure_rate)} failure rate`);
  return parts.join(" · ");
}

/** A statistics read this server doesn't serve: Overview shows what it showed before, with no error. */
export function statisticsMissing(error: {
  status?: number;
  code: string;
}): boolean {
  return error.status === 501 || error.status === 404;
}
