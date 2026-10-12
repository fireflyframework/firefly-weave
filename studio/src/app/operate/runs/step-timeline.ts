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
// A run's steps from runs.steps: one bar per step instance, showing how long
// it waited in the queue and how long it ran, loop iterations grouped under
// their step, and a link to the run a step called. Outputs are read only when
// the person opens Input/Output and asks for one step; logs are not served
// yet, so the Logs tab says so without asking the platform.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import type { PlainError } from "../../errors";
import { durationText, shortId } from "../../format";
import { statusTone, toneAttribute } from "../../status-labels";
import {
  stepOutput,
  type OmissionReason,
  type StepFact,
  type StepOutput,
} from "../run-contracts";

/** Steps grouped by their static step ID, in the order the steps first appear. */
export function timelineGroups(items: readonly StepFact[]) {
  const grouped = new Map<string, StepFact[]>();
  for (const item of items)
    grouped.set(item.node_id, [...(grouped.get(item.node_id) ?? []), item]);
  return [...grouped].map(([node_id, items]) => ({ node_id, items }));
}

/** "Iteration 2 › 1" from the loop indexes, else the instance key as given. */
export function instanceLabel(item: StepFact): string {
  if (item.iteration.length) return `Iteration ${item.iteration.join(" › ")}`;
  return item.instance_key;
}

/** The time the bars share: the first scheduled or started step to the last end (or now). */
export function timelineSpan(
  items: readonly StepFact[],
  now: Date,
): { start: number; end: number } | null {
  let start = Infinity;
  let end = -Infinity;
  for (const item of items) {
    const begin = Date.parse(item.scheduled_at ?? item.started_at ?? "");
    if (!Number.isFinite(begin)) continue;
    start = Math.min(start, begin);
    const finish = item.ended_at ? Date.parse(item.ended_at) : now.getTime();
    end = Math.max(end, Number.isFinite(finish) ? finish : begin);
  }
  if (!Number.isFinite(start)) return null;
  return { start, end: Math.max(end, start + 1) };
}

/** One instance's bar, in percent of the span: where it starts, its wait and its run. */
export function stepBar(
  item: StepFact,
  span: { start: number; end: number },
  now: Date,
): { offset: number; wait: number; run: number; open: boolean } | null {
  const scheduled = Date.parse(item.scheduled_at ?? item.started_at ?? "");
  if (!Number.isFinite(scheduled)) return null;
  const started = Date.parse(item.started_at ?? "");
  const open = !item.ended_at;
  const ended = open ? now.getTime() : Date.parse(item.ended_at!);
  const width = span.end - span.start;
  const percent = (value: number) => (Math.max(0, value) / width) * 100;
  const runFrom = Number.isFinite(started) ? started : ended;
  return {
    offset: percent(scheduled - span.start),
    wait: percent(Math.min(runFrom, ended) - scheduled),
    run: percent(ended - runFrom),
    open,
  };
}

const omissionText: Record<OmissionReason, string> = {
  classified_secret: "This output is classified as secret.",
  classification_unavailable:
    "Studio can't tell whether this output is safe to show.",
  uncertain_derived: "This output may be derived from protected data.",
  confidential_text: "This output holds confidential text.",
  resource_limit: "This output is too large to show.",
};

/** One step instance's output, as the person asked for it. */
export type StepOutputState =
  | { kind: "loading" }
  | { kind: "error"; error: PlainError }
  | { kind: "ready"; output: StepOutput };

/** What the Runs page gives the run detail for its Steps section. */
export interface RunStepsView {
  runId: string;
  items: StepFact[];
  /** false when some steps ran before the platform recorded their timings. */
  complete: boolean;
  nextCursor: string | null;
  loading: boolean;
  error: PlainError | null;
  /** The server's clock, for steps still running. */
  now: Date;
  outputs: ReadonlyMap<string, StepOutputState>;
  loadMore(): void;
  retry(): void;
  readOutput(item: StepFact): void;
  openRun(id: string): void;
}

/** The key an output is kept under. */
export const instanceKey = (item: StepFact) =>
  `${item.node_id}\u0000${item.instance_key}`;

const stepWords: Record<string, string> = {
  scheduled: "Scheduled",
  running: "Running",
  waiting: "Waiting",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Canceled",
  timed_out: "Timed out",
};

type Tab = "timeline" | "io" | "logs";

@Component({
  selector: "weave-step-timeline",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrls: ["../operate.css", "./runs.css"],
  template: `@let v = view();
    <section class="steps" aria-labelledby="steps-title">
      <div class="steps-head">
        <h3 id="steps-title">Steps</h3>
        <div class="operate-tabs steps-tabs" role="tablist" aria-label="Steps">
          @for (tab of tabs; track tab[0]) {
            <button
              type="button"
              role="tab"
              [id]="'steps-tab-' + tab[0]"
              [attr.aria-selected]="selected() === tab[0]"
              [attr.aria-controls]="'steps-panel'"
              [attr.tabindex]="selected() === tab[0] ? 0 : -1"
              (click)="selected.set(tab[0])"
              (keydown)="move($event)"
            >
              {{ tab[1] }}
            </button>
          }
        </div>
      </div>
      <div
        id="steps-panel"
        role="tabpanel"
        [attr.aria-labelledby]="'steps-tab-' + selected()"
      >
        @if (selected() === "logs") {
          <p class="steps-note">
            Logs aren't available from this platform yet. Export history keeps
            the run's events.
          </p>
        } @else if (v.loading && !v.items.length) {
          <div class="steps-loading" role="status">
            <span class="sr-only">Loading steps…</span>
            @for (row of [0, 1, 2]; track row) {
              <span class="step-skeleton" aria-hidden="true"></span>
            }
          </div>
        } @else if (v.error && !v.items.length) {
          <div class="notice operate-problem" data-tone="danger" role="alert">
            <p>Studio could not load the steps. {{ v.error.message }}</p>
            @if (v.error.code) {
              <small class="support-code"
                >Support code: {{ v.error.code }}</small
              >
            }
            <button type="button" (click)="v.retry()">Try again</button>
          </div>
        } @else if (!v.items.length) {
          <p class="steps-note">No steps recorded yet.</p>
        } @else {
          @if (!v.complete) {
            <p class="steps-note">
              Some steps ran before this platform recorded step timings: they
              are listed without times.
            </p>
          }
          @if (selected() === "timeline") {
            <ol class="step-groups" aria-label="Step timeline">
              @for (group of groups(); track group.node_id) {
                <li class="step-group">
                  @if (group.items.length > 1) {
                    <p class="step-group-name">
                      {{ group.node_id }}
                      <span class="numeric"
                        >{{ group.items.length }} instances</span
                      >
                    </p>
                  }
                  <ol class="step-rows">
                    @for (item of group.items; track item.instance_key) {
                      @let bar = barOf(item);
                      <li
                        class="step-row"
                        [class.nested]="group.items.length > 1"
                      >
                        <span class="step-name"
                          ><span class="step-label">{{
                            group.items.length > 1 && item.instance_key
                              ? label(item)
                              : item.node_id
                          }}</span></span
                        ><span
                          class="status-pill"
                          [attr.data-tone]="tone(item.status)"
                          >{{ word(item.status) }}</span
                        ><span
                          class="step-track"
                          role="img"
                          [attr.aria-label]="timing(item)"
                        >
                          @if (bar) {
                            <span
                              class="step-wait"
                              [style.left.%]="bar.offset"
                              [style.width.%]="bar.wait"
                            ></span
                            ><span
                              class="step-run"
                              [class.open]="bar.open"
                              [attr.data-tone]="tone(item.status)"
                              [style.left.%]="bar.offset + bar.wait"
                              [style.width.%]="bar.run"
                            ></span>
                          }</span
                        ><span class="step-time numeric">{{ took(item) }}</span
                        ><span class="step-link">
                          @if (item.child_run_id) {
                            <button
                              type="button"
                              class="text-link"
                              [attr.aria-label]="
                                'Open run ' +
                                short(item.child_run_id) +
                                ' called by ' +
                                item.node_id
                              "
                              (click)="v.openRun(item.child_run_id)"
                            >
                              Called run {{ short(item.child_run_id) }}
                            </button>
                          }
                        </span>
                      </li>
                    }
                  </ol>
                </li>
              }
            </ol>
          } @else {
            <ul class="step-outputs" aria-label="Step outputs">
              @for (
                item of v.items;
                track item.node_id + "|" + item.instance_key
              ) {
                @let shown = outputOf(item);
                <li class="step-output">
                  <div class="step-output-head">
                    <span class="step-label"
                      >{{ item.node_id }}
                      @if (label(item)) {
                        <small>{{ label(item) }}</small>
                      }
                    </span>
                    @if (!shown || shown.kind === "error") {
                      <button
                        type="button"
                        class="tertiary"
                        (click)="v.readOutput(item)"
                      >
                        {{ shown ? "Try again" : "Show output" }}
                      </button>
                    }
                  </div>
                  @if (shown?.kind === "loading") {
                    <p class="steps-note" role="status">Loading output…</p>
                  } @else if (shown?.kind === "error") {
                    <p class="field-error" role="alert">{{ errorOf(shown) }}</p>
                  } @else if (shown?.kind === "ready") {
                    @let output = readyOf(shown);
                    @if (output.kind === "value") {
                      <pre>{{ pretty(output.value) }}</pre>
                    } @else if (output.kind === "omitted") {
                      <p class="steps-note">{{ omitted(output.reason) }}</p>
                    } @else {
                      <p class="steps-note">This step recorded no output.</p>
                    }
                  }
                </li>
              }
            </ul>
          }
          @if (v.nextCursor) {
            <button
              type="button"
              class="load-more"
              [disabled]="v.loading"
              (click)="v.loadMore()"
            >
              Load more steps
            </button>
          }
        }
      </div>
    </section>`,
})
export class StepTimeline {
  view = input.required<RunStepsView>();
  readonly tabs: [Tab, string][] = [
    ["timeline", "Timeline"],
    ["io", "Input/Output"],
    ["logs", "Logs"],
  ];
  readonly selected = signal<Tab>("timeline");
  readonly short = shortId;
  readonly label = instanceLabel;
  readonly groups = computed(() => timelineGroups(this.view().items));
  private readonly span = computed(() =>
    timelineSpan(this.view().items, this.view().now),
  );
  barOf(item: StepFact) {
    const span = this.span();
    return span ? stepBar(item, span, this.view().now) : null;
  }
  word(status: string) {
    return stepWords[status] ?? status;
  }
  tone(status: string) {
    return toneAttribute(
      statusTone("run", status === "scheduled" ? "queued" : status),
    );
  }
  took(item: StepFact) {
    if (item.duration_ms !== null) return durationText(item.duration_ms);
    if (!item.started_at || item.ended_at) return "—";
    const started = Date.parse(item.started_at);
    return Number.isFinite(started)
      ? durationText(Math.max(0, this.view().now.getTime() - started))
      : "—";
  }
  timing(item: StepFact) {
    const parts = [];
    if (item.queue_wait_ms !== null)
      parts.push(`waited ${durationText(item.queue_wait_ms)}`);
    const took = this.took(item);
    if (took !== "—")
      parts.push(item.ended_at ? `ran ${took}` : `running for ${took}`);
    return parts.length
      ? `${item.node_id}: ${parts.join(", ")}`
      : `${item.node_id}: no timings recorded`;
  }
  outputOf(item: StepFact) {
    return this.view().outputs.get(instanceKey(item)) ?? null;
  }
  errorOf(state: StepOutputState) {
    return state.kind === "error" ? state.error.message : "";
  }
  readyOf(state: StepOutputState): StepOutput {
    return state.kind === "ready" ? state.output : { kind: "none" };
  }
  omitted(reason: OmissionReason) {
    return omissionText[reason] ?? "This output can't be shown.";
  }
  pretty(value: unknown) {
    return JSON.stringify(value, null, 2);
  }
  /** Arrow keys move between the tabs (WAI-ARIA tabs). */
  move(event: KeyboardEvent) {
    const order = this.tabs.map((tab) => tab[0]);
    const index = order.indexOf(this.selected());
    const step =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = order[(index + step + order.length) % order.length];
    this.selected.set(next);
    (event.currentTarget as HTMLElement).parentElement
      ?.querySelector<HTMLElement>(`#steps-tab-${next}`)
      ?.focus();
  }
}

/** The output a step's runs.steps?include=output answer carries for it. */
export function outputFromPage(
  item: StepFact,
  page: readonly StepFact[],
): StepOutput {
  const found = page.find(
    (fact) =>
      fact.node_id === item.node_id && fact.instance_key === item.instance_key,
  );
  return found ? stepOutput(found) : { kind: "none" };
}
