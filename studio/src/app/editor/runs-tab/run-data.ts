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
// One run's data while it is open in the Runs tab: the outputs the platform
// recorded for each step instance (or why it left one out), the step
// instances it ran, and what a step received, recomputed from the run's
// data with the version the run used. Kept in memory only.
import { signal } from "@angular/core";
import { shortId } from "../../format";
import type { Workflow } from "../../model";
import { stepOutput, type StepFact } from "../../operate/run-contracts";
import { evaluate } from "../formula/evaluate";
import { referencesOf } from "../formula/preview";
import { findStep } from "../ndv/step-walk";
import { instanceStep } from "../state/instance-keys";
import type { RecordedOutput, Received, RunDataPort } from "./run-port";
import type { RunRecord } from "./run-record";

type Json = unknown;
type StepMark =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "too-large" };

const NO_VERSION =
  "Studio couldn't read the version this run used, so it can't recompute the input.";
const ONE_ITERATION = "Studio can't recompute one iteration's input yet.";
const LEFT_OUT =
  "This input reads data the platform left out of the run, so Studio can't recompute it.";
const LOOP_ROOTS = new Set(["item", "index", "loops"]);
const AT_WORK = new Set<StepFact["status"]>([
  "scheduled",
  "running",
  "waiting",
]);
const FAILED = new Set<StepFact["status"]>(["failed", "timed_out"]);

const keyOf = (fact: StepFact) => fact.instance_key || fact.node_id;
/** The first two segments of a JSON pointer: "/steps/fetch/output" gives ["steps", "fetch"]. */
const head = (pointer: string) => pointer.split("/").slice(1, 3);

export class RunData implements RunDataPort {
  readonly runId: string;
  /** Changes whenever what the run's data says changes, so views redraw. */
  readonly version = signal(0);
  /** Instance key to the step instance's facts, in the order they were read. */
  private readonly facts = new Map<string, StepFact>();
  /** Instance key to the output read with include=output. */
  private readonly outputs = new Map<string, RecordedOutput>();
  /** Step ID to a read in progress, failed or too large. */
  private readonly marks = new Map<string, StepMark>();

  constructor(
    private record: RunRecord,
    readonly environmentId: string,
    private readonly definition: Workflow | null,
  ) {
    this.runId = record.id;
  }

  /** The run read again while it is followed; outputs already read stay. */
  setRecord(record: RunRecord): void {
    if (record.id !== this.runId) return;
    this.record = record;
    this.changed();
  }

  /** Step instances read with their outputs (include=output); `stepId` names the step read. */
  setOutputs(facts: readonly StepFact[], stepId?: string): void {
    if (stepId !== undefined) this.marks.delete(stepId);
    for (const fact of facts) {
      this.marks.delete(fact.node_id);
      this.facts.set(keyOf(fact), fact);
      const output = stepOutput(fact);
      this.outputs.set(
        keyOf(fact),
        output.kind === "none"
          ? {
              kind: "none",
              why: AT_WORK.has(fact.status) ? "running" : "not-recorded",
            }
          : output,
      );
    }
    this.changed();
  }
  /** Step instances read without outputs: status, timing and the runs they called. */
  setFacts(facts: readonly StepFact[]): void {
    for (const fact of facts) this.facts.set(keyOf(fact), fact);
    this.changed();
  }
  markLoading(stepId: string): void {
    this.mark(stepId, { kind: "loading" });
  }
  markError(stepId: string, message: string): void {
    this.mark(stepId, { kind: "error", message });
  }
  markTooLarge(stepId: string): void {
    this.mark(stepId, { kind: "too-large" });
  }

  input(): Json {
    return this.record.input;
  }

  output(key: string): RecordedOutput {
    const read = this.outputs.get(key);
    if (read) return read;
    const mark = this.marks.get(instanceStep(key));
    if (mark) return mark;
    const fact = this.facts.get(key);
    if (
      (fact && AT_WORK.has(fact.status)) ||
      this.record.active.includes(key) ||
      this.record.waiting.includes(key)
    )
      return { kind: "none", why: "running" };
    if (fact || key in this.record.completed) return { kind: "loading" };
    return { kind: "none", why: "not-reached" };
  }

  received(stepId: string): Received {
    if (!this.definition) return { ok: false, reason: NO_VERSION };
    const step = findStep(this.definition.spec.steps, (s) => s.id === stepId);
    if (!step || step["with"] === undefined) return { ok: false, reason: "" };
    const reads = referencesOf(step["with"]).map(head);
    if (reads.some(([root]) => LOOP_ROOTS.has(root)))
      return { ok: false, reason: ONE_ITERATION };
    const steps: Record<string, { output: Json }> = {};
    const leftOut = new Set<string>();
    for (const [key, output] of Object.entries(this.record.completed))
      steps[instanceStep(key)] = { output };
    // What was read with include=output is the platform's classified record.
    for (const [key, output] of this.outputs) {
      const id = instanceStep(key);
      if (output.kind === "value") {
        steps[id] = { output: output.value };
        leftOut.delete(id);
      } else if (output.kind === "omitted") {
        delete steps[id];
        leftOut.add(id);
      }
    }
    if (reads.some(([root, id]) => root === "steps" && leftOut.has(id)))
      return { ok: false, reason: LEFT_OUT };
    const result = evaluate(step["with"], {
      input: this.record.input,
      steps,
    });
    return result.ok
      ? { ok: true, value: result.value }
      : {
          ok: false,
          reason: `Studio couldn't recompute this input: ${result.code}.`,
        };
  }

  iterations(stepId: string): readonly string[] {
    const instances = [...this.facts.values()].filter(
      (fact) => fact.node_id === stepId,
    );
    const failed = (fact: StepFact) => (FAILED.has(fact.status) ? 0 : 1);
    return instances
      .sort((a, b) => failed(a) - failed(b) || byIteration(a, b))
      .map(keyOf);
  }

  failure(stepId: string): string | null {
    const incident = this.record.incidents.find(
      ({ key }) => instanceStep(key) === stepId,
    );
    if (incident) return incident.code;
    for (const fact of this.facts.values())
      if (fact.node_id === stepId && FAILED.has(fact.status))
        return fact.error_code ?? fact.status;
    return null;
  }

  childRun(key: string): string | null {
    return this.facts.get(key)?.child_run_id ?? null;
  }

  label(): string {
    return `run ${shortId(this.runId)}`;
  }

  private mark(stepId: string, mark: StepMark) {
    this.marks.set(stepId, mark);
    this.changed();
  }
  private changed() {
    this.version.update((version) => version + 1);
  }
}

/** Loop indexes in order, outermost first; then the key. */
function byIteration(a: StepFact, b: StepFact): number {
  const length = Math.max(a.iteration.length, b.iteration.length);
  for (let i = 0; i < length; i++) {
    const delta = (a.iteration[i] ?? -1) - (b.iteration[i] ?? -1);
    if (delta) return delta;
  }
  return keyOf(a).localeCompare(keyOf(b));
}
