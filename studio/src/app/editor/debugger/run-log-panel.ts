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
// test answer and executes again. When it stops at an undo step of a
// transaction that needs test output, it asks for that first. With
// breakpoints (F9) it steps through a session on the Studio host: Next,
// Continue, Stop.
import { NgTemplateOutlet } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  afterNextRender,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
  untracked,
  type Signal,
  type WritableSignal,
} from "@angular/core";
import { Icon } from "../../icon";
import type { Step, Workflow } from "../../model";
import type { DebugCommandBody } from "../../designer/simulation/simulation-plan";
import { ndvRegistry, type Json } from "../ndv/registry";
import {
  StepDetailsService,
  type StepDetailsRequest,
} from "../ndv/step-details-service";
import { allSteps, childLists } from "../ndv/step-walk";
import {
  errorPathsAround,
  phaseBlock,
  type PhaseBlock,
} from "../state/execute-gate";
import { splitPhase } from "../transactions/phase-keys";
import {
  askOnce,
  PhaseOutputDialog,
} from "../transactions/phase-output-dialog";
import { PHASE_NOUNS } from "../transactions/simulate-undo";
import { simulatedRollback } from "../transactions/rollback";
import { TRANSACTION_CODE_TEXT } from "../transactions/transaction-words";
import { LOOP_LIMIT, loopProgress, type CanvasRun } from "../canvas/run-state";
import { failureCodeText } from "../canvas/tile-facts";
import { getAt, isJsonObject, parsePointer } from "../../forms/core/json";
import { stepPointerOf } from "../formula/loops";
import { loopLimitText } from "../canvas/loop-frame";
import {
  countsOf,
  overLimitOf,
  pausedOf,
  runFailureOf,
  stepsOf,
  stoppedAt,
} from "../canvas/trace-playback";
import { commandFor, eventCombo, KEY_PLATFORM } from "../state/keymap";
import type { CalledRuns } from "../state/execute-request";
import type {
  ExecuteResponse,
  ExecuteTarget,
  TraceStatus,
} from "../state/execution";
import { instanceStep } from "../state/instance-keys";
import type { ExecutionService, HostSession } from "../state/execution-service";
import type {
  FrameKey,
  HumanEntry,
  SignalEntry,
  StudioTestData,
} from "../state/test-data";
import { withCalleeMock, withScript } from "../state/test-data-ops";
import { transactionSummary } from "../state/run-summary";
import {
  undoneFor,
  withTransactions,
  type TransactionLog,
} from "./run-log-transactions";

/** What the run log needs from the editor (the shell has every member). */
export interface RunLogHost {
  readonly model: { definition: Workflow };
  readonly execution: ExecutionService | null;
  readonly runLogOpen: Signal<boolean>;
  readonly runSession: Signal<HostSession | null>;
  /** The last execution as the canvas plays it back. */
  readonly playback?: Signal<CanvasRun | null>;
  /** Why the last Execute workflow didn't start, such as too many called workflows; null once one starts. */
  readonly executeRefusal?: Signal<string | null>;
  readonly breakpoints: Signal<ReadonlySet<string>>;
  lastTarget: ExecuteTarget | null;
  /** The Workflow panel shows; the run log closes it to make room below 1,600 px. */
  showInspector: boolean;
  readonly windowWidth: number;
  readonly draftId: string;
  /** Step details open a step on this item of a loop (`send[2]`). */
  readonly openedItem?: WritableSignal<{ draftId: string; key: string } | null>;
  /** Draws the editor again after a change the run log made to it. */
  refreshView(): void;
  toggleRunLog(): void;
  summaryOf(stepId: string): string | null;
  staleEntries(): ReadonlySet<string>;
  openEntry(id: string, pane?: "input" | "output"): void;
  /** Step details on a step, on the part asked for. */
  openStepDetails?(
    target: string,
    request?: Partial<StepDetailsRequest>,
  ): Promise<boolean>;
  sessionCommand(command: DebugCommandBody): Promise<void>;
  stopSession(): Promise<void>;
  /** The open workflow's test data, once it is read. */
  readonly testData: { data(): StudioTestData } | null;
  /**
   * Keeps a change of test data (checked like all test data at `pointer`);
   * then, when asked, the last execution runs again with it.
   */
  keepForRunLog(
    change: (data: StudioTestData, now: string) => StudioTestData,
    pointer: string,
    executeAgain: boolean,
  ): Promise<boolean>;
}
/** A key as one segment of a JSON pointer. */
const segment = (key: string) =>
  "/" + key.replace(/~/g, "~0").replace(/\//g, "~1");

export interface RunLogEntry {
  id: string;
  /** "handled": the step failed and its On error went on. */
  status: TraceStatus | "current" | "handled";
  summary: string | null;
  stale: boolean;
  breakpoint: boolean;
  /**
   * A handled failure's code, or the code the run stopped at this step with,
   * as given (cut at 128), on a line of its own.
   */
  code?: string;
  /** Why the run stopped here although the step's On error goes on. */
  reason?: string;
  /**
   * In an error path: the steps whose error paths hold it, outermost first;
   * `first` heads its path with "Error path".
   */
  path?: { owners: readonly string[]; first: boolean };
  /** A loop's row: how far it got, its items (nested loops' too), the items that didn't run. */
  loop?: {
    progress: number | null;
    items: LoopLogItem[];
    notRun: string[];
    /** A list longer than Up to: what it means and what to do. */
    message?: string;
  };
  /** The workflows this step called, with what ran inside them. */
  frames?: FrameLog[];
  /** The transactions undone or finished after it, with their undo steps. */
  transactions?: TransactionLog[];
}

const STATUS_ICONS: Record<RunLogEntry["status"], string> = {
  completed: "check",
  failed: "critical",
  handled: "degraded",
  waiting: "wait",
  running: "wait",
  current: "play",
};

/** One item of a loop in the run log: "Item 3", what it went through, where it failed. */
export interface LoopLogItem {
  label: string;
  /** "handled": a step failed in it and its On error went on. */
  status: "completed" | "failed" | "running" | "handled";
  /** "check, send", "Failed at send", "Failed, continued at send", "at send". */
  text: string;
  /** A handled failure's code, as given (cut at 128). */
  code?: string;
  /** The step its row opens, on this item: where it failed, else the last one it reached (`send[2]`). */
  key: string;
  /** The failed item: each step it went through, the failed one last; it shows open. */
  steps?: LoopLogStep[];
  /** The failed item: the error the run stopped with, when it says. */
  message?: string;
}
/** A step a failed item went through, with what it returned or the code it failed with. */
export interface LoopLogStep {
  id: string;
  /** Opens the step on the item (`check[2]`). */
  key: string;
  status: TraceStatus;
  /** "3 fields", or the failed step's error code; null when there is nothing to say. */
  text: string | null;
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
  /** A list longer than Up to: what it means and what to do. */
  message?: string;
}
/** A Fail step's code and message, from the workflow; undefined for another step. */
function failCopy(
  definition: unknown,
  stepId: string,
): { code?: string; message?: string } | undefined {
  const pointer = stepPointerOf(definition, stepId);
  const step =
    pointer === null ? null : getAt(definition, parsePointer(pointer)!);
  if (!isJsonObject(step) || step["kind"] !== "fail") return undefined;
  const text = (value: unknown) =>
    typeof value === "string" && value ? value : undefined;
  return { code: text(step["code"]), message: text(step["message"]) };
}
/** Why a step whose On error goes on still stopped the run. */
const UNKNOWN_STOP =
  "Its outcome is unknown and it changes data, so the run stopped with an incident, whatever On error says.";

/**
 * The step of this workflow the run stopped at, for its row: the code it
 * stopped with (the run's incident, else a Fail step's code, else the step's
 * own error) and, when its On error goes on but its outcome was unknown,
 * why the run stopped anyway. Null when the run didn't stop at a step.
 */
export function stoppedRow(
  response: ExecuteResponse,
  definition: unknown,
): { step: string; code: string; reason: string | null } | null {
  if (response.status !== "failed") return null;
  const entry = stoppedAt(response);
  if (!entry) return null;
  const code =
    runFailureOf(response, entry.nodeId, definition)?.code ??
    failCopy(definition, entry.nodeId)?.code ??
    entry.error?.code;
  if (!code) return null;
  const pointer = stepPointerOf(definition, entry.nodeId);
  const step =
    pointer === null ? null : getAt(definition, parsePointer(pointer)!);
  const goesOn =
    isJsonObject(step) &&
    (step["onError"] === "continue" || step["onError"] === "errorOutput");
  return {
    step: entry.nodeId,
    code: failureCodeText(code),
    reason: goesOn && entry.error?.outcome === "unknown" ? UNKNOWN_STOP : null,
  };
}
/** What a step in a failed item returned: "3 fields", "2 items"; null for paths, loops and plain values. */
function outputText(kind: string, output: Json | undefined): string | null {
  if (output === undefined || ["switch", "parallel", "forEach"].includes(kind))
    return null;
  const count = Array.isArray(output)
    ? { n: output.length, word: "item" }
    : output !== null && typeof output === "object"
      ? { n: Object.keys(output).length, word: "field" }
      : null;
  return count
    ? `${count.n.toLocaleString("en-US")} ${count.word}${count.n === 1 ? "" : "s"}`
    : null;
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
 * With the workflow, a list longer than Up to says how many items and Up to.
 */
export function loopLog(
  response: ExecuteResponse,
  loopsAround: (stepId: string) => readonly string[],
  definition?: unknown,
): LoopLog[] {
  const trace = stepsOf(response);
  const entries = response.trace.filter((entry) => !entry.frame);
  // The step the run failed at, and its error, said once under its item:
  // the answer's, else a Fail step's own code and message. A failure a
  // step's On error handled went on: it isn't where the run stopped. One
  // its transaction was undone for has its own error, not the transaction's.
  const failedAt = stoppedAt(response);
  const diagnostic = response.diagnostics?.[0];
  const error =
    (TRANSACTION_CODE_TEXT[diagnostic?.code ?? ""] && failedAt?.error) ||
    (diagnostic ??
      (failedAt ? failCopy(definition, failedAt.nodeId) : undefined));
  return loopProgress(
    trace,
    trace.length,
    countsOf(response),
    loopsAround,
    true,
    pausedOf(response),
  ).map((run) => {
    const depth = run.within.length;
    const items = new Map<
      number,
      { steps: string[]; last: TraceStatus; key: string; ran: number[] }
    >();
    let failure: { index: number; step: string; key: string } | null = null;
    // Each item a step failed in and went on: its first such failure.
    const handled = new Map<number, { step: string; key: string }>();
    for (const [position, entry] of trace.entries()) {
      if (loopsAround(entry.nodeId)[depth] !== run.id) continue;
      const iteration = iterationOf(entry.instanceKey);
      if (
        iteration.length <= depth ||
        iteration.slice(0, depth).join() !== run.within.join()
      )
        continue;
      const index = iteration[depth];
      let item = items.get(index);
      if (!item)
        items.set(
          index,
          (item = { steps: [], last: entry.status, key: "", ran: [] }),
        );
      if (!item.steps.includes(entry.nodeId)) item.steps.push(entry.nodeId);
      item.last = entry.status;
      item.key = entry.instanceKey || entry.nodeId;
      // The steps directly in this loop's body, paths included; a transaction
      // has its own row.
      if (
        loopsAround(entry.nodeId).length === depth + 1 &&
        entry.kind !== "transaction"
      )
        item.ran.push(position);
      const key = entry.instanceKey || entry.nodeId;
      if (entry.status === "failed" && entry.handled) {
        if (!handled.has(index))
          handled.set(index, { step: entry.nodeId, key });
      } else if (entry.status === "failed" && !failure)
        failure = { index, step: entry.nodeId, key };
    }
    const reached = [...items.keys()].sort((a, b) => a - b);
    const logged = reached.map((index): LoopLogItem => {
      const item = items.get(index)!;
      const last = item.steps[item.steps.length - 1];
      if (failure?.index === index) {
        const stopped = entries[item.ran.at(-1) ?? -1] === failedAt;
        return {
          label: itemLabel(run.within, index),
          status: "failed",
          text: `Failed at ${failure.step}`,
          key: failure.key,
          steps: item.ran.map((position): LoopLogStep => {
            const entry = entries[position];
            const failed = entry.status === "failed";
            return {
              id: entry.nodeId,
              key: entry.instanceKey || entry.nodeId,
              // A path it took is done with once a step on it failed.
              status: failed
                ? "failed"
                : entry.status === "running"
                  ? "completed"
                  : entry.status,
              text: failed
                ? entry === failedAt && error?.code
                  ? error.code
                  : null
                : outputText(entry.kind, entry.output),
            };
          }),
          ...(stopped && error?.message ? { message: error.message } : {}),
        };
      }
      // A step failed in it and its On error went on: where, and its code.
      const went = handled.get(index);
      if (went && item.last !== "running" && item.last !== "waiting") {
        const code = entries.find(
          (entry) =>
            entry.status === "failed" &&
            (entry.instanceKey || entry.nodeId) === went.key,
        )?.error?.code;
        return {
          label: itemLabel(run.within, index),
          status: "handled",
          text: `Failed, continued at ${went.step}`,
          ...(code ? { code: failureCodeText(code) } : {}),
          key: went.key,
        };
      }
      // The run ended inside a paused loop's items after the ones it finished.
      const running =
        item.last === "running" ||
        item.last === "waiting" ||
        (run.status === "paused" && index >= run.finished);
      return {
        label: itemLabel(run.within, index),
        status: running ? "running" : "completed",
        text: running ? `at ${last}` : item.steps.join(", "),
        key: item.key,
      };
    });
    // A list longer than Up to: its loop failed before any item.
    if (run.overLimit) {
      const over =
        definition === undefined
          ? null
          : overLimitOf(definition, response, run.id);
      return {
        id: run.id,
        within: run.within,
        summary: !over
          ? "more items than Up to"
          : over.count === null
            ? `more items than Up to ${over.limit.toLocaleString("en-US")}`
            : `${plural(over.count)}, Up to ${over.limit.toLocaleString("en-US")}`,
        progress: null,
        items: [],
        notRun: null,
        ...(over ? { message: loopLimitText(over.count, over.limit) } : {}),
      };
    }
    // Another reason it failed before its first item: the run's own words.
    const self = entries.find(
      (entry) =>
        entry.kind === "forEach" &&
        entry.nodeId === run.id &&
        entry === failedAt &&
        iterationOf(entry.instanceKey).join() === run.within.join(),
    );
    if (run.status === "failed" && !reached.length && self && error?.code)
      return {
        id: run.id,
        within: run.within,
        summary: `Failed: ${error.code}`,
        progress: null,
        items: [],
        notRun: null,
        ...(error.message ? { message: error.message } : {}),
      };
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

// Called workflows: the steps that ran inside one show under the call that
// ran it, and a call inside it under its own step.

/** A step that ran inside a called workflow, as the run log lists it. */
export interface FrameStep {
  id: string;
  kind: string;
  status: TraceStatus;
  /** "2 fields": what its output holds; null without one. */
  summary: string | null;
  output?: Json;
  /** The run stopped here: the step needs test output. */
  needsData: boolean;
  /** The workflows this step called, with what ran inside them. */
  frames: FrameLog[];
}
/** One run of a called workflow during an execution. */
export interface FrameLog {
  key: FrameKey;
  /** `notify-customer@1.0.0`: where test output for its steps goes. */
  workflow: string;
  name: string;
  version: string;
  /** "Item 3" when one item of a loop made the call. */
  item: string | null;
  status: TraceStatus;
  steps: FrameStep[];
}
const counted = (count: number, word: string) =>
  `${count.toLocaleString("en-US")} ${word}${count === 1 ? "" : "s"}`;
/** What a called workflow's step returned, in a few words. */
function framedSummary(output: Json | undefined): string | null {
  if (Array.isArray(output)) return counted(output.length, "item");
  if (output && typeof output === "object")
    return counted(Object.keys(output).length, "field");
  return null;
}
/** "Item 3", or "Item 2 › 1" inside the second item of the loop around it; null outside loops. */
function callItem(callNode: string): string | null {
  const at = [...callNode.matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1]) + 1);
  return at.length ? `Item ${at.join(" › ")}` : null;
}

/**
 * The workflows each step of this workflow called during an execution, by
 * step ID: each with the steps that ran inside it, in the order they ran,
 * and the workflows those steps called under them. Each status is its
 * run's, as the host gives it.
 */
export function frameLog(
  called: CalledRuns | null,
): ReadonlyMap<string, FrameLog[]> {
  const top = new Map<string, FrameLog[]>();
  if (!called) return top;
  const logs = new Map<string, FrameLog>();
  for (const view of called.frames) {
    const at = view.workflow.lastIndexOf("@");
    logs.set(view.key, {
      key: view.key,
      workflow: view.workflow,
      name: at > 0 ? view.workflow.slice(0, at) : view.workflow,
      version: at > 0 ? view.workflow.slice(at + 1) : "",
      item: callItem(view.callNode),
      status: view.status,
      steps: [],
    });
  }
  // One row per step, where it first ran, with how it last ended.
  for (const entry of called.trace) {
    const log = logs.get(entry.frame ?? "");
    if (!log) continue;
    const row: FrameStep = {
      id: entry.nodeId,
      kind: entry.kind,
      status: entry.status,
      summary:
        entry.status === "completed" ? framedSummary(entry.output) : null,
      ...(entry.output !== undefined ? { output: entry.output } : {}),
      needsData: false,
      frames: [],
    };
    const at = log.steps.findIndex((step) => step.id === entry.nodeId);
    if (at < 0) log.steps.push(row);
    else log.steps[at] = { ...row, frames: log.steps[at].frames };
  }
  const stop = called.blocked;
  const stopped = stop?.frame
    ? logs.get(stop.frame)?.steps.find((step) => step.id === stop.nodeId)
    : undefined;
  if (stopped) stopped.needsData = true;
  // Each under the step that called it: this workflow's, or one inside the workflow that called it.
  for (const view of called.frames) {
    const log = logs.get(view.key)!;
    const step = instanceStep(view.callNode);
    const cut = view.key.lastIndexOf("/");
    if (cut < 0) {
      top.set(step, [...(top.get(step) ?? []), log]);
      continue;
    }
    const parent = logs.get(view.key.slice(0, cut));
    if (!parent) continue;
    let row = parent.steps.find((item) => item.id === step);
    if (!row)
      parent.steps.push(
        (row = {
          id: step,
          kind: "callWorkflow",
          status: view.status,
          summary: null,
          needsData: false,
          frames: [],
        }),
      );
    row.frames.push(log);
  }
  return top;
}
/** A called workflow that failed or is still running starts open; one that finished starts closed. */
export const opensFirst = (frame: FrameLog): boolean =>
  frame.status !== "completed";
/** Test output stands in for a task (an action, a model) or for the step the run stopped at. */
export const takesTestOutput = (step: FrameStep): boolean =>
  step.needsData || step.kind === "action" || step.kind === "llm";
/** The call's own row when a workflow it called failed: "Failed in notify-customer". */
export function failedIn(frames: readonly FrameLog[]): string | null {
  const failed = frames.find((frame) => frame.status === "failed");
  return failed ? `Failed in ${failed.name}` : null;
}
/** The workflows that ran during an execution, each once, in the order their calls started. */
export function calledNames(called: CalledRuns | null): string[] {
  return [
    ...new Set(
      (called?.frames ?? []).map((view) =>
        view.workflow.slice(0, view.workflow.lastIndexOf("@")),
      ),
    ),
  ];
}
/** Each call's row with the workflows it called; a call whose workflow failed says in which. */
function withFrames(
  entries: RunLogEntry[],
  calls: ReadonlyMap<string, FrameLog[]>,
): RunLogEntry[] {
  return entries.map((entry) => {
    const frames = calls.get(entry.id);
    if (!frames?.length) return entry;
    const failed = entry.status === "failed" ? failedIn(frames) : null;
    return { ...entry, frames, summary: failed ?? entry.summary };
  });
}

/** The failures of this workflow's steps their On error handled, one per entry. */
const handledCount = (response: ExecuteResponse): number =>
  response.trace.filter(
    (entry) => !entry.frame && entry.status === "failed" && !!entry.handled,
  ).length;

/** "The simulated run succeeded with 1 handled error."; null unless it did. */
export function handledSentence(response: ExecuteResponse): string | null {
  const count = handledCount(response);
  if (response.status !== "completed" || !count) return null;
  return `The simulated run succeeded with ${count.toLocaleString("en-US")} handled ${count === 1 ? "error" : "errors"}.`;
}

/** The header's words for an execution's answer. */
export function runStatus(
  response: ExecuteResponse,
  target: ExecuteTarget | null,
  called: CalledRuns | null = null,
): string {
  // A step that failed and went on ran too.
  const ran = new Set(
    response.trace
      .filter(
        (entry) =>
          !entry.frame &&
          (entry.status === "completed" ||
            (entry.status === "failed" && !!entry.handled)),
      )
      .map((entry) => entry.nodeId),
  ).size;
  const steps = `${ran} step${ran === 1 ? "" : "s"}`;
  // Short enough for one line; the question below says what to do.
  const blocked = response.status === "blocked" ? response.blocked : null;
  const inside = called?.blocked;
  if (blocked?.reason === "missing_mock" && inside?.frame) {
    const workflow = called?.frames.find((view) => view.key === inside.frame);
    const name = workflow?.workflow.slice(
      0,
      workflow.workflow.lastIndexOf("@"),
    );
    return `${inside.nodeId}${name ? ` in ${name}` : ""} needs test data`;
  }
  if (blocked?.reason === "signal")
    return `Waiting for ${blocked.message.replace(/^Send signal /, "")}`;
  if (blocked?.reason === "human") return `Waiting for ${blocked.nodeId}`;
  const undo = splitPhase(blocked?.instanceKey ?? "");
  if (blocked?.reason === "missing_mock")
    return undo
      ? `${blocked.nodeId}'s ${PHASE_NOUNS[undo.phase]} needs test output`
      : `${blocked.nodeId} needs test data`;
  if (blocked) return `Stopped at ${blocked.nodeId}`;
  if (response.status === "failed") {
    // A transaction in doubt is why it stopped, whichever step failed.
    if (transactionSummary(response)?.inDoubt) return `Stopped · ${steps}`;
    // Where it stopped, past any failure a step's On error handled; a
    // called run's step (its frame) is the call's, which the call reports.
    const failed = stoppedAt(response);
    // A loop that failed over its Up to, before any item.
    if (
      failed?.kind === "forEach" &&
      response.diagnostics?.some((problem) => problem.code === LOOP_LIMIT)
    )
      return `Failed at ${failed.nodeId}: more items than Up to`;
    // Inside a loop, the item too: "Failed at send, item 4".
    const iteration = failed ? iterationOf(failed.instanceKey) : [];
    const item = iteration.length
      ? `, item ${iteration.map((at) => at + 1).join(" › ")}`
      : "";
    return failed ? `Failed at ${failed.nodeId}${item}` : "Failed";
  }
  if (response.status === "limit") return "Stopped at the simulation's limit";
  if (target?.mode === "before") return `Paused before ${target.stepId}`;
  if (handledCount(response)) return `Succeeded with errors · ${steps}`;
  return `Completed · ${steps}`;
}

@Component({
  selector: "weave-run-log",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, NgTemplateOutlet, PhaseOutputDialog],
  styleUrl: "./run-log-panel.css",
  host: { class: "run-log", role: "region", "aria-label": "Run log" },
  template: `@let list = entries();
    <header class="run-log-head">
      <weave-icon name="runHistory" [size]="16" aria-hidden="true" />
      <h2 class="run-log-title">Run log</h2>
      <!-- A long status takes its own line under the title, whole. -->
      <!-- "Succeeded with errors" reads in the warning tone, on the title's
           line: it shortens last, and its tooltip says it whole. -->
      <span
        class="run-log-status"
        role="status"
        [class.run-log-status-long]="status().length > 26 && !handledText()"
        [attr.data-tone]="handledText() ? 'warning' : null"
        [title]="status()"
        >{{ status() }}</span
      >
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
    @if (handledText(); as said) {
      <!-- First: the run went on past failures its steps handled. -->
      <p class="run-log-outcome">
        <weave-icon name="degraded" [size]="16" aria-hidden="true" />{{ said }}
      </p>
    }
    @if (outcome(); as said) {
      <div class="run-log-outcome" data-tone="danger">
        <weave-icon
          [name]="said.inDoubt ? 'critical' : 'failCircle'"
          [size]="16"
          aria-hidden="true"
        />
        <div>
          <p>{{ said.text }}</p>
          @if (said.detail) {
            <p class="run-log-outcome-detail">{{ said.detail }}</p>
          }
        </div>
      </div>
    }
    @if (!host().runSession()) {
      @let sampled = !!host().execution?.sampledInput();
      @let ran = names();
      @if (sampled || ran.length) {
        <!-- Each workflow's name stays whole on a narrow sheet. -->
        <p class="run-log-note">
          {{
            sampled
              ? "Executed with a sample input. Add test input to choose your own."
              : "Executed with test input."
          }}
          @for (name of ran; track name; let at = $index, count = $count) {
            <span class="run-log-name">{{ name }}</span
            >{{ at < count - 2 ? "," : at === count - 2 ? " and" : "" }}
          }
          @if (ran.length) {
            ran on this computer.
          }
        </p>
      }
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
    @if (host().executeRefusal?.(); as refused) {
      <!-- Nothing ran: what stopped it, in place of the last run's steps. -->
      <p class="run-log-refusal" data-tone="warning">
        <weave-icon name="warning" [size]="16" aria-hidden="true" />{{
          refused
        }}
      </p>
    } @else if (list.length) {
      <ul class="run-log-list" aria-label="Steps that ran">
        @for (entry of list; track entry.id) {
          @if (entry.path; as path) {
            @if (path.first) {
              <!-- Each step's own name says it is in the error path. -->
              <li
                class="run-log-path"
                aria-hidden="true"
                [style.--path-depth]="path.owners.length"
              >
                <weave-icon name="warning" [size]="16" />Error path
              </li>
            }
          }
          <li
            class="run-log-row"
            [class.run-log-in-path]="!!entry.path"
            [style.--path-depth]="entry.path ? entry.path.owners.length : null"
          >
            <button
              type="button"
              class="run-log-entry"
              [attr.data-entry]="entry.id"
              [attr.data-status]="entry.status"
              [attr.aria-label]="label(entry)"
              [attr.aria-describedby]="
                entry.reason ? reasonId + '-' + entry.id : null
              "
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
              @if (entry.code) {
                <code class="run-log-handled-code" [title]="entry.code">{{
                  entry.code
                }}</code>
              }
            </button>
            @if (entry.reason; as reason) {
              <p class="run-log-stop-reason" [id]="reasonId + '-' + entry.id">
                {{ reason }}
              </p>
            }
            @if (entry.loop?.message; as message) {
              <p class="run-log-loop-message">{{ message }}</p>
            }
            @if (entry.loop; as loop) {
              <ul
                class="run-log-items"
                [attr.aria-label]="'Items of ' + entry.id"
              >
                @for (item of loop.items; track item.label) {
                  <li class="run-log-row">
                    <!-- Opens its step on this item. -->
                    <button
                      type="button"
                      class="run-log-entry run-log-item"
                      [attr.data-status]="item.status"
                      [attr.aria-label]="
                        item.label +
                        ': ' +
                        item.text +
                        (item.code ? ', ' + item.code : '')
                      "
                      (click)="openItem(item.key)"
                    >
                      <weave-icon
                        class="run-log-icon"
                        [name]="icons[item.status]"
                        [size]="16"
                        aria-hidden="true"
                      />
                      <strong class="run-log-step">{{ item.label }}</strong>
                      <span class="run-log-summary">{{ item.text }}</span>
                      @if (item.code) {
                        <code
                          class="run-log-handled-code"
                          [title]="item.code"
                          >{{ item.code }}</code
                        >
                      }
                    </button>
                    <!-- The failed item shows open: each step it went through, then the error. -->
                    @if (item.steps; as steps) {
                      <ul
                        class="run-log-item-steps"
                        [attr.aria-label]="'Steps of ' + item.label"
                      >
                        @for (step of steps; track step.key) {
                          <li>
                            <button
                              type="button"
                              class="run-log-entry run-log-item-step"
                              [attr.data-status]="step.status"
                              [attr.aria-label]="
                                step.id +
                                (step.status === 'failed' ? ', failed' : '') +
                                (step.text ? ': ' + step.text : '')
                              "
                              (click)="openItem(step.key)"
                            >
                              <weave-icon
                                class="run-log-icon"
                                [name]="icons[step.status]"
                                [size]="16"
                                aria-hidden="true"
                              />
                              <strong class="run-log-step">{{
                                step.id
                              }}</strong>
                              <span
                                class="run-log-summary"
                                [class.run-log-code]="step.status === 'failed'"
                                >{{ step.text ?? "" }}</span
                              >
                            </button>
                          </li>
                        }
                      </ul>
                      @if (item.message) {
                        <p class="run-log-item-message">{{ item.message }}</p>
                      }
                    }
                  </li>
                }
                @for (note of loop.notRun; track note) {
                  <li class="run-log-not-run">{{ note }}</li>
                }
              </ul>
            }
            @if (entry.frames) {
              <ng-container
                [ngTemplateOutlet]="framesOf"
                [ngTemplateOutletContext]="{ $implicit: entry.frames }"
              />
            }
          </li>
          @for (one of entry.transactions ?? []; track one.key) {
            <!-- After the step it followed: what was undone, in the order it ran. -->
            <li
              class="run-log-row"
              [class.run-log-in-path]="!!entry.path"
              [style.--path-depth]="
                entry.path ? entry.path.owners.length : null
              "
            >
              <button
                type="button"
                class="run-log-entry run-log-transaction"
                [attr.data-transaction]="one.key"
                [attr.data-tone]="one.tone"
                [attr.aria-label]="one.label"
                (click)="openItem(one.key)"
              >
                <weave-icon
                  class="run-log-icon"
                  [name]="one.icon"
                  [size]="16"
                  aria-hidden="true"
                />
                <strong class="run-log-step">{{ one.title }}</strong>
                <span class="run-log-summary">{{ one.text }}</span>
              </button>
              @if (one.steps.length) {
                <ul class="run-log-phases" [attr.aria-label]="one.title">
                  @for (step of one.steps; track step.title) {
                    <li>
                      <button
                        type="button"
                        class="run-log-entry run-log-phase"
                        [attr.data-phase]="step.key"
                        [attr.data-tone]="step.tone"
                        [attr.aria-label]="step.label"
                        (click)="openItem(step.key)"
                      >
                        <weave-icon
                          class="run-log-icon"
                          [name]="step.icon"
                          [size]="16"
                          aria-hidden="true"
                        />
                        <strong class="run-log-step">{{ step.title }}</strong>
                        <span class="run-log-summary">{{ step.text }}</span>
                      </button>
                    </li>
                  }
                </ul>
              }
            </li>
          }
        }
      </ul>
    } @else {
      <p class="run-log-empty">
        Execute the workflow to see each step it runs here.
      </p>
    }
    <!-- The workflows a step called: a row that opens to their steps, and the calls inside them under their own step. -->
    <ng-template #framesOf let-frames>
      <ul class="run-log-frames">
        @for (frame of asFrames(frames); track frame.key) {
          @let open = isOpen(frame);
          <li class="run-log-frame">
            <button
              type="button"
              class="run-log-frame-row"
              [attr.data-frame]="frame.key"
              [attr.data-status]="frame.status"
              [attr.aria-expanded]="open"
              [attr.aria-label]="frameLabel(frame)"
              [title]="frame.name + ' ' + frame.version"
              (click)="toggle(frame)"
              (keydown)="frameKey($event, frame)"
            >
              <weave-icon
                class="run-log-chevron"
                [name]="open ? 'chevronDown' : 'chevron'"
                [size]="16"
                aria-hidden="true"
              />
              <weave-icon
                class="run-log-icon"
                [name]="icons[frame.status]"
                [size]="16"
                aria-hidden="true"
              />
              <strong class="run-log-frame-name">{{ frame.name }}</strong>
              <span class="run-log-frame-version">{{ frame.version }}</span>
              @if (frame.item) {
                <span class="run-log-frame-item">{{ frame.item }}</span>
              }
              @if (frame.status !== "completed") {
                <span class="run-log-frame-status">{{
                  frameWords[frame.status]
                }}</span>
              }
            </button>
            @if (open) {
              <ul
                class="run-log-frame-steps"
                [attr.aria-label]="'Steps of ' + frame.name"
                (keydown)="stepsKey($event, frame)"
              >
                @for (step of frame.steps; track step.id) {
                  <li class="run-log-frame-step">
                    <div
                      class="run-log-entry run-log-inside"
                      [attr.data-status]="
                        step.needsData ? 'needs' : step.status
                      "
                    >
                      <weave-icon
                        class="run-log-icon"
                        [name]="step.needsData ? 'warning' : icons[step.status]"
                        [size]="16"
                        aria-hidden="true"
                      />
                      <strong class="run-log-step">{{ step.id }}</strong>
                      @let said =
                        step.status === "failed"
                          ? "Failed"
                          : step.needsData
                            ? ""
                            : (step.summary ?? "");
                      <span class="run-log-summary" [title]="said">{{
                        said
                      }}</span>
                      @if (settable(step)) {
                        <button
                          type="button"
                          class="sm run-log-set"
                          [class.shown]="step.needsData"
                          [attr.data-set]="frame.key + '/' + step.id"
                          [attr.aria-label]="
                            'Set test output for ' +
                            step.id +
                            ' in ' +
                            frame.name
                          "
                          (click)="startEdit(frame, step)"
                        >
                          <weave-icon
                            name="pin"
                            [size]="16"
                            aria-hidden="true"
                          />
                          Set test output
                        </button>
                      }
                    </div>
                    @if (step.needsData) {
                      <p class="run-log-inside-note">Needs test data</p>
                    }
                    @if (
                      editing()?.frame === frame.key &&
                      editing()?.step === step.id
                    ) {
                      <form
                        class="run-log-question run-log-set-form"
                        (submit)="saveOutput($event, frame, step)"
                        (keydown.escape)="cancelEdit(frame, step, $event)"
                      >
                        <label class="run-log-json-label" [for]="setId"
                          >Test output for {{ step.id }} (JSON)</label
                        >
                        <textarea
                          class="param-input run-log-json"
                          [id]="setId"
                          rows="4"
                          spellcheck="false"
                          [value]="setJson()"
                          (input)="setJson.set($any($event.target).value)"
                        ></textarea>
                        @if (setProblem()) {
                          <p class="pane-problem" role="alert">
                            {{ setProblem() }}
                          </p>
                        }
                        <div class="run-log-set-actions">
                          <button type="submit" class="primary sm">Save</button>
                          <button
                            type="button"
                            class="sm"
                            (click)="cancelEdit(frame, step)"
                          >
                            Cancel
                          </button>
                        </div>
                      </form>
                    }
                    @if (step.frames.length) {
                      <ng-container
                        [ngTemplateOutlet]="framesOf"
                        [ngTemplateOutletContext]="{ $implicit: step.frames }"
                      />
                    }
                  </li>
                }
              </ul>
            }
          </li>
        }
      </ul>
    </ng-template>
    @if (phaseAsked(); as asked) {
      <weave-phase-output-dialog
        [block]="asked"
        [icon]="iconOf(asked.stepId)"
        (closed)="phaseAnswered($event)"
      />
    }`,
})
export class RunLogPanel {
  readonly host = input.required<RunLogHost>();
  readonly icons = STATUS_ICONS;
  readonly jsonId = `run-log-json-${Math.random().toString(36).slice(2, 8)}`;
  readonly json = signal("{}");
  readonly chosen = signal("");
  readonly problem = signal("");
  private readonly el: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly injector = inject(Injector);

  constructor() {
    // Below 1,600 px wide the run log takes the Workflow panel's room, so the
    // canvas keeps enough to show the run; the panel comes back when it closes.
    let madeRoom: RunLogHost | null = null;
    afterNextRender(() => {
      const host = this.host();
      if (host.windowWidth >= 1600 || !host.showInspector) return;
      host.showInspector = false;
      madeRoom = host;
      host.refreshView();
    });
    inject(DestroyRef).onDestroy(() => {
      if (!madeRoom) return;
      madeRoom.showInspector = true;
      madeRoom.refreshView();
    });
    // Step details ask for themselves what they executed stopped at.
    effect(() => {
      const host = this.host();
      const last = host.execution?.last();
      const shown = !!this.details.request() || !!host.runSession();
      untracked(() => {
        const block =
          last && !shown
            ? phaseBlock(host.model.definition, last.blocked)
            : null;
        if (!block || askOnce(last!)) this.phaseAsked.set(block);
      });
    });
  }
  private readonly details = inject(StepDetailsService);
  /** "Set test output to execute", while it is asked of an undo step. */
  readonly phaseAsked = signal<PhaseBlock | null>(null);
  /** Set test output opens the undo step's editor, on its step; Cancel goes back. */
  phaseAnswered(set: boolean) {
    const asked = this.phaseAsked();
    this.phaseAsked.set(null);
    if (!set || !asked) return;
    const { stepId, phase } = asked;
    void this.host().openStepDetails?.(stepId, {
      focus: { kind: "testOutput", phase },
    });
  }
  iconOf(stepId: string): string {
    const step = this.steps().find(({ id }) => id === stepId);
    return ndvRegistry.kind(step?.kind ?? "")?.icon ?? "action";
  }
  /** A row for one item of a loop (`send[2]`) opens its step on that item. */
  openItem(key: string): void {
    const host = this.host();
    const stepId = instanceStep(key);
    if (stepId !== key) host.openedItem?.set({ draftId: host.draftId, key });
    host.openEntry(stepId);
  }

  private steps(): Step[] {
    return allSteps(this.host().model.definition.spec.steps ?? []);
  }
  ended(session: HostSession): boolean {
    const status = String(session.view["status"] ?? "");
    return ["succeeded", "failed", "cancelled", "timed_out"].includes(status);
  }
  status(): string {
    const host = this.host();
    if (host.executeRefusal?.()) return "Didn't start";
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
    return last
      ? runStatus(last, host.lastTarget, this.called())
      : "Nothing ran yet";
  }
  /** "The simulated run succeeded with 1 handled error."; null unless the last execution did. */
  handledText(): string | null {
    const host = this.host();
    if (host.runSession() || host.executeRefusal?.()) return null;
    const last = host.execution?.last();
    return last ? handledSentence(last) : null;
  }
  /** The moment its playback is at, until it ended. */
  private playing(): CanvasRun | null {
    const run = this.host().playback?.() ?? null;
    return run && !run.ended ? run : null;
  }
  /** "The transaction move is in doubt, so the simulated run stopped.", once the playback ended. */
  outcome() {
    const host = this.host();
    if (host.runSession() || host.executeRefusal?.() || this.playing())
      return null;
    const last = host.execution?.last();
    return last ? transactionSummary(last) : null;
  }
  /** A row for each step that ran, in the order they did. */
  private readonly ran = computed((): RunLogEntry[] => {
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
    // The steps of an error path follow their step's row, further in.
    const paths = errorPathsAround(host.model.definition.spec.steps ?? []);
    const loops = loopLog(last, around, host.model.definition);
    const stopped = stoppedRow(last, host.model.definition);
    // Steps a transaction that went on was undone for: their codes.
    const undone = undoneFor(last);
    const seen = new Map<string, RunLogEntry>();
    for (const entry of last.trace) {
      if (entry.frame || around(entry.nodeId).length) continue;
      const runs = loops.filter(
        (loop) =>
          loop.id === entry.nodeId || around(loop.id)[0] === entry.nodeId,
      );
      const own = runs.find((loop) => loop.id === entry.nodeId);
      const handled = entry.status === "failed" && !!entry.handled;
      const owners = paths(entry.nodeId);
      seen.set(entry.nodeId, {
        id: entry.nodeId,
        status: handled
          ? "handled"
          : own && entry.status !== "failed"
            ? own.items.some((item) => item.status === "failed")
              ? "failed"
              : entry.status
            : entry.status,
        summary: handled
          ? "Failed, continued"
          : own
            ? own.summary
            : entry.status === "completed"
              ? host.summaryOf(entry.nodeId)
              : null,
        stale: stale.has(entry.instanceKey || entry.nodeId),
        breakpoint: marks.has(entry.nodeId),
        ...(handled && entry.error
          ? { code: failureCodeText(entry.error.code) }
          : {}),
        ...(!own && undone.has(entry.nodeId)
          ? { code: undone.get(entry.nodeId)! }
          : {}),
        // Where the run stopped: its code, and why its On error didn't go on.
        ...(!own && stopped?.step === entry.nodeId && entry.status === "failed"
          ? {
              code: stopped.code,
              ...(stopped.reason ? { reason: stopped.reason } : {}),
            }
          : {}),
        ...(owners.length ? { path: { owners, first: false } } : {}),
        ...(own
          ? {
              loop: {
                progress: own.progress,
                items: runs.flatMap((loop) => loop.items),
                notRun: runs.flatMap((loop) =>
                  loop.notRun ? [loop.notRun] : [],
                ),
                ...(own.message ? { message: own.message } : {}),
              },
            }
          : {}),
      });
    }
    return [...seen.values()];
  });
  readonly entries = computed((): RunLogEntry[] => {
    const host = this.host();
    let rows = this.ran();
    const last = host.execution?.last();
    if (host.runSession() || !last) return rows;
    if (last.transactions?.length) {
      // While it plays back, each undo step as it plays; then how it ended.
      const playing = this.playing();
      rows = withTransactions(
        rows,
        last,
        playing?.transactions ?? simulatedRollback(last, host.model.definition),
        !!playing || last.status === "blocked",
        this.loopsAround(),
      );
    }
    // "Error path" heads the first row of each path, under its step.
    const joined = (owners: readonly string[]) => owners.join("/") + "/";
    const listed = rows.map((row, at) => {
      const before = joined(rows[at - 1]?.path?.owners ?? []);
      // Back from a path inside this one: no second heading.
      return row.path && !before.startsWith(joined(row.path.owners))
        ? { ...row, path: { ...row.path, first: true } }
        : row;
    });
    return withFrames(listed, this.calls());
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

  // Called workflows: what ran inside them, opened and closed in place.
  readonly frameWords: Record<TraceStatus, string> = {
    completed: "Completed",
    failed: "Failed",
    waiting: "Waiting",
    running: "Running",
  };
  readonly setId = `run-log-set-${Math.random().toString(36).slice(2, 8)}`;
  readonly reasonId = `run-log-reason-${Math.random().toString(36).slice(2, 8)}`;
  readonly setJson = signal("{}");
  readonly setProblem = signal("");
  /** The step inside a called workflow whose test output is being set. */
  readonly editing = signal<{ frame: FrameKey; step: string } | null>(null);
  /** Called workflows opened or closed by hand, for the answer they belong to. */
  private readonly opened = signal<{
    called: CalledRuns | null;
    open: ReadonlyMap<FrameKey, boolean>;
  }>({ called: null, open: new Map() });
  private called(): CalledRuns | null {
    return this.host().execution?.called() ?? null;
  }
  readonly calls = computed(() => frameLog(this.called()));
  readonly names = computed(() => calledNames(this.called()));
  /** The template's context value, typed. */
  asFrames(frames: unknown): FrameLog[] {
    return frames as FrameLog[];
  }
  isOpen(frame: FrameLog): boolean {
    const kept = this.opened();
    return kept.called === this.called() && kept.open.has(frame.key)
      ? kept.open.get(frame.key)!
      : opensFirst(frame);
  }
  toggle(frame: FrameLog, open = !this.isOpen(frame)) {
    const called = this.called();
    const kept = this.opened();
    const next = new Map(kept.called === called ? kept.open : []);
    next.set(frame.key, open);
    this.opened.set({ called, open: next });
  }
  /** "notify-customer 1.0.0, item 3, failed": the row says whether it is open (aria-expanded). */
  frameLabel(frame: FrameLog): string {
    return [
      `${frame.name} ${frame.version}`.trim(),
      frame.item?.toLowerCase(),
      frame.status,
    ]
      .filter(Boolean)
      .join(", ");
  }
  /** → opens a called workflow's row, ← closes it; Enter and Space are the button's own. */
  frameKey(event: KeyboardEvent, frame: FrameLog) {
    const open = this.isOpen(frame);
    if (event.key === "ArrowRight" && !open) this.toggle(frame, true);
    else if (event.key === "ArrowLeft" && open) this.toggle(frame, false);
    else return;
    event.preventDefault();
  }
  /** Escape inside a called workflow's steps closes it, and focus goes back to its row. */
  stepsKey(event: KeyboardEvent, frame: FrameLog) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    this.focusOn(`[data-frame="${CSS.escape(frame.key)}"]`);
    this.toggle(frame, false);
  }
  private focusOn(selector: string, later = false) {
    const focus = () =>
      this.el.nativeElement.querySelector<HTMLElement>(selector)?.focus();
    if (later) afterNextRender(focus, { injector: this.injector });
    else focus();
  }
  readonly settable = takesTestOutput;
  startEdit(frame: FrameLog, step: FrameStep) {
    const kept =
      this.host().testData?.data().workflows?.[frame.workflow]?.mocks[step.id]
        ?.output ??
      step.output ??
      {};
    this.setJson.set(JSON.stringify(kept, null, 2));
    this.setProblem.set("");
    this.editing.set({ frame: frame.key, step: step.id });
    this.focusOn(`#${this.setId}`, true);
  }
  cancelEdit(frame: FrameLog, step: FrameStep, event?: Event) {
    event?.preventDefault();
    event?.stopPropagation();
    this.editing.set(null);
    this.focusOn(`[data-set="${CSS.escape(`${frame.key}/${step.id}`)}"]`, true);
  }
  async saveOutput(event: Event, frame: FrameLog, step: FrameStep) {
    event.preventDefault();
    let value: Json;
    try {
      value = JSON.parse(this.setJson() || "{}") as Json;
    } catch {
      this.setProblem.set(
        "This isn't JSON yet. Check the brackets and quotes.",
      );
      return;
    }
    this.setProblem.set("");
    // Under the called workflow's test data; the last execution runs again when it stopped here.
    const saved = await this.host().keepForRunLog(
      (data, now) => withCalleeMock(data, frame.workflow, step.id, value, now),
      segment("workflows") +
        segment(frame.workflow) +
        segment("mocks") +
        segment(step.id),
      step.needsData,
    );
    if (!saved) return;
    this.editing.set(null);
    this.focusOn(`[data-frame="${CSS.escape(frame.key)}"]`, true);
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
    // Kept as the step's test answer; the last execution runs again, so the next one doesn't ask.
    const section = asked.reason === "signal" ? "signals" : "humans";
    const entry: SignalEntry | HumanEntry =
      asked.reason === "signal"
        ? { payload: value, source: "manual" }
        : { decision: this.chosen(), data: value, source: "manual" };
    const saved = await this.host().keepForRunLog(
      (data, now) => withScript(data, section, asked.nodeId, entry, now),
      segment(section) + segment(asked.nodeId),
      true,
    );
    if (saved) this.json.set("{}");
  }
  label(entry: RunLogEntry): string {
    const words: Record<RunLogEntry["status"], string> = {
      completed: "finished",
      failed: "failed",
      handled: "failed and continued",
      waiting: "waiting",
      running: "reached",
      current: "paused here",
    };
    const owner = entry.path?.owners.at(-1);
    return [
      entry.id,
      owner ? `in the error path of ${owner}` : "",
      words[entry.status],
      // A handled or stopping step's code; a step that ran, its summary.
      entry.code ?? (entry.status === "handled" ? "" : entry.summary),
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
