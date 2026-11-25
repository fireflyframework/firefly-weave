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
// What activating a version does to the workflow's platform event trigger,
// and doing it. The platform keeps a trigger's events and source workflows
// fixed: Studio creates one for every event and workflow the version
// declares, moves it to the new version when they are unchanged, and
// replaces it when they changed: it creates the new trigger first and stops
// the old one only after that worked, so something always reacts.
//
// The platform's stop checks that the person may manage triggers in the
// environment and, with If-Match, that the trigger is still the revision
// Studio read; it never checks the workflow, so Studio's own checks guard
// which trigger it stops: it counts only enabled triggers of this workflow
// whose activation belongs to this workflow, replaces only the one it stored,
// reads a trigger again right before each stop and stops it only at the
// revision the dialog read, and asks before a stop that would skip events
// still waiting to start. A create whose answer was lost is adopted rather than
// made twice, and a stop it couldn't make is kept as owed, so two triggers
// never react to the same events unseen.
import { ApiError } from "../../api";
import { describeError } from "../../errors";
import {
  eventSubtitle,
  type EventDeclaration,
} from "../triggers/event-trigger";
import {
  EVENT_CHOICE_ORDER,
  EVENT_CHOICES,
  EVENT_TYPES,
  type PlatformEventType,
} from "../triggers/platform-event";
import type { PlanActivation } from "./trigger-plan";
import {
  MAX_PAGES,
  TOO_MANY_TRIGGERS,
  type TriggerApi,
} from "./trigger-resources-api";

/** `event_triggers.read`. */
export interface EventTriggerView {
  id: string;
  name: string;
  activation_id: string;
  events: PlatformEventType[];
  /** The workflows it reacts to; left out for any workflow. */
  source_workflows?: string[];
  /** Whether its runs may resolve transactions; left out means unknown, never false. */
  may_reconcile?: boolean;
  workflow_name: string;
  principal_id: string;
  revision: number;
  disabled: boolean;
  created_at: string;
}
/** A stop Studio owes: kept per environment when a stop failed or the person kept the old trigger for now. */
export interface PendingStop {
  triggerId: string;
  revision: number;
  activationId: string;
}
/** The body of `event_triggers.create`. */
export interface EventTriggerRequestBody {
  name: string;
  activation_id: string;
  events: PlatformEventType[];
  /** Left out for any workflow. */
  source_workflows?: string[];
  may_reconcile: boolean;
}
/** A create as the dialog plans it, before the activation it starts exists. */
export type EventTriggerAsk = Omit<EventTriggerRequestBody, "activation_id">;
/** The trigger that has to keep reacting once another one stops. */
export interface StayingTrigger {
  id: string;
  name: string;
  activationId: string;
  /** Its revision, when nothing was done to it. */
  revision?: number;
}

export type EventRow =
  | {
      kind: "create";
      /** The workflow the trigger starts. */
      workflow: string;
      name: string;
      request: EventTriggerAsk;
      /** Every trigger of the environment, as the plan read them. */
      known: readonly string[];
      text: string;
    }
  | {
      kind: "retarget";
      binding: EventTriggerView;
      from: string;
      to: string;
      /** Whether its runs may resolve transactions, which the move keeps. */
      reconciles: boolean;
      /** Found by its name, not by the ID Studio stored. */
      byName: boolean;
      text: string;
    }
  | {
      kind: "replace";
      binding: EventTriggerView;
      workflow: string;
      name: string;
      request: EventTriggerAsk;
      known: readonly string[];
      from: string;
      to: string;
      reconciles: boolean;
      before: EventDeclaration;
      after: EventDeclaration;
      text: string;
    }
  /** Another trigger reacting as declared, or one Studio owes a stop; Stop it is on. */
  | {
      kind: "duplicate";
      binding: EventTriggerView;
      from: string;
      stop: boolean;
      /** An event can start both: it shares an event and a workflow with what the version declares. */
      shared: boolean;
      text: string;
    }
  /**
   * A trigger Studio can't judge and leaves as it is. Stop it is off, and
   * offered only for one made outside Studio.
   */
  | {
      kind: "blocked";
      binding: EventTriggerView;
      reason: "outside-studio" | "unknown-reconcile";
      from: string;
      stop: boolean;
      /** Why, as the row leads with it. */
      lead: string;
      text: string;
    }
  /** A trigger the new version doesn't use; Stop it is off. */
  | {
      kind: "left-behind";
      binding: EventTriggerView;
      from: string;
      stop: boolean;
      text: string;
    };

export interface EventPlanInput {
  /** The version's `eventTrigger`; null when it reacts to no platform event. */
  declaration: EventDeclaration | null;
  /** The workflow's name. */
  workflow: string;
  /** The version being activated. */
  version: string;
  /** The trigger ID Studio stored for this environment after creating or moving it. */
  storedId: string | undefined;
  /** The stops Studio owes in this environment. */
  pendingStops: readonly PendingStop[];
  /** Every platform event trigger in the environment. */
  bindings: readonly EventTriggerView[];
  activations: readonly PlanActivation[];
}

export type EventStatus =
  | "created"
  | "moved"
  | "replaced"
  | "stopped"
  /** Events wait to start: nothing was stopped until the person chooses Stop now. */
  | "waiting"
  /** Someone changed the trigger since the dialog read it: nothing was sent. */
  | "changed"
  /** The old trigger keeps running for now, as the person chose, or because the declared one isn't set up. */
  | "kept"
  | "failed";
export interface EventResult {
  status: EventStatus;
  /** The trigger the workflow runs on now: created, adopted, moved, or the one stopped. */
  id?: string;
  text: string;
  /** The name of the trigger a create or a replace made. */
  made?: string;
  /** The person may not do this here: trying again changes nothing. */
  forbidden?: true;
  /** The answer was lost, so it may have happened; trying again looks first. */
  unknown?: true;
  /** The name the trigger would have, when a create failed. */
  asked?: string;
  /** Waiting only: how many events wait to start; null when Studio can't check. */
  waiting?: number | null;
  /** Kept only: Studio didn't stop it, because the trigger that reacts as declared isn't set up. */
  held?: true;
  /** What the row's button does again: create the trigger, or stop the old one. */
  retry?: "create" | "stop";
  /** The stop Studio owes for this row's trigger; null clears it, absent leaves it as it is. */
  pendingStop?: PendingStop | null;
  /** What must still react when the row's button stops its trigger later. */
  staying?: StayingTrigger;
}
export interface ApplyOptions {
  /** The person chose Stop now: stop even when events wait to start. */
  stopWithWaiting?: boolean;
  /** The environment's name for sentences, such as "local". */
  environment?: string;
  /** The trigger that must still react when this row's stops; a replace that has it already made its own. */
  staying?: StayingTrigger;
}

export const AS_YOU =
  "Runs start as you after the move, including events waiting to start.";
export const MAY_RESOLVE =
  "Its runs may resolve transactions, as you after the move. This needs permission to reconcile transactions.";
/** The most source workflows a replace row names before it counts the rest. */
const NAMED_SOURCES = 3;

const sameSet = (left: readonly string[], right: readonly string[]) => {
  const a = new Set(left),
    b = new Set(right);
  return a.size === b.size && [...a].every((item) => b.has(item));
};
/** Sources compare as sets; absent means any workflow, which equals only itself. */
const sameSources = (
  left: readonly string[] | null | undefined,
  right: readonly string[] | null | undefined,
) => (left == null || right == null ? left == right : sameSet(left, right));
const reactsAs = (view: EventTriggerView, declaration: EventDeclaration) =>
  sameSet(view.events, declaration.events) &&
  sameSources(view.source_workflows, declaration.from);
export const declarationOf = (view: EventTriggerView): EventDeclaration => ({
  events: [...view.events],
  from: view.source_workflows ? [...view.source_workflows] : null,
});
const inPlatformOrder = (events: readonly PlatformEventType[]) =>
  EVENT_TYPES.filter((type) => events.includes(type));
const share = (left: readonly string[], right: readonly string[]) =>
  left.some((item) => right.includes(item));
/** Whether one event can start both: they share an event and a workflow it may come from. */
export const overlap = (left: EventDeclaration, right: EventDeclaration) =>
  share(left.events, right.events) &&
  (left.from === null || right.from === null || share(left.from, right.from));
/** What is said first of a second trigger of `workflow`. */
export const second = (workflow: string, name: string, shared: boolean) =>
  shared
    ? `Two triggers start ${workflow} for the same events.`
    : `Trigger ${name} also starts this workflow, for other events.`;
/** "both start notify-team for each event they share", while two triggers react. */
export const bothStart = (workflow: string, shared: boolean) =>
  shared
    ? `both start ${workflow} for each event they share`
    : "both start this workflow, for different events,";
/** The stop Studio owes for a trigger as it was read. */
export const owedStop = (view: EventTriggerView): PendingStop => ({
  triggerId: view.id,
  revision: view.revision,
  activationId: view.activation_id,
});

/** The workflow's name, or the first free `-2`, `-3`… among the environment's enabled triggers. */
export function bindingName(
  workflow: string,
  taken: ReadonlySet<string>,
): string {
  if (!taken.has(workflow)) return workflow;
  for (let number = 2; ; number++) {
    const name = `${workflow}-${number}`;
    if (!taken.has(name)) return name;
  }
}

/** Whether an enabled trigger is exactly the one `request` would create for `workflow`. */
export function sameTrigger(
  view: EventTriggerView,
  request: EventTriggerRequestBody,
  workflow: string,
): boolean {
  return (
    !view.disabled &&
    view.name === request.name &&
    view.workflow_name === workflow &&
    view.activation_id === request.activation_id &&
    sameSet(view.events, request.events) &&
    sameSources(view.source_workflows, request.source_workflows) &&
    view.may_reconcile === request.may_reconcile
  );
}

function unknownAuthority(view: EventTriggerView) {
  const lead = `Studio can't tell whether ${view.name} may resolve transactions, so it leaves it as it is.`;
  return { lead, text: lead };
}
function outsideStudio(view: EventTriggerView, declaration: EventDeclaration) {
  const lead = `${view.name} was created outside Studio with other ${sameSet(view.events, declaration.events) ? "source workflows" : "events"}.`;
  return { lead, text: `${lead} Studio leaves it as it is.` };
}

/** "A", "A and B", "A, B and C". */
export function joined(words: readonly string[]) {
  return words.length < 2
    ? words.join("")
    : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}
/** "Needs attention and Undone": every event, in the order people choose them in. */
export const eventNames = ({ events }: EventDeclaration) =>
  joined(
    EVENT_CHOICE_ORDER.filter((type) => events.includes(type)).map(
      (type) => EVENT_CHOICES[type].short,
    ),
  );
/** "from move-between-systems", "from any workflow in this project": every workflow, however many. */
export const sourceNames = ({ from }: EventDeclaration) =>
  from === null
    ? "from any workflow in this project"
    : from.length
      ? `from ${joined(from)}`
      : "from no workflow";
/** "Needs attention · from move-between-systems". */
export const reacts = (declaration: EventDeclaration) =>
  `${eventNames(declaration)} · ${sourceNames(declaration)}`;
/** "Needs attention and Undone from any workflow in this project", the workflows past the first few counted. */
function reaction(declaration: EventDeclaration) {
  const from = declaration.from;
  const shown =
    from === null || from.length <= NAMED_SOURCES
      ? from
      : [
          ...from.slice(0, NAMED_SOURCES - 1),
          `${from.length - NAMED_SOURCES + 1} more`,
        ];
  return `${eventNames(declaration)} ${sourceNames({ ...declaration, from: shown })}`;
}

/**
 * The rows the activation dialog shows for the workflow's platform event
 * trigger: the declared trigger (create, move, replace, or blocked when
 * Studio can't judge it), then every other trigger of this workflow, as a
 * duplicate to stop or a trigger left behind.
 */
export function eventBindingPlan(input: EventPlanInput): EventRow[] {
  const { declaration, workflow, version: to } = input;
  const activations = new Map(input.activations.map((item) => [item.id, item]));
  // The platform's stop doesn't check the workflow, so this filter is the only guard.
  const live = input.bindings.filter(
    (view) =>
      !view.disabled &&
      view.workflow_name === workflow &&
      activations.get(view.activation_id)?.workflow === workflow,
  );
  const fromOf = (view: EventTriggerView) =>
    activations.get(view.activation_id)?.version ?? "";
  const taken = new Set(
    input.bindings.filter((view) => !view.disabled).map((view) => view.name),
  );
  const known = input.bindings.map((view) => view.id);
  const rows: EventRow[] = [];
  let found: EventTriggerView | undefined;

  if (declaration !== null) {
    const stored = live.find((view) => view.id === input.storedId);
    // Without the stored ID, a trigger with a name Studio gives that reacts as
    // declared is the one to move, the workflow's own name first; then the
    // trigger named after the workflow, whatever it reacts to.
    const named = live.find((view) => view.name === workflow);
    const reacting = [named, ...live].find(
      (view) =>
        view !== undefined &&
        isStudioName(view.name, workflow) &&
        reactsAs(view, declaration),
    );
    found = stored ?? reacting ?? named;
    if (!found) {
      const name = bindingName(workflow, taken);
      rows.push({
        kind: "create",
        workflow,
        name,
        request: {
          name,
          events: inPlatformOrder(declaration.events),
          ...(declaration.from === null
            ? {}
            : { source_workflows: [...declaration.from] }),
          may_reconcile: false,
        },
        known,
        text: `Create platform event trigger ${name} for ${eventSubtitle(declaration)}`,
      });
    } else if (typeof found.may_reconcile !== "boolean") {
      rows.push({
        kind: "blocked",
        binding: found,
        reason: "unknown-reconcile",
        from: fromOf(found),
        stop: false,
        ...unknownAuthority(found),
      });
    } else {
      const from = fromOf(found);
      const reconciles = found.may_reconcile;
      const resolves = reconciles ? ` ${MAY_RESOLVE}` : "";
      if (reactsAs(found, declaration))
        rows.push({
          kind: "retarget",
          binding: found,
          from,
          to,
          reconciles,
          byName: found !== stored,
          text: `Move platform event trigger ${found.name} from ${from} to ${to}. ${AS_YOU}${resolves}`,
        });
      else if (found !== stored)
        rows.push({
          kind: "blocked",
          binding: found,
          reason: "outside-studio",
          from,
          stop: false,
          ...outsideStudio(found, declaration),
        });
      else {
        const name = bindingName(workflow, taken);
        const before = declarationOf(found);
        rows.push({
          kind: "replace",
          binding: found,
          workflow,
          name,
          request: {
            name,
            events: inPlatformOrder(declaration.events),
            ...(declaration.from === null
              ? {}
              : { source_workflows: [...declaration.from] }),
            may_reconcile: reconciles,
          },
          known,
          from,
          to,
          reconciles,
          before,
          after: {
            events: [...declaration.events],
            from: declaration.from === null ? null : [...declaration.from],
          },
          text: `Replace platform event trigger ${found.name}: ${to} reacts to ${reaction(declaration)} (now: ${reaction(before)}). Studio creates ${name}, then stops ${found.name}.${resolves}`,
        });
      }
    }
  }

  // Another trigger reacting as declared starts the workflow a second time for
  // every event, so it is a duplicate, but only while a trigger reacting as
  // declared stays: a blocked row changes nothing, so beside it only a
  // trigger it already reacts as counts.
  const covered =
    rows[0]?.kind !== "blocked" ||
    (found !== undefined &&
      declaration !== null &&
      reactsAs(found, declaration));
  // An owed stop counts only while the trigger is as it was when the stop was owed.
  const owed = new Set(
    input.pendingStops
      .filter((stop) =>
        live.some(
          (view) =>
            view.id === stop.triggerId &&
            view.revision === stop.revision &&
            view.activation_id === stop.activationId,
        ),
      )
      .map((stop) => stop.triggerId),
  );
  for (const view of live) {
    if (view === found) continue;
    const from = fromOf(view);
    const duplicate =
      declaration !== null &&
      (owed.has(view.id) || (covered && reactsAs(view, declaration)));
    const shared =
      declaration !== null && overlap(declarationOf(view), declaration);
    rows.push(
      duplicate
        ? {
            kind: "duplicate",
            binding: view,
            from,
            stop: true,
            shared,
            text: `${second(workflow, view.name, shared)} Stop ${view.name}`,
          }
        : {
            kind: "left-behind",
            binding: view,
            from,
            stop: false,
            text: `Platform event trigger ${view.name} keeps running on ${from}`,
          },
    );
  }
  return rows;
}

/** Whether a row changes anything after the activation. */
export const eventRowActionable = (row: EventRow): boolean =>
  row.kind === "create" ||
  row.kind === "retarget" ||
  row.kind === "replace" ||
  ((row.kind === "duplicate" || row.kind === "left-behind") && row.stop) ||
  (row.kind === "blocked" && row.reason === "outside-studio" && row.stop);

interface TriggerPage {
  items: Record<string, unknown>[];
  next_cursor: string | null;
}
/**
 * The event trigger routes of one environment. The paths are fixed when the
 * work starts, so every request of one row goes to the environment it was
 * activated in, whatever the person chooses meanwhile.
 */
function routes(api: TriggerApi) {
  const base = `${api.environment}/event-triggers`;
  const one = (id: string) => `${base}/${encodeURIComponent(id)}`;
  return {
    list: (cursor?: string) =>
      api.request<TriggerPage>(
        `${base}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      ),
    read: (id: string) => api.request<EventTriggerView>(one(id)),
    starts: (id: string, cursor?: string) =>
      api.request<TriggerPage>(
        `${one(id)}/starts?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
      ),
    create: (body: EventTriggerRequestBody) =>
      api.mutate<EventTriggerView>(base, "POST", body),
    retarget: (id: string, activationId: string, revision: number) =>
      api.mutate<EventTriggerView>(
        `${one(id)}/retarget`,
        "POST",
        { activation_id: activationId },
        revision,
      ),
    disable: (id: string, revision: number) =>
      api.mutate<EventTriggerView>(
        `${one(id)}/disable`,
        "POST",
        undefined,
        revision,
      ),
  };
}
type Routes = ReturnType<typeof routes>;

async function listed(
  route: Routes,
): Promise<{ items: EventTriggerView[]; truncated: boolean }> {
  const items: EventTriggerView[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    const answer = await route.list(cursor);
    items.push(...(answer.items as unknown as EventTriggerView[]));
    if (answer.next_cursor === null) return { items, truncated: false };
    cursor = answer.next_cursor;
  }
  return { items, truncated: true };
}

/** Every platform event trigger in the environment, at most `MAX_PAGES` pages. */
export const listEventTriggers = (api: TriggerApi) => listed(routes(api));

const withPeriod = (text: string) => (/[.!?]$/.test(text) ? text : `${text}.`);
/** Two refusals of a create in Studio's words: the platform's name the language's field and a command. */
const CREATE_REFUSALS: Readonly<Record<string, string>> = {
  "WV-TRIGGER-EVENTS":
    "the workflow doesn't accept these platform events. Choose them in its Platform event trigger and publish a new version.",
  "WV-TRIGGER-LIMIT":
    "this environment already has 64 platform event triggers. Stop one first.",
};
const isForbidden = (error: unknown) =>
  error instanceof ApiError && error.status === 403;
/** What a failure says of the request: not allowed, or no answer, so it may have been done. */
function failure(error: unknown): { forbidden?: true; unknown?: true } {
  if (isForbidden(error)) return { forbidden: true };
  const refused =
    error instanceof ApiError
      ? error.status >= 400 && error.status < 500
      : error instanceof Error && error.message === TOO_MANY_TRIGGERS;
  return refused ? {} : { unknown: true };
}
/** Why a create or move was refused, with the permissions a 403 means. */
function refusedFor(error: unknown, where: string, reconciles: boolean) {
  if (isForbidden(error))
    return reconciles
      ? `you need to manage triggers, start runs and read runs and reconcile transactions in ${where}.`
      : `you need to manage triggers, start runs and read runs in ${where}.`;
  return (
    (error instanceof ApiError ? CREATE_REFUSALS[error.code] : undefined) ??
    withPeriod(describeError(error).message)
  );
}
const stopRefused = (error: unknown, where: string) =>
  isForbidden(error)
    ? `you need to manage triggers in ${where}.`
    : withPeriod(describeError(error).message);
const changedText = (name: string) =>
  `${name} changed since you opened this. Review it.`;
const madeText = (name: string) =>
  `${name} was created since you opened this. Review it.`;
const waitingText = (name: string, count: number | null) =>
  count === null
    ? `Studio can't check whether ${name} has events waiting to start.`
    : `${name} has ${count} ${count === 1 ? "event" : "events"} waiting to start. Stopping it now skips them.`;

/** Whether `name` is one Studio gives this workflow's triggers: its own, or with a number. */
export function isStudioName(
  name: string,
  workflow: string,
  planned = workflow,
) {
  if (name === planned || name === workflow) return true;
  const number = name.startsWith(`${workflow}-`)
    ? name.slice(workflow.length + 1)
    : "";
  return /^[0-9]+$/.test(number);
}

/**
 * Creates the trigger, adopting one an earlier attempt already made. It
 * lists first, so a create whose answer was lost is never made twice; on a
 * name conflict it lists again, adopts an equal trigger, or takes the next
 * free name and creates at most once more. A trigger one event would
 * start beside this one, and that wasn't there when the dialog read them, is
 * someone else's: the workflow would start twice, so nothing is created.
 */
async function created(
  route: Routes,
  row: Extract<EventRow, { kind: "create" | "replace" }>,
  activationId: string,
): Promise<
  | { ok: true; view: Pick<EventTriggerView, "id" | "name"> }
  | { ok: false; name: string; error: unknown }
  | { ok: false; theirs: string }
> {
  const { request: ask, workflow, known } = row;
  let name = ask.name;
  for (let attempt = 0; ; attempt++) {
    let all: { items: EventTriggerView[]; truncated: boolean };
    try {
      all = await listed(route);
    } catch (error) {
      return { ok: false, name, error };
    }
    if (all.truncated)
      return { ok: false, name, error: Error(TOO_MANY_TRIGGERS) };
    const earlier = all.items.find(
      (view) =>
        isStudioName(view.name, workflow, ask.name) &&
        sameTrigger(
          view,
          { ...ask, name: view.name, activation_id: activationId },
          workflow,
        ),
    );
    if (earlier) return { ok: true, view: earlier };
    const theirs = all.items.find(
      (view) =>
        !view.disabled &&
        view.workflow_name === workflow &&
        !known.includes(view.id) &&
        overlap(declarationOf(view), {
          events: ask.events,
          from: ask.source_workflows ?? null,
        }),
    );
    if (theirs) return { ok: false, theirs: theirs.name };
    const taken = new Set(
      all.items.filter((view) => !view.disabled).map((view) => view.name),
    );
    if (taken.has(name)) name = bindingName(workflow, taken);
    try {
      return {
        ok: true,
        view: await route.create({
          ...ask,
          name,
          activation_id: activationId,
        }),
      };
    } catch (error) {
      const conflict =
        error instanceof ApiError && error.code === "WV-TRIGGER-CONFLICT";
      if (!conflict || attempt > 0) return { ok: false, name, error };
    }
  }
}

/**
 * How many of a trigger's starts still wait, newest first. The platform
 * can't filter them, so it reads page after page while a full page holds
 * none waiting, at most `MAX_PAGES` pages (500 starts): the count is that of
 * the first page that holds any. Null when that many pages hold none and
 * more remain: Studio can't tell, so it asks.
 */
async function waitingStarts(
  route: Routes,
  id: string,
): Promise<number | null> {
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page++) {
    const answer = await route.starts(id, cursor);
    const count = answer.items.filter(
      (start) => start["status"] === "requested",
    ).length;
    if (count !== 0 || answer.next_cursor === null) return count;
    cursor = answer.next_cursor;
  }
  return null;
}

type Stop =
  | { kind: "stopped" }
  | { kind: "changed" }
  /** The one that has to keep reacting doesn't, or is no longer as it was. */
  | { kind: "alone" }
  | { kind: "waiting"; count: number | null }
  | { kind: "failed"; error: unknown };

/**
 * Stops a trigger only as the dialog showed it: first, unless the person
 * chose Stop now, it checks that no event waits to start (a stop skips
 * them); then that the one that stays, if any, still reacts; then it reads the
 * trigger again and stops it only when its revision and activation are
 * unchanged. The stop sends the revision the dialog read as If-Match, so a
 * trigger moved between that read and the stop keeps running and counts as
 * changed. A trigger already stopped or gone counts as stopped.
 */
async function stopped(
  route: Routes,
  view: EventTriggerView,
  checkWaiting: boolean,
  staying?: StayingTrigger,
): Promise<Stop> {
  if (checkWaiting) {
    const count = await waitingStarts(route, view.id).catch(() => null);
    if (count !== 0) return { kind: "waiting", count };
  }
  if (staying) {
    try {
      const now = await route.read(staying.id);
      if (
        now.disabled ||
        now.workflow_name !== view.workflow_name ||
        now.activation_id !== staying.activationId ||
        (staying.revision !== undefined && now.revision !== staying.revision)
      )
        return { kind: "alone" };
    } catch (error) {
      return error instanceof ApiError && error.status === 404
        ? { kind: "alone" }
        : { kind: "failed", error };
    }
  }
  let fresh: EventTriggerView;
  try {
    fresh = await route.read(view.id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404)
      return { kind: "stopped" };
    return { kind: "failed", error };
  }
  if (fresh.disabled) return { kind: "stopped" };
  if (
    fresh.revision !== view.revision ||
    fresh.activation_id !== view.activation_id ||
    fresh.workflow_name !== view.workflow_name
  )
    return { kind: "changed" };
  try {
    await route.disable(view.id, view.revision);
    return { kind: "stopped" };
  } catch (error) {
    if (error instanceof ApiError && error.code === "WV-REVISION")
      return { kind: "changed" };
    return { kind: "failed", error };
  }
}

/**
 * Does what one row says, after the activation `activationId` succeeded.
 * Running a row again is safe: a create adopts what an earlier run made, and
 * a stop reads the trigger again first. Only actionable rows may be applied.
 */
export async function applyEventRow(
  api: TriggerApi,
  row: EventRow,
  activationId: string,
  options: ApplyOptions = {},
): Promise<EventResult> {
  if (!eventRowActionable(row))
    throw Error("This row changes nothing after the activation.");
  const where = options.environment ?? "this environment";
  const route = routes(api);
  const checkWaiting = !options.stopWithWaiting;

  if (row.kind === "create") {
    const made = await created(route, row, activationId);
    if ("theirs" in made)
      return { status: "changed", text: madeText(made.theirs) };
    return made.ok
      ? {
          status: "created",
          id: made.view.id,
          made: made.view.name,
          text: `Created ${made.view.name}`,
        }
      : {
          status: "failed",
          retry: "create",
          ...failure(made.error),
          asked: made.name,
          text: `Couldn't create ${made.name}: ${refusedFor(made.error, where, false)}`,
        };
  }

  if (row.kind === "retarget") {
    const name = row.binding.name;
    const moved = { status: "moved" as const, id: row.binding.id };
    const text = `Moved ${name} to ${row.to}`;
    try {
      await route.retarget(row.binding.id, activationId, row.binding.revision);
      return { ...moved, text };
    } catch (error) {
      if (!(error instanceof ApiError && error.code === "WV-REVISION"))
        return {
          status: "failed",
          ...failure(error),
          text: `Couldn't move ${name}: ${refusedFor(error, where, row.reconciles)}`,
        };
      // Its revision moved on: by this same move whose answer was lost, or by someone else.
      const now = await route.read(row.binding.id).catch(() => null);
      return now && !now.disabled && now.activation_id === activationId
        ? { ...moved, text }
        : { status: "changed", text: changedText(name) };
    }
  }

  if (row.kind === "replace") {
    const old = row.binding;
    const { staying } = options;
    const made = staying
      ? { ok: true as const, view: staying }
      : await created(route, row, activationId);
    if ("theirs" in made)
      return { status: "changed", text: madeText(made.theirs) };
    if (!made.ok)
      return {
        status: "failed",
        retry: "create",
        ...failure(made.error),
        asked: made.name,
        text: `Couldn't create ${made.name}: ${refusedFor(made.error, where, row.request.may_reconcile)}`,
      };
    const name = made.view.name;
    const done = { id: made.view.id, made: name };
    const stop = await stopped(route, old, checkWaiting, staying);
    switch (stop.kind) {
      case "alone":
        return {
          status: "changed",
          ...done,
          text: changedText(name),
          pendingStop: owedStop(old),
        };
      case "stopped":
        return {
          status: "replaced",
          ...done,
          text: `Replaced by ${name}`,
          pendingStop: null,
        };
      case "changed":
        return {
          status: "changed",
          ...done,
          text: `Created ${name}. ${changedText(old.name)}`,
          pendingStop: null,
        };
      case "waiting":
        return {
          status: "waiting",
          ...done,
          waiting: stop.count,
          text: `Created ${name}. ${waitingText(old.name, stop.count)}`,
          pendingStop: owedStop(old),
        };
      case "failed":
        return {
          status: "failed",
          ...done,
          retry: "stop",
          ...failure(stop.error),
          text: `Created ${name}, but couldn't stop ${old.name}: ${stopRefused(stop.error, where)}`,
          pendingStop: owedStop(old),
        };
    }
  }

  // A duplicate, a trigger left behind or one made outside Studio that the person wants stopped.
  const view = row.binding;
  const { staying } = options;
  const later = {
    ...(row.kind === "duplicate" ? { pendingStop: owedStop(view) } : {}),
    ...(staying ? { staying } : {}),
  };
  const stop = await stopped(route, view, checkWaiting, staying);
  switch (stop.kind) {
    case "alone":
      return {
        status: "changed",
        id: view.id,
        text: changedText(staying!.name),
        pendingStop: owedStop(view),
      };
    case "stopped":
      return {
        status: "stopped",
        id: view.id,
        text: `Stopped ${view.name}`,
        pendingStop: null,
      };
    case "changed":
      return {
        status: "changed",
        id: view.id,
        text: changedText(view.name),
        pendingStop: null,
      };
    case "waiting":
      return {
        status: "waiting",
        id: view.id,
        waiting: stop.count,
        text: waitingText(view.name, stop.count),
        ...later,
      };
    case "failed":
      return {
        status: "failed",
        id: view.id,
        retry: "stop",
        ...failure(stop.error),
        text: `Couldn't stop ${view.name}: ${stopRefused(stop.error, where)}`,
        ...later,
      };
  }
}
