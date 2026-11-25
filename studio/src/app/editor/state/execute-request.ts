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
// What Execute step, Execute previous steps and Execute workflow send to the
// Studio host: the steps a target needs (the host's own slice, mirrored), and
// the request built from test data and the last results, with the workflows
// it calls (a draft on this computer goes with its source, so it runs here
// too). DOM-free.
import type { Step, Workflow } from "../../model";
import { calledReferenceSites } from "../activation/callee-pins";
import { simulateBlock } from "../ndv/panes/simulate-failure";
import { blockSteps } from "../ndv/step-walk";
import { blocksOf } from "../step-blocks";
import { splitPhase } from "../transactions/phase-keys";
import { phaseOperation } from "../transactions/phases";
import { undoChoiceOf } from "../transactions/points";
import type { Json } from "../ndv/registry";
import type {
  Blocked,
  ExecuteRequest,
  ExecuteResponse,
  ExecuteTarget,
  FrameView,
  MockKey,
  ScriptedFailure,
  TraceEntry,
} from "./execution";
import { instanceStep } from "./instance-keys";
import type { LastResults } from "./last-results";
import type { StudioTestData } from "./test-data";

type JsonObject = { [key: string]: Json };

/** The step lists a step holds, as the host's slicing walks them (a transaction's own steps and error paths included). */
const branchesOf = (step: Step): Step[][] => blocksOf(step).map(blockSteps);

/**
 * The IDs of the steps the host keeps to execute `stepId` (the target with
 * what it holds, what runs before it in its sequence and every enclosing one,
 * and the groups holding it), in document order; null when the workflow has
 * no such step. Branches the host empties run nothing, so they aren't listed.
 */
export function sliceStepIds(
  workflow: Workflow,
  stepId: string,
): string[] | null {
  const cut = (steps: Step[]): string[] | null => {
    const kept: string[] = [];
    for (const step of steps) {
      // A group as the target runs all of itself.
      if (step.id === stepId) return [...kept, step.id, ...everyId(step)];
      for (const branch of branchesOf(step)) {
        const inner = cut(branch);
        if (inner) return [...kept, step.id, ...inner];
      }
      kept.push(step.id, ...everyId(step));
    }
    return null;
  };
  return cut(workflow.spec.steps ?? []);
}

/** The IDs of every step a step holds, depth-first. */
function everyId(step: Step): string[] {
  return branchesOf(step).flatMap((branch) =>
    branch.flatMap((inner) => [inner.id, ...everyId(inner)]),
  );
}

/** Kinds whose output a mock stands for in a simulation (the rest compute theirs). */
export const MOCKED_KINDS: ReadonlySet<string> = new Set(["action", "llm"]);
const isCall = (step: Step | undefined) =>
  (step?.kind as string) === "callWorkflow" && step?.["mode"] !== "detach";
/** A pin stands in for these steps; a waiting call's too. */
const mocked = (step: Step | undefined): boolean =>
  !!step && (MOCKED_KINDS.has(step.kind) || isCall(step));
/** A last result stands in for these: not a call, whose called workflow runs here again. */
const replayed = (step: Step | undefined): boolean =>
  !!step && MOCKED_KINDS.has(step.kind);

/** The most called workflows the host runs in one test. */
export const CALLEE_LIMIT = 50;
/** A draft on this computer that the workflow calls, as the host compiles it. */
export interface LocalSource {
  source: string;
  format: "yaml" | "json";
}
/** The workflows an execution reaches: every called reference, and the drafts here among them. */
export interface CalledWorkflows {
  sources: Readonly<Record<string, LocalSource>>;
  references: readonly string[];
}
/** Why the request can't go: it would run more called workflows than the host takes; null when it fits. */
export function tooManyCallees(request: ExecuteRequest): string | null {
  const count = Object.keys(request.workflows ?? {}).length;
  return count > CALLEE_LIMIT
    ? `This test would run ${count} called workflows. Studio can run at most ${CALLEE_LIMIT} in one test.`
    : null;
}

export interface ExecuteParts {
  source: string;
  format: "yaml" | "json";
  workflow: Workflow;
  /** Documents of the actions this workflow owns, and local definitions it uses. */
  definitions: JsonObject[];
  connected: boolean;
  testData: StudioTestData;
  results: LastResults | null;
  /** The workflow input to use when test data has none (a sample of its fields). */
  sampleInput: Json;
  /** The workflows it reaches and the drafts here among them; without it, the ones it calls directly. */
  callees?: CalledWorkflows;
  target: ExecuteTarget;
  now: Date;
}

/**
 * The host request for one execution. A step's mock is its pin (under its
 * instance key, then its step ID), otherwise its last result, however old;
 * scripts and AI turns come from test data. Nothing else is sent.
 */
export function executeRequest(parts: ExecuteParts): ExecuteRequest {
  const { testData, results, workflow } = parts;
  const steps = new Map<string, Step>();
  /** The steps a transaction holds. */
  const inside = new Set<string>();
  const visit = (list: Step[], held: boolean) => {
    for (const step of list) {
      steps.set(step.id, step);
      if (held) inside.add(step.id);
      for (const block of blocksOf(step))
        visit(
          blockSteps(block),
          held ||
            ((step.kind as string) === "transaction" && block.kind === "body"),
        );
    }
  };
  visit(workflow.spec.steps ?? [], false);
  /** The step a key's undo step belongs to while it has it; null for any other key. */
  const undoes = (key: string): Step | null => {
    const named = splitPhase(key);
    const step = named && steps.get(instanceStep(key));
    return step && inside.has(step.id) && phaseOperation(step, named.phase)
      ? step
      : null;
  };
  // Executing one step sends only the test data of the steps it runs.
  const runs =
    parts.target.mode === "workflow" || !parts.target.stepId
      ? null
      : new Set(sliceStepIds(workflow, parts.target.stepId) ?? []);
  const sent = (key: string) => !runs || runs.has(instanceStep(key));
  // A simulated failure takes its key, or every item of its step: the host
  // takes a key as a failure or a mock, never both, so no pin, replayed
  // result or AI turns goes there. A failure of a step that can't take one
  // any more (its On error went back to Stop the run, a call that no longer
  // waits, an undo step it no longer has) or that left the draft stays in
  // test data and isn't sent: the host would refuse the whole request for it.
  const failures = Object.fromEntries(
    Object.entries(testData.errors ?? {}).filter(([key]) => {
      if (splitPhase(key)) return !!undoes(key);
      const step = steps.get(instanceStep(key));
      return !!step && simulateBlock(step, false, inside.has(step.id)) === null;
    }),
  );
  const fails = (key: string) =>
    Object.hasOwn(failures, key) || Object.hasOwn(failures, instanceStep(key));
  const errors: Record<MockKey, ScriptedFailure> = {};
  for (const [key, entry] of Object.entries(failures)) {
    if (!sent(key)) continue;
    const { code, outcome, attempts } = entry;
    const step = steps.get(instanceStep(key))!;
    // What a failure changed is for a step of a transaction that is undone or
    // can't be; the host refuses it anywhere else. Set before the step was
    // in one, an unknown outcome says so itself: the host would go by the code.
    const effect =
      splitPhase(key) || !inside.has(step.id) || !undoChoiceOf(step)
        ? undefined
        : (entry.effect ?? (outcome === "unknown" ? "unknown" : undefined));
    errors[`node:${key}`] = {
      code,
      ...(outcome === undefined ? {} : { outcome }),
      ...(attempts === undefined ? {} : { attempts }),
      ...(effect === undefined ? {} : { effect }),
    };
  }
  const mocks: Record<MockKey, Json> = {};
  const pins = testData.pins ?? {};
  // A pin gives way only to a failure at its own key: an item's pin under
  // its step's failure for every item is the more specific key, and the
  // host takes it for that item. An undo step's pin stands for its result
  // alone, while the step has it.
  for (const [key, pin] of Object.entries(pins))
    if (
      sent(key) &&
      !Object.hasOwn(failures, key) &&
      (splitPhase(key) ? !!undoes(key) : mocked(steps.get(instanceStep(key))))
    )
      mocks[`node:${key}`] = pin.output;
  for (const [key, result] of Object.entries(results?.steps ?? {})) {
    const step = instanceStep(key);
    if (
      result.output === undefined ||
      !sent(key) ||
      fails(key) ||
      !replayed(steps.get(step)) ||
      Object.hasOwn(pins, key) ||
      Object.hasOwn(pins, step)
    )
      continue;
    mocks[`node:${key}`] = result.output;
  }
  // Every workflow it reaches goes, so the host runs it from its source here
  // or its copy in the project; test data adds the outputs inside it.
  const reached = parts.callees ?? {
    sources: {},
    references: calledReferenceSites(workflow).map((site) => site.reference),
  };
  const callees: Record<
    string,
    Partial<LocalSource> & { mocks: Record<MockKey, Json> }
  > = {};
  for (const reference of [
    ...reached.references,
    ...Object.keys(testData.workflows ?? {}),
  ])
    callees[reference] ??= {
      ...reached.sources[reference],
      mocks: Object.fromEntries(
        Object.entries(testData.workflows?.[reference]?.mocks ?? {}).map(
          ([key, mock]) => [`node:${key}`, mock.output],
        ),
      ) as Record<MockKey, Json>,
    };
  const input = testData.input?.value;
  return {
    source: parts.source,
    format: parts.format,
    ...(parts.definitions.length ? { definitions: parts.definitions } : {}),
    ...(Object.keys(callees).length ? { workflows: callees } : {}),
    catalog: parts.connected ? "project" : "none",
    stubUnresolved: true,
    input: input === undefined ? parts.sampleInput : (input as Json),
    mocks,
    // Only when some fail: a host that predates failures refuses the field.
    ...(Object.keys(errors).length ? { errors } : {}),
    scripts: {
      signals: { ...(testData.signals ?? {}) },
      humans: { ...(testData.humans ?? {}) },
      aiTurns: Object.fromEntries(
        Object.entries(testData.aiTurns ?? {}).filter(([key]) => !fails(key)),
      ),
      autoAdvanceWaits: true,
    },
    target: parts.target,
    now: parts.now.toISOString(),
  };
}

/**
 * The host's block on a call that waits, in the words of a call: it needs
 * test data (a pin on the call) or the workflow it calls. Other blocks stay.
 */
export function calleeBlock(workflow: Workflow, blocked: Blocked): Blocked {
  if (blocked.reason !== "missing_mock" || blocked.instanceKey) return blocked;
  let found: Step | undefined;
  const visit = (list: Step[]) => {
    for (const step of list) {
      if (step.id === blocked.nodeId) found = step;
      branchesOf(step).forEach(visit);
    }
  };
  visit(workflow.spec.steps ?? []);
  const uses = found && isCall(found) ? String(found["uses"] ?? "") : "";
  if (!uses) return blocked;
  const at = uses.lastIndexOf("@");
  return {
    ...blocked,
    reason: "callee",
    message: `Call ${at > 0 ? uses.slice(0, at) : uses} needs test data or the workflow itself.`,
  };
}

/** What an execution ran inside the workflows it called: for the run log, never read as this workflow's steps. */
export interface CalledRuns {
  /** Each called workflow's run, in the order the calls started. */
  frames: FrameView[];
  /** The steps that ran inside them, each with its frame, in the order they ran. */
  trace: TraceEntry[];
  /** Where the run stopped inside one, as the host says it: that workflow's step, and its frame. */
  blocked: Blocked | null;
}

/**
 * The host's answer as the editor reads it: only this workflow's steps in
 * the trace, and a stop inside a called workflow named on this workflow's
 * call that runs it, so a called workflow's step never passes for one of
 * this workflow's with the same ID. What ran inside called workflows goes
 * apart, for the run log (null when nothing was called).
 */
export function splitCalls(
  workflow: Workflow,
  answer: ExecuteResponse,
): { response: ExecuteResponse; called: CalledRuns | null } {
  const { frames = [], ...rest } = answer;
  const inside = answer.trace.filter((entry) => entry.frame);
  const stop = answer.blocked;
  const response: ExecuteResponse = {
    ...rest,
    trace: answer.trace.filter((entry) => !entry.frame),
  };
  if (stop?.frame) {
    // The call of this workflow the stop is under: the frame's first key.
    const call = stop.frame.split("/")[0];
    const step = instanceStep(call);
    response.blocked = {
      ...stop,
      nodeId: step,
      instanceKey: call === step ? "" : call,
    };
  } else if (stop) response.blocked = calleeBlock(workflow, stop);
  const called =
    frames.length || inside.length
      ? { frames, trace: inside, blocked: stop?.frame ? stop : null }
      : null;
  return { response, called };
}
