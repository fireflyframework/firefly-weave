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
// The run log: what the last execution ran, step by step, beside the canvas.
// Each entry opens its step details (Enter; I and O on its Input or Output)
// and says when it is out of date. When the run waits for a signal or a
// person the test data doesn't answer, the log asks, keeps the answer as a
// test answer and executes again. With breakpoints (F9) it steps through a
// session on the Studio host: Next, Continue, Stop.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
  type Signal,
} from "@angular/core";
import { Icon } from "../../icon";
import type { Step, Workflow } from "../../model";
import type { DebugCommandBody } from "../../designer/simulation/simulation-plan";
import type { Json } from "../ndv/registry";
import { allSteps, childLists } from "../ndv/step-walk";
import { loopProgress } from "../canvas/run-state";
import { countsOf, stepsOf } from "../canvas/trace-playback";
import { commandFor, eventCombo, KEY_PLATFORM } from "../state/keymap";
import type {
  ExecuteResponse,
  ExecuteTarget,
  TraceStatus,
} from "../state/execution";
import type { ExecutionService, HostSession } from "../state/execution-service";
import type { HumanEntry, SignalEntry } from "../state/test-data";

/** What the run log needs from the editor (the shell has every member). */
export interface RunLogHost {
  readonly model: { definition: Workflow };
  readonly execution: ExecutionService | null;
  readonly runLogOpen: Signal<boolean>;
  readonly runSession: Signal<HostSession | null>;
  readonly breakpoints: Signal<ReadonlySet<string>>;
  lastTarget: ExecuteTarget | null;
  toggleRunLog(): void;
  summaryOf(stepId: string): string | null;
  staleEntries(): ReadonlySet<string>;
  openEntry(id: string, pane?: "input" | "output"): void;
  sessionCommand(command: DebugCommandBody): Promise<void>;
  stopSession(): Promise<void>;
  answerScript(
    section: "signals" | "humans",
    stepId: string,
    entry: SignalEntry | HumanEntry,
  ): Promise<boolean>;
}

export interface RunLogEntry {
  id: string;
  status: TraceStatus | "current";
  summary: string | null;
  stale: boolean;
  breakpoint: boolean;
  /** A loop's row: how far it got, its items (nested loops' too), the items that didn't run. */
  loop?: {
    progress: number | null;
    items: LoopLogItem[];
    notRun: string[];
  };
}

const STATUS_ICONS: Record<RunLogEntry["status"], string> = {
  completed: "check",
  failed: "critical",
  waiting: "wait",
  running: "wait",
  current: "play",
};

/** One item of a loop in the run log: "Item 3", what it went through, where it failed. */
export interface LoopLogItem {
  label: string;
  status: "completed" | "failed" | "running";
  /** "check, send", "Failed at send", "at send". */
  text: string;
  /** The step its row opens: where it failed, else the last one it reached. */
  step: string;
}
/** One run of a loop in the run log. */
export interface LoopLog {
  id: string;
  within: readonly number[];
  /** "20 of 20 items", "3 of 20 items finished", "2 items". */
  summary: string;
  /** How far it got, 0 to 1; null without a count. */
  progress: number | null;
  items: LoopLogItem[];
  /** "Items 5 to 20 didn't run."; null when every item ran. */
  notRun: string | null;
}
const plural = (count: number) =>
  `${count.toLocaleString("en-US")} ${count === 1 ? "item" : "items"}`;
/** The item of each loop around an instance, outermost first: `send[2][0]` gives [2, 0]. */
const iterationOf = (key: string): number[] =>
  [...key.replace(/[#~].*$/, "").matchAll(/\[(\d+)\]/g)].map((match) =>
    Number(match[1]),
  );
/** "Item 3", or "Item 2 › 1" inside the second item of the loop around it. */
const itemLabel = (within: readonly number[], index: number) =>
  `Item ${[...within, index].map((at) => at + 1).join(" › ")}`;

/**
 * Each run of each loop in an execution, for the run log: how many items
 * finished, each item with the steps it went through (or where it failed),
 * and the items that didn't run. `loopsAround` names the loops around a step.
 */
export function loopLog(
  response: ExecuteResponse,
  loopsAround: (stepId: string) => readonly string[],
): LoopLog[] {
  const trace = stepsOf(response);
  return loopProgress(
    trace,
    trace.length,
    countsOf(response),
    loopsAround,
    true,
  ).map((run) => {
    const depth = run.within.length;
    const items = new Map<number, { steps: string[]; last: TraceStatus }>();
    let failure: { index: number; step: string } | null = null;
    for (const entry of trace) {
      if (loopsAround(entry.nodeId)[depth] !== run.id) continue;
      const iteration = iterationOf(entry.instanceKey);
      if (
        iteration.length <= depth ||
        iteration.slice(0, depth).join() !== run.within.join()
      )
        continue;
      const index = iteration[depth];
      let item = items.get(index);
      if (!item) items.set(index, (item = { steps: [], last: entry.status }));
      if (!item.steps.includes(entry.nodeId)) item.steps.push(entry.nodeId);
      item.last = entry.status;
      if (entry.status === "failed" && !failure)
        failure = { index, step: entry.nodeId };
    }
    const reached = [...items.keys()].sort((a, b) => a - b);
    const logged = reached.map((index): LoopLogItem => {
      const item = items.get(index)!;
      const last = item.steps[item.steps.length - 1];
      if (failure?.index === index)
        return {
          label: itemLabel(run.within, index),
          status: "failed",
          text: `Failed at ${failure.step}`,
          step: failure.step,
        };
      const running = item.last === "running" || item.last === "waiting";
      return {
        label: itemLabel(run.within, index),
        status: running ? "running" : "completed",
        text: running ? `at ${last}` : item.steps.join(", "),
        step: last,
      };
    });
    const count = run.count;
    const first = reached.length ? reached[reached.length - 1] + 2 : 1;
    return {
      id: run.id,
      within: run.within,
      summary:
        count === null
          ? plural(run.finished)
          : run.status === "done"
            ? `${run.finished.toLocaleString("en-US")} of ${plural(count)}`
            : `${run.finished.toLocaleString("en-US")} of ${plural(count)} finished`,
      progress: count ? Math.min(1, run.finished / count) : null,
      items: logged,
      notRun:
        count === null || first > count
          ? null
          : first === count
            ? `${itemLabel(run.within, count - 1)} didn't run.`
            : `Items ${first.toLocaleString("en-US")} to ${count.toLocaleString("en-US")} didn't run.`,
    };
  });
}

/** The header's words for an execution's answer. */
export function runStatus(
  response: ExecuteResponse,
  target: ExecuteTarget | null,
): string {
  const ran = new Set(
    response.trace
      .filter((entry) => !entry.frame && entry.status === "completed")
      .map((entry) => entry.nodeId),
  ).size;
  const steps = `${ran} step${ran === 1 ? "" : "s"}`;
  // Short enough for one line; the question below says what to do.
  const blocked = response.status === "blocked" ? response.blocked : null;
  if (blocked?.reason === "signal")
    return `Waiting for ${blocked.message.replace(/^Send signal /, "")}`;
  if (blocked?.reason === "human") return `Waiting for ${blocked.nodeId}`;
  if (blocked?.reason === "missing_mock")
    return `${blocked.nodeId} needs test data`;
  if (blocked) return `Stopped at ${blocked.nodeId}`;
  if (response.status === "failed") {
    const failed = response.trace.find((entry) => entry.status === "failed");
    // Inside a loop, the item too: "Failed at send, item 4".
    const iteration = failed ? iterationOf(failed.instanceKey) : [];
    const item = iteration.length
      ? `, item ${iteration.map((at) => at + 1).join(" › ")}`
      : "";
    return failed ? `Failed at ${failed.nodeId}${item}` : "Failed";
  }
  if (response.status === "limit") return "Stopped at the simulation's limit";
  if (target?.mode === "before") return `Paused before ${target.stepId}`;
  return `Completed · ${steps}`;
}

@Component({
  selector: "weave-run-log",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  styleUrl: "./run-log-panel.css",
  host: { class: "run-log", role: "region", "aria-label": "Run log" },
  template: `@let list = entries();
    <header class="run-log-head">
      <weave-icon name="runHistory" [size]="16" aria-hidden="true" />
      <h2 class="run-log-title">Run log</h2>
      <span class="run-log-status" role="status" [title]="status()">{{
        status()
      }}</span>
      <button
        type="button"
        class="icon-button run-log-close"
        aria-label="Close the run log"
        title="Close the run log (L)"
        (click)="host().toggleRunLog()"
      >
        <weave-icon name="close" [size]="16" />
      </button>
    </header>
    @if (!host().runSession() && host().execution?.sampledInput()) {
      <p class="run-log-note">
        Executed with a sample input. Add test input to choose your own.
      </p>
    }
    @if (host().runSession(); as session) {
      <div class="run-log-controls" role="group" aria-label="Step through">
        <button
          type="button"
          class="sm"
          [disabled]="ended(session)"
          (click)="host().sessionCommand({ kind: 'next' })"
        >
          Next
        </button>
        <button
          type="button"
          class="sm"
          [disabled]="ended(session)"
          (click)="host().sessionCommand({ kind: 'continue' })"
        >
          Continue
        </button>
        <button type="button" class="sm" (click)="host().stopSession()">
          <weave-icon name="close" [size]="16" />Stop
        </button>
      </div>
    }
    @if (question(); as asked) {
      <form class="run-log-question" (submit)="answer($event)">
        <p class="run-log-question-text">{{ asked.message }}</p>
        @if (asked.reason === "human") {
          <fieldset class="run-log-decisions">
            <legend class="sr-only">Decision</legend>
            @for (decision of asked.decisions; track decision) {
              <label class="run-log-decision">
                <input
                  type="radio"
                  name="run-log-decision"
                  [value]="decision"
                  [checked]="decision === chosen()"
                  (change)="chosen.set(decision)"
                />{{ decision }}
              </label>
            }
          </fieldset>
        }
        <label class="run-log-json-label" [for]="jsonId">{{
          asked.reason === "signal" ? "Payload (JSON)" : "Form data (JSON)"
        }}</label>
        <textarea
          class="param-input run-log-json"
          [id]="jsonId"
          rows="3"
          spellcheck="false"
          [value]="json()"
          (input)="json.set($any($event.target).value)"
        ></textarea>
        @if (problem()) {
          <p class="pane-problem" role="alert">{{ problem() }}</p>
        }
        <button type="submit" class="primary sm">
          {{ asked.reason === "signal" ? "Send" : "Decide" }}
        </button>
      </form>
    }
    @if (list.length) {
      <ul class="run-log-list" aria-label="Steps that ran">
        @for (entry of list; track entry.id) {
          <li class="run-log-row">
            <button
              type="button"
              class="run-log-entry"
              [attr.data-entry]="entry.id"
              [attr.data-status]="entry.status"
              [attr.aria-label]="label(entry)"
              (click)="host().openEntry(entry.id)"
              (keydown)="key($event, entry)"
            >
              <weave-icon
                class="run-log-icon"
                [name]="icons[entry.status]"
                [size]="16"
                aria-hidden="true"
              />
              <strong class="run-log-step">{{ entry.id }}</strong>
              <span class="run-log-summary">{{ entry.summary ?? "" }}</span>
              @if (entry.stale) {
                <span class="pill" data-tone="warning">Out of date</span>
              }
              @if (entry.breakpoint) {
                <span class="run-log-breakpoint" title="Breakpoint"></span>
              }
              @if (entry.loop?.progress !== null && entry.loop) {
                <span
                  class="run-log-progress"
                  [style.--progress]="entry.loop.progress"
                  aria-hidden="true"
                ></span>
              }
            </button>
            @if (entry.loop; as loop) {
              <ul
                class="run-log-items"
                [attr.aria-label]="'Items of ' + entry.id"
              >
                @for (item of loop.items; track item.label) {
                  <li class="run-log-row">
                    <button
                      type="button"
                      class="run-log-entry run-log-item"
                      [attr.data-status]="item.status"
                      [attr.aria-label]="item.label + ': ' + item.text"
                      (click)="host().openEntry(item.step)"
                    >
                      <weave-icon
                        class="run-log-icon"
                        [name]="icons[item.status]"
                        [size]="16"
                        aria-hidden="true"
                      />
                      <strong class="run-log-step">{{ item.label }}</strong>
                      <span class="run-log-summary">{{ item.text }}</span>
                    </button>
                  </li>
                }
                @for (note of loop.notRun; track note) {
                  <li class="run-log-not-run">{{ note }}</li>
                }
              </ul>
            }
          </li>
        }
      </ul>
    } @else {
      <p class="run-log-empty">
        Execute the workflow to see each step it runs here.
      </p>
    }`,
})
export class RunLogPanel {
  readonly host = input.required<RunLogHost>();
  readonly icons = STATUS_ICONS;
  readonly jsonId = `run-log-json-${Math.random().toString(36).slice(2, 8)}`;
  readonly json = signal("{}");
  readonly chosen = signal("");
  readonly problem = signal("");

  private steps(): Step[] {
    return allSteps(this.host().model.definition.spec.steps ?? []);
  }
  ended(session: HostSession): boolean {
    const status = String(session.view["status"] ?? "");
    return ["succeeded", "failed", "cancelled", "timed_out"].includes(status);
  }
  status(): string {
    const host = this.host();
    const session = host.runSession();
    if (session) {
      const view = session.view as {
        status?: string;
        current_nodes?: string[];
      };
      const at = (view.current_nodes ?? []).find((id) => !id.startsWith("@"));
      if (view.status === "paused" && at) return `Paused before ${at}`;
      if (view.status === "succeeded")
        return `Completed · ${this.entries().length} steps`;
      if (view.status === "failed") return `Failed${at ? ` at ${at}` : ""}`;
      return at ? `Waiting at ${at}` : "Waiting";
    }
    const last = host.execution?.last();
    return last ? runStatus(last, host.lastTarget) : "Nothing ran yet";
  }
  readonly entries = computed((): RunLogEntry[] => {
    const host = this.host();
    const marks = host.breakpoints();
    const stale = host.staleEntries();
    const session = host.runSession();
    if (session) {
      const view = session.view as {
        boundaries?: { kind?: string; node_id?: string | null }[];
        current_nodes?: string[];
      };
      const known = new Set(this.steps().map((step) => step.id));
      // What ran, then where it is paused.
      const ids = [
        ...new Set(
          [
            ...(view.boundaries ?? [])
              .filter((b) => b.kind === "node" && b.node_id)
              .map((b) => b.node_id!),
            ...(view.current_nodes ?? []),
          ]
            .map((id) => id.replace(/\[.*$/, ""))
            .filter((id) => known.has(id)),
        ),
      ];
      const current = new Set(view.current_nodes ?? []);
      return ids.map((id) => ({
        id,
        status: current.has(id) ? "current" : "completed",
        summary: null,
        stale: false,
        breakpoint: marks.has(id),
      }));
    }
    const last = host.execution?.last();
    if (!last) return [];
    // The steps inside a loop show under its row, item by item.
    const around = this.loopsAround();
    const loops = loopLog(last, around);
    const seen = new Map<string, RunLogEntry>();
    for (const entry of last.trace) {
      if (entry.frame || around(entry.nodeId).length) continue;
      const runs = loops.filter(
        (loop) =>
          loop.id === entry.nodeId || around(loop.id)[0] === entry.nodeId,
      );
      const own = runs.find((loop) => loop.id === entry.nodeId);
      seen.set(entry.nodeId, {
        id: entry.nodeId,
        status:
          own && entry.status !== "failed"
            ? own.items.some((item) => item.status === "failed")
              ? "failed"
              : entry.status
            : entry.status,
        summary: own
          ? own.summary
          : entry.status === "completed"
            ? host.summaryOf(entry.nodeId)
            : null,
        stale: stale.has(entry.instanceKey || entry.nodeId),
        breakpoint: marks.has(entry.nodeId),
        ...(own
          ? {
              loop: {
                progress: own.progress,
                items: runs.flatMap((loop) => loop.items),
                notRun: runs.flatMap((loop) =>
                  loop.notRun ? [loop.notRun] : [],
                ),
              },
            }
          : {}),
      });
    }
    return [...seen.values()];
  });
  /** The loops around each step of the workflow, outermost first. */
  private loopsAround(): (stepId: string) => readonly string[] {
    const around = new Map<string, readonly string[]>();
    const walk = (steps: readonly Step[], outer: readonly string[]) => {
      for (const step of steps) {
        if (outer.length) around.set(step.id, outer);
        const inner = step.kind === "forEach" ? [...outer, step.id] : outer;
        for (const list of childLists(step)) walk(list, inner);
      }
    };
    walk(this.host().model.definition.spec.steps ?? [], []);
    return (stepId) => around.get(stepId) ?? [];
  }
  /** What the run waits for that the test data doesn't answer: a signal or a person. */
  question() {
    const host = this.host();
    if (host.runSession()) return null;
    const blocked = host.execution?.last()?.blocked;
    if (!blocked || (blocked.reason !== "signal" && blocked.reason !== "human"))
      return null;
    const step = this.steps().find((item) => item.id === blocked.nodeId);
    const decisions = Array.isArray(step?.["decisions"])
      ? (step["decisions"] as unknown[]).map(String)
      : [];
    if (blocked.reason === "human" && !decisions.includes(this.chosen()))
      queueMicrotask(() => this.chosen.set(decisions[0] ?? ""));
    return { ...blocked, decisions };
  }
  async answer(event: Event) {
    event.preventDefault();
    const asked = this.question();
    if (!asked) return;
    let value: Json;
    try {
      value = JSON.parse(this.json() || "{}") as Json;
    } catch {
      this.problem.set("This isn't JSON yet. Check the brackets and quotes.");
      return;
    }
    this.problem.set("");
    const saved =
      asked.reason === "signal"
        ? await this.host().answerScript("signals", asked.nodeId, {
            payload: value,
          })
        : await this.host().answerScript("humans", asked.nodeId, {
            decision: this.chosen(),
            data: value,
          });
    if (saved) this.json.set("{}");
  }
  label(entry: RunLogEntry): string {
    const words: Record<RunLogEntry["status"], string> = {
      completed: "finished",
      failed: "failed",
      waiting: "waiting",
      running: "reached",
      current: "paused here",
    };
    return [
      entry.id,
      words[entry.status],
      entry.summary,
      entry.stale ? "out of date" : "",
      entry.breakpoint ? "breakpoint" : "",
    ]
      .filter(Boolean)
      .join(", ");
  }
  /** I and O open the entry's Input or Output; Enter opens its step details (the button's click). */
  key(event: KeyboardEvent, entry: RunLogEntry) {
    const command = commandFor(eventCombo(event, KEY_PLATFORM), "runLog", {
      platform: KEY_PLATFORM,
      typing: false,
    });
    if (command === "focusEntryInput" || command === "focusEntryOutput") {
      event.preventDefault();
      this.host().openEntry(
        entry.id,
        command === "focusEntryInput" ? "input" : "output",
      );
    } else if (command === "toggleRunLog") {
      event.preventDefault();
      this.host().toggleRunLog();
    }
  }
}
