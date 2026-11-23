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
// The platform's stop checks only that the person may manage triggers in the
// environment, so Studio's own checks are the only guard on which trigger it
// stops: it counts only enabled triggers of this workflow whose activation
// belongs to this workflow, replaces only the one it stored, reads a trigger
// again right before each stop and stops it only when nothing changed since
// the dialog read it, and asks before a stop that would skip events still
// waiting to start. A create whose answer was lost is adopted rather than
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
import { TOO_MANY_TRIGGERS, type PlanActivation } from "./trigger-plan";
import { MAX_PAGES, type TriggerApi } from "./trigger-resources-api";

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

export type EventRow =
  | {
      kind: "create";
      /** The workflow the trigger starts. */
      workflow: string;
      name: string;
      request: EventTriggerAsk;
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
      text: string;
    }
  /** A trigger Studio can't judge and leaves as it is. */
  | {
      kind: "blocked";
      binding: EventTriggerView;
      reason: "outside-studio" | "unknown-reconcile";
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
  | "failed";
export interface EventResult {
  status: EventStatus;
  /** The trigger the workflow runs on now: created, adopted, moved, or the one stopped. */
  id?: string;
  text: string;
  /** What the row's button does again: create the trigger, or stop the old one. */
  retry?: "create" | "stop";
  /** The stop Studio owes for this row's trigger; null clears it, absent leaves it as it is. */
  pendingStop?: PendingStop | null;
}
export interface ApplyOptions {
  /** The person chose Stop now: stop even when events wait to start. */
  stopWithWaiting?: boolean;
  /** The environment's name for sentences, such as "local". */
  environment?: string;
}

const AS_YOU =
  "Runs start as you after the move, including events waiting to start.";
const RESOLVES =
  " Its runs may resolve transactions, as you after the move. This needs permission to reconcile transactions.";
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
const declarationOf = (view: EventTriggerView): EventDeclaration => ({
  events: [...view.events],
  from: view.source_workflows ? [...view.source_workflows] : null,
});
const inPlatformOrder = (events: readonly PlatformEventType[]) =>
  EVENT_TYPES.filter((type) => events.includes(type));
const owedStop = (view: EventTriggerView): PendingStop => ({
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

/** "A", "A and B", "A, B and C". */
function joined(words: readonly string[]) {
  return words.length < 2
    ? words.join("")
    : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}
/** "Needs attention and Undone from any workflow in this project". */
function reaction(declaration: EventDeclaration) {
  const events = joined(
    EVENT_CHOICE_ORDER.filter((type) => declaration.events.includes(type)).map(
      (type) => EVENT_CHOICES[type].short,
    ),
  );
  const from = declaration.from;
  if (from === null) return `${events} from any workflow in this project`;
  const shown =
    from.length <= NAMED_SOURCES
      ? from
      : [
          ...from.slice(0, NAMED_SOURCES - 1),
          `${from.length - NAMED_SOURCES + 1} more`,
        ];
  return `${events} from ${joined(shown)}`;
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
        studioName(view.name, workflow) &&
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
        text: `Create platform event trigger ${name} for ${eventSubtitle(declaration)}`,
      });
    } else if (typeof found.may_reconcile !== "boolean") {
      rows.push({
        kind: "blocked",
        binding: found,
        reason: "unknown-reconcile",
        text: `Studio can't tell whether ${found.name} may resolve transactions, so it leaves it as it is.`,
      });
    } else {
      const from = fromOf(found);
      const reconciles = found.may_reconcile;
      const resolves = reconciles ? RESOLVES : "";
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
          text: `${found.name} was created outside Studio with other events or source workflows. Studio leaves it as it is.`,
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
    rows.push(
      duplicate
        ? {
            kind: "duplicate",
            binding: view,
            from,
            stop: true,
            text: `Two triggers start ${workflow} for the same events. Stop ${view.name}`,
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
  ((row.kind === "duplicate" || row.kind === "left-behind") && row.stop);

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
    starts: (id: string) =>
      api.request<TriggerPage>(`${one(id)}/starts?limit=50`),
    create: (body: EventTriggerRequestBody) =>
      api.mutate<EventTriggerView>(base, "POST", body),
    retarget: (id: string, activationId: string, revision: number) =>
      api.mutate<EventTriggerView>(
        `${one(id)}/retarget`,
        "POST",
        { activation_id: activationId },
        revision,
      ),
    disable: (id: string) =>
      api.mutate<EventTriggerView>(`${one(id)}/disable`, "POST", undefined),
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
/** Why a create or move was refused, with the permissions a 403 means. */
function refusedFor(error: unknown, where: string, reconciles: boolean) {
  if (error instanceof ApiError && error.status === 403)
    return reconciles
      ? `you need to manage triggers, start runs, read runs and reconcile transactions in ${where}.`
      : `you need to manage triggers, start runs and read runs in ${where}.`;
  return withPeriod(describeError(error).message);
}
const stopRefused = (error: unknown, where: string) =>
  error instanceof ApiError && error.status === 403
    ? `you need to manage triggers in ${where}.`
    : withPeriod(describeError(error).message);
const changedText = (name: string) =>
  `${name} changed since you opened this. Review it.`;
const waitingText = (name: string, count: number | null) =>
  count === null
    ? `Studio can't check whether ${name} has events waiting to start.`
    : `${name} has ${count} ${count === 1 ? "event" : "events"} waiting to start. Stopping it now skips them.`;

/** Whether `name` is one Studio gives this workflow's triggers: its own, or with a number. */
function studioName(name: string, workflow: string, planned = workflow) {
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
 * free name and creates at most once more.
 */
async function created(
  route: Routes,
  ask: EventTriggerAsk,
  activationId: string,
  workflow: string,
): Promise<
  | { ok: true; view: EventTriggerView }
  | { ok: false; name: string; error: unknown }
> {
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
        studioName(view.name, workflow, ask.name) &&
        sameTrigger(
          view,
          { ...ask, name: view.name, activation_id: activationId },
          workflow,
        ),
    );
    if (earlier) return { ok: true, view: earlier };
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

type Stop =
  | { kind: "stopped" }
  | { kind: "changed" }
  | { kind: "waiting"; count: number | null }
  | { kind: "failed"; error: unknown };

/**
 * Stops a trigger only as the dialog showed it: first, unless the person
 * chose Stop now, it checks that no event waits to start (a stop skips
 * them); then it reads the trigger again and stops it only when its revision
 * and activation are unchanged. A trigger already stopped or gone counts as
 * stopped.
 */
async function stopped(
  route: Routes,
  view: EventTriggerView,
  checkWaiting: boolean,
): Promise<Stop> {
  if (checkWaiting) {
    let count: number | null;
    try {
      const page = await route.starts(view.id);
      count = page.items.filter(
        (start) => start["status"] === "requested",
      ).length;
    } catch {
      count = null;
    }
    if (count !== 0) return { kind: "waiting", count };
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
    await route.disable(view.id);
    return { kind: "stopped" };
  } catch (error) {
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
    const made = await created(route, row.request, activationId, row.workflow);
    return made.ok
      ? {
          status: "created",
          id: made.view.id,
          text: `Created ${made.view.name}`,
        }
      : {
          status: "failed",
          retry: "create",
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
    const made = await created(route, row.request, activationId, row.workflow);
    if (!made.ok)
      return {
        status: "failed",
        retry: "create",
        text: `Couldn't create ${made.name}: ${refusedFor(made.error, where, row.request.may_reconcile)}`,
      };
    const id = made.view.id;
    const name = made.view.name;
    const stop = await stopped(route, old, checkWaiting);
    switch (stop.kind) {
      case "stopped":
        return {
          status: "replaced",
          id,
          text: `Replaced ${old.name} by ${name}`,
          pendingStop: null,
        };
      case "changed":
        return {
          status: "changed",
          id,
          text: `Created ${name}. ${changedText(old.name)}`,
          pendingStop: null,
        };
      case "waiting":
        return {
          status: "waiting",
          id,
          text: `Created ${name}. ${waitingText(old.name, stop.count)}`,
          pendingStop: owedStop(old),
        };
      case "failed":
        return {
          status: "failed",
          id,
          retry: "stop",
          text: `Created ${name}, but couldn't stop ${old.name}: ${stopRefused(stop.error, where)}`,
          pendingStop: owedStop(old),
        };
    }
  }

  // A duplicate or a trigger left behind that the person wants stopped.
  const view = row.binding;
  const owes = row.kind === "duplicate";
  const stop = await stopped(route, view, checkWaiting);
  switch (stop.kind) {
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
        text: waitingText(view.name, stop.count),
        ...(owes ? { pendingStop: owedStop(view) } : {}),
      };
    case "failed":
      return {
        status: "failed",
        id: view.id,
        retry: "stop",
        text: `Couldn't stop ${view.name}: ${stopRefused(stop.error, where)}`,
        ...(owes ? { pendingStop: owedStop(view) } : {}),
      };
  }
}
