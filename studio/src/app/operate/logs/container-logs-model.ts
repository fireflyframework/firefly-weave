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
  LogRequestView,
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
/** The target's runners are read again this often while the tab shows: they are in touch for 90 s at a time. */
export const RUNNER_REFRESH_MS = 30_000;

/** A wait that `cancel` ends at once, so whatever awaits it goes on instead of hanging. */
export function delay(milliseconds: number): {
  done: Promise<void>;
  cancel(): void;
} {
  let end = () => {};
  let timer: ReturnType<typeof setTimeout> | undefined;
  const done = new Promise<void>((resolve) => {
    end = resolve;
    timer = setTimeout(resolve, milliseconds);
  });
  return {
    done,
    cancel() {
      clearTimeout(timer);
      end();
    },
  };
}

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
/** The snapshot note while Follow is on: when its newest snapshot was read. */
export const followingNote = (readAt: string, timeZone?: string) =>
  `Following: updated ${clock(readAt, timeZone)}. ${KEPT}`;
/** The fetch bar's hint while Follow waits for a runner out of touch. */
export const followWaitingNote = (target: string) =>
  `Follow is waiting for the runner of ${target}. It fetches again once the runner is back in touch.`;
/**
 * Why a snapshot's lines may be missing. With members chosen, each one the
 * runner couldn't read has its own box, so only the cuts remain to say.
 */
export const missingNote = (chosen: boolean) =>
  chosen
    ? "Some lines are missing: long lines were cut, or older lines were left out to keep the snapshot within 256 KiB."
    : "Some lines are missing: long lines were cut, older lines were left out to keep the snapshot within 256 KiB, or a member couldn't be read.";
export const UNREAD_NOTE =
  "Weave couldn't read this member's logs. It may have stopped or been removed since the list was read.";
export const RESTART_NOTE =
  "Restarted. Lines above are from the previous container.";
/** "4 lines", or "4 lines · restarted 3 times" for a member whose container restarted. */
export const memberCount = (lines: number, restarts: number) =>
  restarts === 0
    ? lineCount(lines)
    : `${lineCount(lines)} · restarted ${restarts === 1 ? "once" : `${restarts.toLocaleString("en-US")} times`}`;
export const copiedNote = (lines: number) => `Copied ${lineCount(lines)}`;
export const COPY_FAILED = "Couldn't copy. Select the lines and copy them.";

/** One member's box: its lines, or null when the runner couldn't read it. */
export interface MemberBox {
  member: string;
  log: MemberLog | null;
}
/**
 * The member boxes of a snapshot: each chosen member in the order chosen,
 * read or not, since a member that printed nothing still comes back with no
 * lines; with none chosen, every member the runner read.
 */
export function memberBoxes(
  snapshot: Pick<LogRequestView, "requestedMembers" | "members">,
): MemberBox[] {
  if (!snapshot.requestedMembers.length)
    return snapshot.members.map((log) => ({ member: log.member, log }));
  const read = new Map(snapshot.members.map((log) => [log.member, log]));
  return snapshot.requestedMembers.map((member) => ({
    member,
    log: read.get(member) ?? null,
  }));
}

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
