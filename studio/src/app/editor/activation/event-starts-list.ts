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
// What the Starts tab shows of a platform event trigger in the environment
// Studio is connected to: the trigger, the events it was offered page by page,
// and the stop an activation still owes, made here with the same checks as
// there. Drawing the tab asks for it, so it is read once for each account,
// environment, workflow and trigger, and a failed read is tried again only
// when the person asks, after its pause. Whatever the person left while it was
// read is dropped.
import { signal } from "@angular/core";
import { ApiError, type StudioApi } from "../../api";
import { describeError } from "../../errors";
import { activationRun } from "./activation-run";
import {
  applyEventRow,
  type EventTriggerView,
  type PendingStop,
} from "./event-bindings";
import {
  readStarts,
  STARTS_RETRY_MAX_MS,
  STARTS_RETRY_MS,
  type StartRow,
} from "./event-starts";
import type { TriggerApi } from "./trigger-resources-api";

/** What Starts looks at now. */
export interface StartsTarget {
  api: TriggerApi & Pick<StudioApi, "project">;
  /** Whose data it is: another account or workspace is another list. */
  scope: string;
  environmentId: string;
  /** The environment's name, for sentences. */
  environment: string;
  workflow: string;
  /** The trigger an activation made or moved for this environment. */
  triggerId: string | undefined;
  pendingStops: readonly PendingStop[];
}
export interface StartsHost {
  /** Null while Studio isn't connected to an environment. */
  target(): StartsTarget | null;
  /** The trigger's stop is no longer owed in that environment. */
  settle(environmentId: string, triggerId: string): void;
  now?(): number;
}

export type StartsState =
  | { kind: "loading" }
  | { kind: "offline" }
  /** No trigger of this workflow here. */
  | { kind: "none" }
  | { kind: "forbidden" }
  | { kind: "failed"; message: string; retryAt: number }
  | {
      kind: "list";
      trigger: {
        name: string;
        /** The version it reacts for; null when it can't be read. */
        version: string | null;
        stopped: boolean;
      };
      rows: StartRow[];
      next: string | null;
      readAt: number;
      refreshing: boolean;
      more: "idle" | "loading" | "failed";
    };

interface OwedTriggers {
  old: EventTriggerView;
  /** The trigger that stays. */
  keeps: EventTriggerView;
  /** How many more stops are owed after this one. */
  others: number;
}
export type Owed =
  /** A trigger Studio owes a stop can't be read: nothing to stop it by. */
  | { kind: "unread"; workflow: string }
  | ({ kind: "owed" | "stopping" } & OwedTriggers)
  | ({ kind: "waiting"; count: number | null } & OwedTriggers)
  | ({ kind: "changed"; text: string } & OwedTriggers)
  | ({ kind: "failed"; text: string; forbidden: boolean } & OwedTriggers)
  | ({ kind: "stopped" } & OwedTriggers);

export interface OwedCallout {
  tone: "warning" | "danger" | "success";
  lead: string;
  detail: string;
  actions: readonly ("stop" | "keep" | "now" | "refresh")[];
  busy: boolean;
}

const twice = (workflow: string) =>
  `Two triggers start ${workflow} for the same events.`;
/** What the tab says first while a stop is owed, and how it went. */
export function owedCallout(owed: Owed): OwedCallout {
  const says = (
    tone: OwedCallout["tone"],
    lead: string,
    detail: string,
    ...actions: OwedCallout["actions"]
  ): OwedCallout => ({ tone, lead, detail, actions, busy: false });
  if (owed.kind === "unread")
    return says(
      "warning",
      twice(owed.workflow),
      "Stop the extra one when you next activate.",
    );
  const { old, keeps, others } = owed;
  const both = `both start ${old.workflow_name} for every new event until you stop it.`;
  switch (owed.kind) {
    case "owed":
      return says(
        "warning",
        twice(old.workflow_name),
        others === 0
          ? `Stop ${old.name} to keep only ${keeps.name}.`
          : `Stop ${old.name}. ${others === 1 ? "Another one is" : `${others} more are`} left to stop after it.`,
        "stop",
      );
    case "stopping":
      return {
        ...says(
          "warning",
          twice(old.workflow_name),
          `Stopping ${old.name}…`,
          "stop",
        ),
        busy: true,
      };
    case "waiting":
      return owed.count === null
        ? says(
            "warning",
            `Studio can't check whether ${old.name} has events waiting to start.`,
            "Stopping it now skips any that are waiting.",
            "keep",
            "now",
          )
        : says(
            "warning",
            `${old.name} has ${owed.count} ${owed.count === 1 ? "event" : "events"} waiting to start. Stopping it now skips them.`,
            `Keep it for now and ${both}`,
            "keep",
            "now",
          );
    case "changed":
      return says(
        "warning",
        owed.text,
        "Studio stopped nothing. Refresh the starts to see what it is now.",
        "refresh",
      );
    case "failed":
      return owed.forbidden
        ? says(
            "danger",
            owed.text,
            "Ask an administrator to stop it, or for the permission.",
          )
        : says("danger", owed.text, `B${both.slice(1)}`, "stop");
    case "stopped":
      return says(
        "success",
        `Stopped ${old.name}.`,
        `Only ${keeps.name} starts ${keeps.workflow_name} now.`,
      );
  }
}

const isRefusal = (error: unknown, status: number) =>
  error instanceof ApiError && error.status === status;
const withPeriod = (text: string) => (/[.!?]$/.test(text) ? text : `${text}.`);

export class EventStartsList {
  readonly state = signal<StartsState>({ kind: "loading" });
  readonly owed = signal<Owed | null>(null);
  /** The target the list was last read for. */
  private shown: string | null = null;
  /** Grows with each read: an older one that answers late is dropped. */
  private turn = 0;
  private reading: Promise<void> = Promise.resolve();
  private keeps: EventTriggerView | null = null;
  private pending: Owed[] = [];
  private pause = 0;

  constructor(private readonly host: StartsHost) {}

  private now() {
    return this.host.now?.() ?? Date.now();
  }
  private key(target: StartsTarget | null): string {
    if (!target) return "";
    return JSON.stringify([
      target.scope,
      target.api.environment,
      target.workflow,
      target.triggerId ?? null,
    ]);
  }
  /** Whether what is shown is still what the person looks at. */
  private still(key: string) {
    return () => this.shown === key && this.key(this.host.target()) === key;
  }

  /** Reads the list for what Starts looks at now, the first time it is drawn for it. */
  show(): void {
    const key = this.key(this.host.target());
    if (this.shown === key) return;
    this.shown = key;
    this.pause = 0;
    this.reset({ kind: "loading" });
    this.reading = this.read();
  }
  /** When the read that runs, if any, has answered. */
  settled(): Promise<void> {
    return this.reading;
  }
  /** Reads it again, as the person asked: not before a failed read's pause is over. */
  refresh(): Promise<void> {
    const state = this.state();
    if (state.kind === "failed" && this.now() < state.retryAt)
      return Promise.resolve();
    if (state.kind === "list") this.state.set({ ...state, refreshing: true });
    this.reading = this.read();
    return this.reading;
  }
  private reset(state: StartsState) {
    this.keeps = null;
    this.pending = [];
    this.owed.set(null);
    this.state.set(state);
  }

  private async read(): Promise<void> {
    const target = this.host.target();
    const turn = ++this.turn;
    if (!target) return this.reset({ kind: "offline" });
    const { api, triggerId, workflow } = target;
    const environment = api.environment;
    const current = this.still(this.key(target));
    const holds = () => turn === this.turn && current();
    if (!triggerId) return this.reset({ kind: "none" });
    const one = (id: string) =>
      `${environment}/event-triggers/${encodeURIComponent(id)}`;
    let view: EventTriggerView;
    try {
      view = await api.request<EventTriggerView>(one(triggerId));
    } catch (error) {
      if (!holds()) return;
      if (isRefusal(error, 403)) return this.reset({ kind: "forbidden" });
      if (isRefusal(error, 404)) return this.reset({ kind: "none" });
      return this.failed(describeError(error).message);
    }
    if (!holds()) return;
    if (view.workflow_name !== workflow) return this.reset({ kind: "none" });
    const source =
      view.source_workflows?.length === 1
        ? view.source_workflows[0]
        : undefined;
    const run = (id: string) => activationRun(api, environment, id);
    const [starts, version, owed] = await Promise.all([
      readStarts(api, triggerId, undefined, {
        now: () => this.now(),
        ...(source ? { source } : {}),
      }),
      run(view.activation_id).then(
        (found) => found.version || null,
        () => null,
      ),
      Promise.all(
        view.disabled
          ? []
          : target.pendingStops
              .filter((stop) => stop.triggerId !== triggerId)
              .map(
                async (stop): Promise<EventTriggerView | "unread" | null> => {
                  try {
                    const old = await api.request<EventTriggerView>(
                      one(stop.triggerId),
                    );
                    // The platform's stop checks no workflow: Studio stops only
                    // this one's trigger, as it was when the stop was owed.
                    return !old.disabled &&
                      old.workflow_name === workflow &&
                      old.revision === stop.revision &&
                      old.activation_id === stop.activationId &&
                      (await run(old.activation_id)).workflow === workflow
                      ? old
                      : null;
                  } catch (error) {
                    return isRefusal(error, 404) ? null : "unread";
                  }
                },
              ),
      ),
    ]);
    if (!holds() || "dropped" in starts) return;
    if ("refused" in starts)
      return this.reset({
        kind: starts.refused === "forbidden" ? "forbidden" : "none",
      });
    if ("failed" in starts) return this.failed(starts.failed, starts.retryAt);
    this.pause = 0;
    this.keeps = view;
    const known = owed.filter((item) => item !== null);
    this.pending = known.map(
      (item, index): Owed =>
        item === "unread"
          ? { kind: "unread", workflow }
          : {
              kind: "owed",
              old: item,
              keeps: view,
              others: known.length - index - 1,
            },
    );
    this.owed.set(this.pending[0] ?? null);
    this.state.set({
      kind: "list",
      trigger: { name: view.name, version, stopped: view.disabled },
      rows: starts.rows,
      next: starts.next,
      readAt: this.now(),
      refreshing: false,
      more: "idle",
    });
  }
  private failed(message: string, retryAt?: number) {
    this.pause = Math.min(
      this.pause ? this.pause * 2 : STARTS_RETRY_MS,
      STARTS_RETRY_MAX_MS,
    );
    this.reset({
      kind: "failed",
      message,
      retryAt: retryAt ?? this.now() + this.pause,
    });
  }

  /** Twenty more; the ID of the first one, or null when none was read. */
  async more(): Promise<string | null> {
    const state = this.state();
    const target = this.host.target();
    if (
      !target?.triggerId ||
      state.kind !== "list" ||
      !state.next ||
      state.more === "loading"
    )
      return null;
    const turn = this.turn;
    const current = this.still(this.key(target));
    this.state.set({ ...state, more: "loading" });
    const source =
      this.keeps?.source_workflows?.length === 1
        ? this.keeps.source_workflows[0]
        : undefined;
    const page = await readStarts(target.api, target.triggerId, state.next, {
      now: () => this.now(),
      ...(source ? { source } : {}),
    });
    const now = this.state();
    if (turn !== this.turn || !current() || now.kind !== "list") return null;
    if (!("rows" in page)) {
      this.state.set({ ...now, more: "failed" });
      return null;
    }
    this.state.set({
      ...now,
      rows: [...now.rows, ...page.rows],
      next: page.next,
      more: "idle",
    });
    return page.rows[0]?.id ?? null;
  }

  /** Stop it: asks first when events wait to start. */
  stop(): Promise<void> {
    return this.stopOwed(false);
  }
  /** Stop now: skips the events that wait. */
  stopNow(): Promise<void> {
    return this.stopOwed(true);
  }
  /** Keep it for now: the warning comes back. */
  keep(): void {
    const owed = this.owed();
    if (owed?.kind === "waiting") this.owed.set(this.pending[0] ?? null);
  }
  private async stopOwed(stopWithWaiting: boolean): Promise<void> {
    const owed = this.owed();
    const target = this.host.target();
    if (
      !target?.triggerId ||
      !owed ||
      !(stopWithWaiting
        ? owed.kind === "waiting"
        : owed.kind === "owed" || owed.kind === "failed")
    )
      return;
    const { old, keeps, others } = owed as Owed & OwedTriggers;
    const triggers = { old, keeps, others };
    const { api, environmentId, environment } = target;
    const path = api.environment;
    const current = this.still(this.key(target));
    const turn = this.turn;
    const says = (next: Owed | null) => {
      if (turn === this.turn && current()) this.owed.set(next);
    };
    this.owed.set({ kind: "stopping", ...triggers });
    // Something must still react once the old one stops.
    try {
      const staying = await api.request<EventTriggerView>(
        `${path}/event-triggers/${encodeURIComponent(keeps.id)}`,
      );
      if (
        staying.disabled ||
        staying.revision !== keeps.revision ||
        staying.workflow_name !== keeps.workflow_name
      )
        return says({
          kind: "changed",
          text: `${keeps.name} changed since you opened this. Review it.`,
          ...triggers,
        });
    } catch (error) {
      return says({
        kind: "failed",
        text: `Couldn't stop ${old.name}: ${withPeriod(describeError(error).message)}`,
        forbidden: false,
        ...triggers,
      });
    }
    if (api.environment !== path) return;
    const result = await applyEventRow(
      api,
      { kind: "duplicate", binding: old, from: "", stop: true, text: "" },
      old.activation_id,
      { stopWithWaiting, environment },
    );
    // Whatever the person looks at now, that environment owes it no longer.
    if (result.pendingStop === null) this.host.settle(environmentId, old.id);
    switch (result.status) {
      case "stopped":
        this.pending = this.pending.slice(1);
        return says(this.pending[0] ?? { kind: "stopped", ...triggers });
      case "waiting":
        return says({
          kind: "waiting",
          count: result.waiting ?? null,
          ...triggers,
        });
      case "changed":
        return says({ kind: "changed", text: result.text, ...triggers });
      default:
        return says({
          kind: "failed",
          text: result.text,
          forbidden: result.forbidden === true,
          ...triggers,
        });
    }
  }
}
