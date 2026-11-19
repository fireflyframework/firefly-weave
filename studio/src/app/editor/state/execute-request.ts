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
import type { Json } from "../ndv/registry";
import type {
  Blocked,
  ExecuteRequest,
  ExecuteTarget,
  MockKey,
} from "./execution";
import { instanceStep } from "./instance-keys";
import type { LastResults } from "./last-results";
import type { StudioTestData } from "./test-data";

type JsonObject = { [key: string]: Json };

/** The step lists a step holds, as the host's slicing walks them (transactions included). */
function branchesOf(step: Step): Step[][] {
  const list = (branch: unknown): Step[] =>
    branch &&
    typeof branch === "object" &&
    Array.isArray((branch as { steps?: unknown }).steps)
      ? (branch as { steps: Step[] }).steps
      : [];
  switch (step.kind as string) {
    case "switch":
      return [
        ...(Array.isArray(step["cases"]) ? step["cases"].map(list) : []),
        list(step["default"]),
      ];
    case "parallel":
      return step["branches"] && typeof step["branches"] === "object"
        ? Object.values(step["branches"] as Record<string, unknown>).map(list)
        : [];
    case "forEach":
      return [list(step["body"])];
    case "transaction":
      return [list(step)];
    default:
      return [];
  }
}

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
  const visit = (list: Step[]) => {
    for (const step of list) {
      steps.set(step.id, step);
      branchesOf(step).forEach(visit);
    }
  };
  visit(workflow.spec.steps ?? []);
  // Executing one step sends only the test data of the steps it runs.
  const runs =
    parts.target.mode === "workflow" || !parts.target.stepId
      ? null
      : new Set(sliceStepIds(workflow, parts.target.stepId) ?? []);
  const sent = (key: string) => !runs || runs.has(instanceStep(key));
  const mocks: Record<MockKey, Json> = {};
  const pins = testData.pins ?? {};
  for (const [key, pin] of Object.entries(pins))
    if (sent(key) && mocked(steps.get(instanceStep(key))))
      mocks[`node:${key}`] = pin.output;
  for (const [key, result] of Object.entries(results?.steps ?? {})) {
    const step = instanceStep(key);
    if (
      result.output === undefined ||
      !sent(key) ||
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
    scripts: {
      signals: { ...(testData.signals ?? {}) },
      humans: { ...(testData.humans ?? {}) },
      aiTurns: { ...(testData.aiTurns ?? {}) },
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
