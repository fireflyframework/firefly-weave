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
// What the activation dialog shows of a platform event trigger's row. Before
// activating, a row leads with what Studio does and follows with what it
// means; a row that changes who reacts, or under whose authority, says so in
// the row itself. After activating, it says what happened and what is still
// to be chosen.
import type { EventDeclaration } from "../triggers/event-trigger";
import type { IconName } from "../../icon-data";
import {
  AS_YOU,
  bothStart,
  declarationOf,
  eventNames,
  isStudioName,
  MAY_RESOLVE,
  overlap,
  owedStop,
  reacts,
  second,
  sourceNames,
  type EventResult,
  type EventRow,
  type EventStatus,
} from "./event-bindings";

export type RowTone = "plain" | "muted" | "warning" | "danger" | "success";
export interface EventRowView {
  lead: string;
  detail: string | null;
  tone: RowTone;
  icon: IconName | null;
  /** "Its runs may resolve transactions…", on a move or a replace that keeps it. */
  resolves: string | null;
  /** What reacts now against what the new version reacts to. */
  compare: {
    now: string;
    version: string;
    next: string;
    /** The new version reacts to workflows the trigger doesn't react to now. */
    wider: boolean;
  } | null;
  /** Stop it, as the person left it; null without the box. */
  stop: boolean | null;
  /** The row's button after the activation: Retry, Stop it, or Keep it for now and Stop now. */
  action: "retry" | "stop" | "choose" | null;
}

const PLAIN: EventRowView = {
  lead: "",
  detail: null,
  tone: "plain",
  icon: null,
  resolves: null,
  compare: null,
  stop: null,
  action: null,
};
const SUCCESS = { tone: "success", icon: "ok" } as const;
const DANGER = { tone: "danger", icon: "failCircle" } as const;
const WARNING = { tone: "warning", icon: "warning" } as const;
const LEFT_ALONE = { tone: "muted", icon: "lock" } as const;
/** The icon of what happened to any other kind of trigger: set up or stopped, not, or left as it is. */
export const resultIcon = (status: EventStatus): IconName =>
  status === "failed"
    ? DANGER.icon
    : status === "changed"
      ? WARNING.icon
      : SUCCESS.icon;

const same = (left: readonly string[], right: readonly string[]) =>
  left.length === right.length && left.every((item) => right.includes(item));

/** Whether the new version reacts to a workflow the trigger doesn't react to now. */
const widens = (before: string[] | null, after: string[] | null) =>
  before !== null &&
  (after === null || after.some((name) => !before.includes(name)));

/** "other events", "other workflows" or both: what a replace is for. */
function changes(before: EventDeclaration, after: EventDeclaration) {
  const sameEvents = same(before.events, after.events);
  const sameSources =
    before.from === null || after.from === null
      ? before.from === after.from
      : same(before.from, after.from);
  return sameSources
    ? "other events"
    : sameEvents
      ? "other workflows"
      : "other events and workflows";
}

/** What the dialog couldn't check before the activation, of any kind of trigger. */
export const uncheckedView = (
  lead: string,
  detail: string | null = null,
): EventRowView => ({ ...PLAIN, ...DANGER, lead, detail });

/** A row as the dialog shows it before the activation. */
export function eventRowView(row: EventRow): EventRowView {
  switch (row.kind) {
    case "create":
      return {
        ...PLAIN,
        lead: `Create platform event trigger ${row.name}`,
        detail: `For ${reacts({
          events: row.request.events,
          from: row.request.source_workflows ?? null,
        })}.`,
      };
    case "retarget":
      return {
        ...PLAIN,
        lead: `Move platform event trigger ${row.binding.name} from ${row.from} to ${row.to}`,
        detail: AS_YOU,
        resolves: row.reconciles ? MAY_RESOLVE : null,
      };
    case "replace":
      return {
        ...PLAIN,
        lead: `Replace platform event trigger ${row.binding.name}`,
        detail: `${row.to} reacts to ${changes(row.before, row.after)}, so Studio creates ${row.name}, then stops ${row.binding.name}.`,
        resolves: row.reconciles ? MAY_RESOLVE : null,
        compare: {
          now: reacts(row.before),
          version: row.to,
          next: reacts(row.after),
          wider: widens(row.before.from, row.after.from),
        },
      };
    case "duplicate":
      return {
        ...PLAIN,
        ...WARNING,
        lead: second(row.binding.workflow_name, row.binding.name, row.shared),
        detail: row.shared
          ? `Stop trigger ${row.binding.name}, which an earlier activation left running. Both react until one stops.`
          : `An earlier activation left it running for ${reacts(declarationOf(row.binding))}. Once it's stopped, those events no longer start this workflow.`,
        stop: row.stop,
      };
    case "blocked":
      return row.reason === "outside-studio"
        ? {
            ...PLAIN,
            ...LEFT_ALONE,
            lead: row.lead,
            detail:
              "Studio leaves it as it is. Change it with the command line.",
            stop: row.stop,
          }
        : {
            ...PLAIN,
            ...LEFT_ALONE,
            lead: row.lead,
            detail:
              "Ask an administrator to check the trigger with the command line.",
          };
    case "left-behind": {
      const what = `${reacts(declarationOf(row.binding))}.`;
      return {
        ...PLAIN,
        lead: `Platform event trigger ${row.binding.name} keeps running on ${row.from}`,
        detail: isStudioName(row.binding.name, row.binding.workflow_name)
          ? `For ${what}`
          : `Made with the command line for ${what}`,
        stop: row.stop,
      };
    }
  }
}

/** "both start notify-team for each event they share until you stop it…", while another trigger reacts beside this one. */
const both = (row: EventRow): string | null => {
  if (row.kind !== "replace" && row.kind !== "duplicate") return null;
  const shared =
    row.kind === "replace" ? overlap(row.before, row.after) : row.shared;
  return `${bothStart(row.binding.workflow_name, shared)} until you stop it in Starts or at the next activation.`;
};
const keepsRunning = (row: EventRow) =>
  "from" in row ? `It keeps running on ${row.from}.` : null;
/** What keeps reacting while a stop is owed or put off. */
const stillReacting = (row: EventRow) => {
  const said = both(row);
  return said ? `B${said.slice(1)}` : keepsRunning(row);
};

const unchanged = (row: Extract<EventRow, { kind: "retarget" | "replace" }>) =>
  `Nothing changed: ${row.binding.name} still reacts for ${row.from}.`;

function resultDetail(row: EventRow, result: EventResult): string | null {
  switch (result.status) {
    case "replaced":
      return row.kind === "replace"
        ? `${result.made} reacts to ${eventNames(row.after)} ${sourceNames(row.after)}. ${row.binding.name} is stopped.`
        : null;
    case "stopped":
      return row.kind === "blocked"
        ? "Activate again to create the platform event trigger this version declares."
        : null;
    case "changed":
      return `Studio ${
        row.kind === "retarget"
          ? "moved"
          : (row.kind === "create" || row.kind === "replace") && !result.made
            ? "created"
            : "stopped"
      } nothing. Close this and activate again to see what it is now.`;
    case "kept":
      return result.held
        ? "Studio stops it only after the trigger above is set up."
        : stillReacting(row);
    case "waiting":
      if (result.waiting === null)
        return "Stopping it now skips any that are waiting.";
      return both(row)
        ? `Keep it for now and ${both(row)}`
        : "from" in row
          ? `Keep it for now and it keeps running on ${row.from}.`
          : null;
    case "failed":
      if (result.retry === "stop")
        return result.forbidden
          ? "Ask an administrator to stop it, or for the permission."
          : stillReacting(row);
      if (result.forbidden)
        return "Ask an administrator for these permissions, then activate again.";
      if (row.kind === "retarget" || row.kind === "create") {
        if (result.unknown)
          return `Retry first checks whether it was ${row.kind === "create" ? "created" : "moved"}.`;
        return row.kind === "retarget" ? unchanged(row) : null;
      }
      if (row.kind !== "replace") return null;
      return result.unknown
        ? `${row.binding.name} still reacts for ${row.from}. Retry first checks whether ${result.asked} was created.`
        : unchanged(row);
    default:
      return null;
  }
}

/** A row as the dialog shows it after the activation; null when nothing was done to it. */
export function eventResultView(
  row: EventRow,
  result: EventResult | null,
): EventRowView {
  if (!result)
    return row.kind === "left-behind"
      ? { ...PLAIN, lead: `${row.binding.name} keeps running on ${row.from}` }
      : { ...eventRowView(row), stop: null };
  const { status } = result;
  return {
    ...PLAIN,
    ...(status === "failed"
      ? result.retry === "stop"
        ? WARNING
        : DANGER
      : status === "waiting" || status === "changed" || status === "kept"
        ? WARNING
        : SUCCESS),
    lead: result.text,
    detail: resultDetail(row, result),
    action:
      status === "waiting"
        ? "choose"
        : status === "failed" && !result.forbidden
          ? result.retry === "stop"
            ? "stop"
            : "retry"
          : null,
  };
}

const oldName = (row: EventRow) =>
  "binding" in row ? row.binding.name : row.name;
/** Keep it for now: the stop the waiting question asked about is put off. */
export function keptResult(row: EventRow, asked: EventResult): EventResult {
  return {
    ...asked,
    status: "kept",
    text: `${asked.made ? `Created ${asked.made}. ` : ""}Kept ${oldName(row)} for now.`,
  };
}
/** A duplicate the person unticked: both keep reacting, and the stop is owed. */
export const declinedResult = (
  row: Extract<EventRow, { kind: "duplicate" }>,
): EventResult => ({
  status: "kept",
  id: row.binding.id,
  text: `Kept ${row.binding.name} for now.`,
  pendingStop: owedStop(row.binding),
});
/** A duplicate Studio didn't stop, because the trigger that reacts as declared isn't set up. */
export const heldResult = (
  row: Extract<EventRow, { kind: "duplicate" }>,
): EventResult => ({
  status: "kept",
  id: row.binding.id,
  held: true,
  text: `Kept ${row.binding.name} for now.`,
});
