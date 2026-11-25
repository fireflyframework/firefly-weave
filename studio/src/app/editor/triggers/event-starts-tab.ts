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
// The Starts tab of the Platform event trigger's step details: what its
// trigger started in the environment Studio is connected to, and first the
// stop an activation still owes, made here.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  Injector,
  input,
  output,
  signal,
} from "@angular/core";
import { absoluteTime, isoTime, relativeTime, shortId } from "../../format";
import { Icon } from "../../icon";
import type { IconName } from "../../icon-data";
import {
  EventStartsList,
  owedCallout,
  type OwedCallout,
  type StartsTarget,
} from "../activation/event-starts-list";
import type { StartRow } from "../activation/event-starts";
import {
  pendingStopsOf,
  storedId,
  withPendingStops,
} from "../activation/stored-resources";
import type { FormSession } from "../ndv/params/form-session";
import {
  triggerIntents,
  withTriggerIntents,
  type TriggerIntent,
} from "../state/canvas-sidecar";

/** What stands in place of the list: what there is to know, and what to do. */
interface Empty {
  title: string;
  detail: string;
  /** Try again, in this many seconds; null without the button. */
  retryIn: number | null;
}
const TICK_MS = 1_000;
const CALLOUT_ICONS: Record<OwedCallout["tone"], IconName> = {
  warning: "warning",
  danger: "failCircle",
  success: "ok",
};
const ROW_ICONS: Record<StartRow["tone"], IconName> = {
  ok: "ok",
  waiting: "spinner",
  skipped: "typeAny",
  failed: "failCircle",
};
let sequence = 0;

@Component({
  selector: "weave-event-starts-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  templateUrl: "./event-starts-tab.html",
  styleUrl: "./event-starts-tab.css",
})
export class EventStartsTab {
  readonly session = input.required<FormSession>();
  readonly trigger = input.required<TriggerIntent>();
  /** Open run: the run a start started. */
  readonly openRun = output<string>();
  /** Whether a stop is owed, as the platform says it now; null until it is read. */
  readonly owes = output<boolean | null>();
  readonly id = `event-starts-${++sequence}`;
  readonly list = new EventStartsList({
    target: () => this.target(),
    settle: (environmentId, triggerId) => this.settle(environmentId, triggerId),
  });
  readonly icons = ROW_ICONS;
  readonly calloutIcons = CALLOUT_ICONS;
  readonly shortId = shortId;
  /** The clock times and the countdown are drawn by. */
  private readonly clock = signal(Date.now());
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  constructor() {
    const timer = setInterval(() => this.clock.set(Date.now()), TICK_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
    effect(() => {
      const state = this.list.state();
      const owed = this.list.owed();
      this.owes.emit(
        state.kind === "list" ? !!owed && owed.kind !== "stopped" : null,
      );
    });
  }

  private target(): StartsTarget | null {
    const { host } = this.session();
    const environmentId = host.profile?.environmentId;
    if (!environmentId) return null;
    const trigger = this.trigger();
    return {
      api: host.api,
      scope: `${host.stepDataScope ?? ""}|${host.model.opened}`,
      environmentId,
      environment: this.environment(),
      workflow: host.model.definition.metadata.name,
      triggerId: storedId(trigger, environmentId),
      pendingStops: pendingStopsOf(trigger, environmentId),
    };
  }
  environment(): string {
    return this.session().host.environmentName || "this environment";
  }
  connected(): boolean {
    return this.target() !== null;
  }
  /** A stop made: the trigger choice owes it no longer, in the workflow it was made in. */
  private settle(environmentId: string, triggerId: string) {
    const { host } = this.session();
    const { model } = host;
    if (model.readonly || host.editingLocked) return;
    const next = withTriggerIntents(
      model.canvas,
      triggerIntents(model.canvas).map((intent) =>
        intent.id === this.trigger().id
          ? withPendingStops(
              intent,
              environmentId,
              pendingStopsOf(intent, environmentId).filter(
                (stop) => stop.triggerId !== triggerId,
              ),
            )
          : intent,
      ),
    );
    if (next !== model.canvas) host.perform(() => (model.canvas = next));
  }

  /** Drawing the tab reads the list the first time for what it shows. */
  state() {
    queueMicrotask(() => this.list.show());
    return this.list.state();
  }
  callout(): OwedCallout | null {
    const owed = this.list.owed();
    return owed && owedCallout(owed);
  }
  empty(): Empty | null {
    const state = this.list.state();
    const where = this.environment();
    const says = (title: string, detail: string): Empty => ({
      title,
      detail,
      retryIn: null,
    });
    switch (state.kind) {
      case "offline":
        return says(
          "Connect to a platform to see what this trigger started.",
          "Starts come from the platform where the workflow is active.",
        );
      case "none":
        return says(
          `No platform event trigger in ${where} yet.`,
          "Activate this workflow to create it.",
        );
      case "forbidden":
        return says(
          `You can't see what this trigger started in ${where}.`,
          "It needs permission to manage triggers and read runs there.",
        );
      case "failed":
        this.clock();
        return {
          title: "Studio couldn't load what this trigger started.",
          detail: state.message,
          retryIn: Math.max(0, Math.ceil((state.retryAt - Date.now()) / 1_000)),
        };
      case "list":
        return state.rows.length
          ? null
          : says(
              "Nothing started yet.",
              "Runs start when a matching transaction event happens.",
            );
      default:
        return null;
    }
  }
  ago(value: unknown): string {
    this.clock();
    return relativeTime(value);
  }
  readonly exact = absoluteTime;
  readonly iso = isoTime;

  async refresh() {
    await this.list.refresh();
  }
  /** Show more: focus goes to the first new one. */
  async more() {
    const first = await this.list.more();
    if (first) this.focus(`[data-start="${CSS.escape(first)}"]`);
  }
  async act(action: OwedCallout["actions"][number]) {
    switch (action) {
      case "stop":
        await this.list.stop();
        break;
      case "now":
        await this.list.stopNow();
        break;
      case "keep":
        this.list.keep();
        break;
      case "refresh":
        await this.list.refresh();
        break;
    }
    const said = this.callout();
    if (said?.tone === "success")
      this.session().announce(`${said.lead} ${said.detail}`);
    // To the safe answer, the way on, or the list once nothing is asked.
    if (said?.actions.length) this.focus(".es-callout button");
    else if (said?.tone === "danger") this.focus(".es-callout");
    else this.focus(".es-list, .es-empty", true);
  }

  /** `preventScroll` keeps what was just said in view while the list takes focus. */
  private focus(selector: string, preventScroll = false) {
    afterNextRender(
      () =>
        this.element.nativeElement
          .querySelector<HTMLElement>(selector)
          ?.focus({ preventScroll }),
      { injector: this.injector },
    );
  }
}
