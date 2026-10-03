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
// Dates, durations, short IDs and workspace names, written one way everywhere.

const minute = 60_000;
const hour = 60 * minute;
const day = 24 * hour;

/** A Date from an ISO string, a Date or epoch milliseconds; null when invalid. */
export function toDate(value: unknown): Date | null {
  if (value instanceof Date)
    return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value !== "string" && typeof value !== "number") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

const plural = (count: number, unit: string) =>
  `${count} ${unit}${count === 1 ? "" : "s"}`;

/**
 * How long ago or how soon: "just now", "2 min ago", "3 hours ago",
 * "in 1 day". An unreadable value gives "".
 */
export function relativeTime(value: unknown, now: Date = new Date()): string {
  const date = toDate(value);
  if (!date) return "";
  const delta = date.getTime() - now.getTime();
  const size = Math.abs(delta);
  if (size < 45_000) return "just now";
  const text =
    size < hour
      ? `${Math.max(1, Math.round(size / minute))} min`
      : size < day
        ? plural(Math.round(size / hour), "hour")
        : plural(Math.round(size / day), "day");
  return delta < 0 ? `${text} ago` : `in ${text}`;
}

/**
 * "Sat 3 Oct, 17:00": the weekday, day, month and 24-hour time in the
 * viewer's time zone (or `timeZone`).
 */
export function absoluteTime(value: unknown, timeZone?: string): string {
  const date = toDate(value);
  if (!date) return "";
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}

/** "Sat 3 Oct, 17:00 (in 1 day)"; "" when the value isn't a date. */
export function dateWithRelative(
  value: unknown,
  now: Date = new Date(),
  timeZone?: string,
): string {
  const absolute = absoluteTime(value, timeZone);
  return absolute ? `${absolute} (${relativeTime(value, now)})` : "";
}

/** The ISO form for `<time datetime>`; "" when the value isn't a date. */
export function isoTime(value: unknown): string {
  return toDate(value)?.toISOString() ?? "";
}

/** "45 s", "2 min 5 s", "1 h 4 min", "3 days"; "" for a negative span. */
export function durationText(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "";
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return seconds % 60 ? `${minutes} min ${seconds % 60} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48)
    return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return plural(Math.floor(hours / 24), "day");
}

/**
 * An opaque ID (a UUID, or any 20+ characters without spaces) shortened to
 * its first 8 characters: "700587c4". A readable ID such as "development"
 * stays whole, so it never reads as a broken word ("developm").
 */
export function shortId(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "";
  const id = String(value);
  const opaque = /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) || /^\S{20,}$/.test(id);
  return opaque ? id.slice(0, 8) : id;
}

/** Names of the selected workspace's levels. */
export interface WorkspaceNames {
  tenant?: string;
  project: string;
  environment: string;
}

/** "Payments / Production". */
export function workspaceShort(names: WorkspaceNames): string {
  return [names.project, names.environment].filter(Boolean).join(" / ");
}

/** "Acme / Payments / Production". */
export function workspaceLong(names: WorkspaceNames): string {
  return [names.tenant, names.project, names.environment]
    .filter(Boolean)
    .join(" / ");
}

/** While names are unknown: "Workspace 1a2b3c4d" from the environment ID. */
export function workspaceFallback(environmentId: string): string {
  return `Workspace ${shortId(environmentId)}`;
}

/** Shown while the workspace names are still loading. */
export const loadingWorkspace = "Loading workspace…";
