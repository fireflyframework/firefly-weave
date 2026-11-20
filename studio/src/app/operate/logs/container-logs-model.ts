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
// What the Container logs tab says: whether a target's runner can fetch its
// logs, what a fetch asks for, and a snapshot's notes, file names and plain
// text. Pure: the tab renders it, tests check it.
import type {
  LogComponent,
  LogFailureCode,
  LogRunner,
  LogTarget,
  MemberLog,
} from "./container-logs-api";

/** How far back a fetch reads, in menu order; the last 15 minutes is the default. */
export const sinceChoices: readonly { value: number; label: string }[] = [
  { value: 300, label: "Last 5 minutes" },
  { value: 900, label: "Last 15 minutes" },
  { value: 3600, label: "Last hour" },
  { value: 21_600, label: "Last 6 hours" },
  { value: 86_400, label: "Last 24 hours" },
];
export const DEFAULT_SINCE = 900;
export const DEFAULT_TAIL = 200;
export const MAX_MEMBERS = 5;
/** Follow asks for a new snapshot this often while the page is visible. */
export const CONTAINER_FOLLOW_MS = 10_000;
/** A pending snapshot is asked about this often until the runner answers. */
export const WAIT_MS = 1_000;

export type RunnerStatus =
  | { kind: "online" }
  | { kind: "offline"; text: string }
  | { kind: "unsupported"; text: string };

/**
 * Whether the target's runner can fetch logs now: the target and a runner in
 * touch (within 90 s) must both read logs.
 */
export function runnerStatus(
  target: LogTarget,
  runners: readonly LogRunner[],
  now = Date.now(),
): RunnerStatus {
  const unsupported: RunnerStatus = {
    kind: "unsupported",
    text: "This target's runner can't fetch logs.",
  };
  if (!target.capabilities.includes("read_logs")) return unsupported;
  const registered = runners.filter(
    (item) => item.targetId === target.id && !item.revoked,
  );
  const live = registered.filter((item) => Date.parse(item.expiresAt) > now);
  if (live.some((item) => item.capabilities.includes("read_logs")))
    return { kind: "online" };
  if (live.length) return unsupported;
  const seen = Math.max(
    0,
    ...registered.map((item) => Date.parse(item.lastSeen)),
  );
  if (!seen)
    return {
      kind: "offline",
      text: `No runner has registered for ${target.name}, so Weave can't fetch its logs.`,
    };
  return {
    kind: "offline",
    text: `The runner for ${target.name} hasn't been in touch for ${away(now - seen)}, so Weave can't fetch its logs.`,
  };
}
function away(milliseconds: number): string {
  const minutes = Math.max(1, Math.floor(milliseconds / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.floor(hours / 24)} days`;
}

/** The members a fetch names at first: the first five observed. */
export const defaultMembers = (component: LogComponent | null): string[] =>
  (component?.members ?? []).slice(0, MAX_MEMBERS);

/** "" for a number of lines the platform takes; otherwise what to enter. */
export function tailProblem(text: string): string {
  const value = Number(text.trim());
  return /^\d+$/.test(text.trim()) && value >= 1 && value <= 1000
    ? ""
    : "Enter a number of lines from 1 to 1,000.";
}

export const fetchingNote = (target: string) =>
  `Fetching asks the runner of ${target} for a snapshot. It changes nothing on the target.`;
export const waitingNote = (target: string) =>
  `Asking the runner of ${target}. A snapshot usually arrives within 10 seconds.`;
export const EXPIRED_NOTE =
  "This snapshot expired after 1 hour. Fetch logs again to see current lines.";
const KEPT =
  "Snapshots expire after 1 hour. Secrets are redacted before they leave the runner.";

function clock(at: string, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).formatToParts(new Date(at));
  const part = (type: string) =>
    parts.find((item) => item.type === type)?.value ?? "00";
  return `${part("hour")}:${part("minute")}:${part("second")}`;
}
/** A line's time, "10:41:52", in the viewer's time zone. */
export const lineTime = (at: string, timeZone?: string) => clock(at, timeZone);
export const snapshotNote = (readAt: string, timeZone?: string) =>
  `Snapshot from ${clock(readAt, timeZone)} · ${KEPT}`;
export const lineCount = (count: number) =>
  count === 1 ? "1 line" : `${count.toLocaleString("en-US")} lines`;

const failures: Record<
  LogFailureCode,
  (target: string, component: string) => string
> = {
  no_answer: (target) =>
    `The runner of ${target} didn't answer within 2 minutes. Try again, or check its runner in Operate › Clusters.`,
  unknown_component: (target, component) =>
    `The runner of ${target} doesn't read the logs of ${component}.`,
  no_members: (_, component) =>
    `None of the members you chose runs in ${component} anymore. Choose others, or fetch all of them.`,
  unreadable: (target) =>
    `The runner of ${target} couldn't read these logs. Try again in a moment.`,
};
export const failureNote = (
  failure: LogFailureCode,
  target: string,
  component: string,
) => failures[failure](target, component);

/** One member's lines as plain text: time, stream and text, a line each. */
export const memberText = (member: MemberLog) =>
  member.lines
    .map(
      (line) =>
        `${new Date(line.at).toISOString()} ${line.stream} ${line.text}\n`,
    )
    .join("");

const fileSafe = (text: string) => text.replace(/[^A-Za-z0-9._-]/g, "-");
/** "<target>-<component>-<member>-<time>.log", in characters every file system takes. */
export function downloadName(
  target: string,
  component: string,
  member: string,
  readAt: string,
): string {
  const time = new Date(readAt)
    .toISOString()
    .replace(/\.\d+Z$/, "Z")
    .replace(/[-:]/g, "");
  return [target, component, member].map(fileSafe).join("-") + `-${time}.log`;
}
