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
// What the run's Logs section and the Logs page show and ask for: an entry's
// words, the queries filters make, the phone's filter summary, how Follow
// adds new entries, and what a failed read says. Pure: the store and the
// pages call these, tests check them.
import { describeError, type PlainError } from "../../errors";
import type { IconName } from "../../icon-data";
import type { LogsLevel, LogsRange, LogsRoute } from "../operate-routes";
import { defaultLogsRoute } from "../operate-routes";
import type {
  ExecutionLogQuery,
  LogLevel,
  LogSource,
  RunLogEntry,
  RunLogQuery,
} from "../run-contracts";
import type { LogRow } from "./logs-api";

export const LEVEL_WORDS: Readonly<Record<LogLevel, string>> = {
  debug: "Debug",
  info: "Info",
  warning: "Warning",
  error: "Error",
};
/** The tone a level's word is drawn in. */
export const LEVEL_TONES: Readonly<Record<LogLevel, string>> = {
  debug: "neutral",
  info: "info",
  warning: "warning",
  error: "danger",
};
export const SOURCE_WORDS: Readonly<Record<LogSource, string>> = {
  engine: "Engine",
  worker: "Worker",
  connector: "Connector",
  ai: "AI",
};
/** The Level menu, lowest first. */
export const levelChoices: readonly { value: LogsLevel; label: string }[] = [
  { value: "", label: "Everything" },
  { value: "info", label: "Info or higher" },
  { value: "warning", label: "Warning or higher" },
  { value: "error", label: "Error only" },
];
/** The Time menu, and how a sentence names each range. */
export const rangeChoices: readonly {
  value: LogsRange;
  label: string;
  words: string;
  milliseconds: number;
}[] = [
  {
    value: "15m",
    label: "Last 15 minutes",
    words: "last 15 minutes",
    milliseconds: 15 * 60_000,
  },
  {
    value: "1h",
    label: "Last hour",
    words: "last hour",
    milliseconds: 3_600_000,
  },
  {
    value: "6h",
    label: "Last 6 hours",
    words: "last 6 hours",
    milliseconds: 6 * 3_600_000,
  },
  {
    value: "24h",
    label: "Last 24 hours",
    words: "last 24 hours",
    milliseconds: 24 * 3_600_000,
  },
  {
    value: "7d",
    label: "Last 7 days",
    words: "last 7 days",
    milliseconds: 7 * 86_400_000,
  },
];
const rangeOf = (range: LogsRange) =>
  rangeChoices.find((choice) => choice.value === range) ?? rangeChoices[1];
const levelLabel = (level: LogsLevel) =>
  levelChoices.find((choice) => choice.value === level)?.label ?? "";

/** How long the platform keeps worker and connector entries by default. */
const KEPT = "7 days";
/** A run's entries per read: the most a page holds. */
export const RUN_LOG_PAGE = 500;
/** Entries across runs per read. */
export const LOG_PAGE = 100;
/** How often Follow asks for new entries. */
export const FOLLOW_MS = 10_000;
/** The longest range a message search may cover. */
const SEARCH_RANGE_MS = 6 * 3_600_000;
const SEARCH_LIMIT = 200;

/** What one entry shows, in the order the columns read. */
export interface EntryLine {
  /** "10:41:58.611" in the viewer's time zone. */
  time: string;
  /** The full date and time, for the time's title. */
  title: string;
  datetime: string;
  level: string;
  tone: string;
  source: string;
  /** The step, its iteration when it has one, or "—" for the run itself. */
  step: string;
  message: string;
  flags: { icon: IconName; text: string }[];
  /** A worker's code and fields, "ORDER.CHECKED  items=3"; "" for none. */
  details: string;
}

function clock(date: Date, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).formatToParts(date);
  const part = (type: string) =>
    parts.find((item) => item.type === type)?.value ?? "00";
  const milliseconds = String(date.getUTCMilliseconds()).padStart(3, "0");
  return `${part("hour")}:${part("minute")}:${part("second")}.${milliseconds}`;
}
function fullDate(date: Date, timeZone?: string): string {
  const day = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
  return `${day}, ${clock(date, timeZone)}`;
}
const fieldText = (value: unknown) => (value === null ? "null" : String(value));

/** One entry's words; its text is shown as given, never as markup. */
export function entryLine(entry: RunLogEntry, timeZone?: string): EntryLine {
  const date = new Date(entry.at);
  const flags: EntryLine["flags"] = [];
  if (entry.redactions)
    flags.push({
      icon: "lock",
      text:
        entry.redactions === 1
          ? "1 value hidden"
          : `${entry.redactions.toLocaleString("en-US")} values hidden`,
    });
  if (entry.truncated) flags.push({ icon: "stale", text: "Shortened" });
  const details =
    entry.source === "engine"
      ? []
      : [
          ...(entry.code ? [entry.code] : []),
          ...Object.entries(entry.fields).map(
            ([key, value]) => `${key}=${fieldText(value)}`,
          ),
        ];
  return {
    time: clock(date, timeZone),
    title: fullDate(date, timeZone),
    datetime: date.toISOString(),
    level: LEVEL_WORDS[entry.level],
    tone: LEVEL_TONES[entry.level],
    source: SOURCE_WORDS[entry.source],
    step: entry.node_id ? entry.instance_key || entry.node_id : "—",
    message: entry.message,
    flags,
    details: details.join("  "),
  };
}

/** The filters of a run's Logs section: every level and step by default. */
export interface RunLogsFilters {
  level: LogsLevel;
  step: string;
}
export const defaultRunLogsFilters: Readonly<RunLogsFilters> = Object.freeze({
  level: "",
  step: "",
});
export function runLogQuery(
  filters: RunLogsFilters,
  cursor?: string,
): RunLogQuery {
  return {
    ...(filters.level ? { level: filters.level } : {}),
    ...(filters.step ? { node_id: filters.step } : {}),
    limit: RUN_LOG_PAGE,
    ...(cursor ? { cursor } : {}),
  };
}
const entries = (count: number) =>
  count === 1 ? "1 entry" : `${count.toLocaleString("en-US")} entries`;
/** "8 entries · engine entries come from the run's history; …". */
export function runCountText(count: number, more: boolean): string {
  const shown = more ? `The first ${entries(count)}` : entries(count);
  return `${shown} · engine entries come from the run's history; worker and connector entries are kept for ${KEPT}.`;
}

/** The message filter as the platform searches it: trimmed. */
export const messageQuery = (contains: string) => contains.trim();
/** Why the message filter can't be sent as it is; "" when it can. */
export function searchProblem(range: LogsRange, contains: string): string {
  const text = messageQuery(contains);
  if (!text) return "";
  if (text.length > SEARCH_LIMIT)
    return `Keep the message to ${SEARCH_LIMIT} characters or fewer.`;
  return rangeOf(range).milliseconds > SEARCH_RANGE_MS
    ? "Narrow the time range to 6 hours or less to search messages."
    : "";
}
/**
 * The query of a first page: the range ends now, and each filter the person
 * set is sent. The page's later pages repeat it exactly with their cursor.
 */
export function executionLogQuery(
  route: LogsRoute,
  contains: string,
  now: Date,
): ExecutionLogQuery {
  const to = now.getTime();
  const text = messageQuery(contains);
  return {
    from: new Date(to - rangeOf(route.range).milliseconds).toISOString(),
    to: new Date(to).toISOString(),
    ...(route.workflow ? { workflow: route.workflow } : {}),
    ...(route.level ? { level: route.level } : {}),
    ...(route.code ? { code: route.code } : {}),
    ...(route.step ? { node_id: route.step } : {}),
    ...(text ? { contains: text } : {}),
    ...(route.test ? { include_test: true } : {}),
    limit: LOG_PAGE,
  };
}
/** "6 entries in the last hour · entries are kept for 7 days". */
export function countText(
  count: number,
  range: LogsRange,
  more: boolean,
): string {
  const shown = more ? `The newest ${entries(count)}` : entries(count);
  return `${shown} in the ${rangeOf(range).words} · entries are kept for ${KEPT}`;
}

/**
 * The phone's summary of the folded filters: every one that isn't at its
 * default, the message included (on screen only), and whether Follow is on.
 */
export function filterSummary(
  route: LogsRoute,
  contains: string,
  follow: boolean,
): string {
  const parts: string[] = [];
  if (route.range !== defaultLogsRoute.range)
    parts.push(rangeOf(route.range).words);
  if (route.level !== defaultLogsRoute.level)
    parts.push(levelLabel(route.level));
  if (route.workflow) parts.push(`workflow ${route.workflow}`);
  if (route.step) parts.push(`step ${route.step}`);
  if (route.code) parts.push(`code ${route.code}`);
  const text = messageQuery(contains);
  if (text) parts.push(`message contains “${text}”`);
  if (route.test) parts.push("test runs included");
  const filters = parts.length
    ? parts
    : ["the defaults (last hour, Warning or higher, test runs off)"];
  if (follow) filters.push("following every 10 seconds");
  return filters.join(" · ");
}

/**
 * Follow's merge of a fresh first page into what is shown: new entries go on
 * top and the rest stay as they are. When the fresh page shares no entry
 * with what is shown and more follow it, a gap would open between them, so
 * the list starts again from the fresh page.
 */
export function mergeFollowed(
  shown: readonly LogRow[],
  first: readonly LogRow[],
  firstHasMore: boolean,
): { rows: LogRow[]; replaced: boolean } {
  const fresh = new Set(first.map((row) => row.id));
  const overlap = shown.some((row) => fresh.has(row.id));
  if (!overlap && (firstHasMore || !shown.length))
    return { rows: [...first], replaced: true };
  return {
    rows: [...first, ...shown.filter((row) => !fresh.has(row.id))],
    replaced: false,
  };
}

/** What a failed log read shows. */
export type LogFailure =
  | { kind: "unserved" }
  | { kind: "refused" }
  | { kind: "withheld" }
  | { kind: "slow"; error: PlainError }
  | { kind: "error"; error: PlainError };
/**
 * An older platform (501) asks no more; a refusal shows the access state; a
 * withheld run answers its logs as it answers the run; a read the platform
 * stopped (503) says so in the platform's words, with Try again.
 */
export function logFailure(error: unknown): LogFailure {
  const plain = describeError(error);
  if (plain.status === 501) return { kind: "unserved" };
  if (plain.status === 403) return { kind: "refused" };
  if (
    plain.code === "WV-LEGACY-UNAVAILABLE" ||
    plain.code === "WV-IR-UNSUPPORTED"
  )
    return { kind: "withheld" };
  if (plain.status === 503 && plain.code === "WV-UNAVAILABLE")
    return { kind: "slow", error: plain };
  return { kind: "error", error: plain };
}
