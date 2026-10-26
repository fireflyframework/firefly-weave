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
// the request built from test data and the last results. DOM-free.
import type { Step, Workflow } from "../../model";
import type { Json } from "../ndv/registry";
import type { ExecuteRequest, ExecuteTarget, MockKey } from "./execution";
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
const mocked = (step: Step | undefined): boolean =>
  !!step &&
  (MOCKED_KINDS.has(step.kind) ||
    ((step.kind as string) === "callWorkflow" && step["mode"] !== "detach"));

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
  const mocks: Record<MockKey, Json> = {};
  const pins = testData.pins ?? {};
  for (const [key, pin] of Object.entries(pins))
    if (mocked(steps.get(instanceStep(key)))) mocks[`node:${key}`] = pin.output;
  for (const [key, result] of Object.entries(results?.steps ?? {})) {
    const step = instanceStep(key);
    if (
      result.output === undefined ||
      !mocked(steps.get(step)) ||
      Object.hasOwn(pins, key) ||
      Object.hasOwn(pins, step)
    )
      continue;
    mocks[`node:${key}`] = result.output;
  }
  const callees: Record<string, { mocks: Record<MockKey, Json> }> = {};
  for (const [reference, callee] of Object.entries(testData.workflows ?? {}))
    callees[reference] = {
      mocks: Object.fromEntries(
        Object.entries(callee.mocks ?? {}).map(([key, mock]) => [
          `node:${key}`,
          mock.output,
        ]),
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
