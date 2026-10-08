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
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { stepSummary } from "../src/app/designer/canvas-summary";
import { conditionSummary } from "../src/app/designer/conditions";
import {
  createKindLoader,
  loadKindRegistrations,
} from "../src/app/editor/ndv/kinds";
import {
  ndvRegistry,
  type Json,
  type KindContext,
  type StepKindDescriptor,
} from "../src/app/editor/ndv/registry";
import {
  createStep,
  freshWorkflow,
  kinds,
  stepIdPrefix,
  type Step,
  type Workflow,
} from "../src/app/model";
import { propertyFields } from "../src/app/property-grid";

beforeAll(() => loadKindRegistrations());

const descriptor = (kind: string): StepKindDescriptor => {
  const found = ndvRegistry.kind(kind);
  if (!found) throw new Error(`No descriptor for ${kind}`);
  return found;
};
const workflowWith = (...steps: Step[]): Workflow => {
  const workflow = freshWorkflow();
  workflow.spec.steps.push(...steps);
  return workflow;
};
const context = (
  workflow: Workflow,
  contracts: Record<string, Json> = {},
): KindContext => ({
  workflow,
  features: [],
  actionContract: (uses) => contracts[uses] ?? null,
  tableContract: (uses) => contracts[uses] ?? null,
  workflowContract: () => null,
});
const classicFields = (step: Step) =>
  propertyFields(step)
    .filter((f) => f.kind === "expression")
    .map((f) => ({ path: f.path, label: f.label }));
const registryFields = (step: Step) =>
  descriptor(step.kind)
    .fields(step)
    .map((f) => ({ path: f.path, label: f.label }));

describe("built-in step kinds", () => {
  it("has a descriptor for each of the ten kinds", () => {
    expect(
      ndvRegistry
        .kinds()
        .map((d) => d.kind)
        .sort(),
    ).toEqual([...kinds].sort());
  });

  it("creates the classic editor's defaults and ID prefixes", () => {
    for (const kind of kinds) {
      expect(descriptor(kind).create("s"), kind).toEqual(createStep(kind, "s"));
      expect(descriptor(kind).idPrefix, kind).toBe(stepIdPrefix[kind]);
    }
  });

  it("lists every expression field the classic editor edits", () => {
    const route = createStep("switch", "route");
    (route["cases"] as unknown[]).push({ steps: [], output: { literal: {} } });
    const fan = createStep("parallel", "fan");
    (fan["branches"] as Record<string, unknown>)["third"] = {
      steps: [],
      output: { literal: {} },
    };
    for (const step of [...kinds.map((k) => createStep(k, "s")), route, fan])
      expect(registryFields(step), step.kind).toEqual(classicFields(step));
  });

  it("gives each kind its role, panel category, icon and test data needs", () => {
    expect(
      Object.fromEntries(
        ndvRegistry
          .kinds()
          .map((d) => [
            d.kind,
            [d.role, d.category, d.icon, d.pinnable, d.script ?? null],
          ]),
      ),
    ).toEqual({
      action: ["app", "app", "action", true, null],
      decisionTable: ["rules", "data", "decisionTable", false, null],
      llm: ["ai-task", "ai", "llm", true, null],
      transform: ["transform", "data", "transform", false, null],
      switch: ["decision", "flow", "switch", false, null],
      parallel: ["parallel", "flow", "parallel", false, null],
      wait: ["wait", "wait", "wait", false, null],
      signal: ["signal", "wait", "signal", false, "signal"],
      humanTask: ["human", "human", "humanTask", false, "human"],
      fail: ["fail", "flow", "fail", false, null],
    });
  });

  it("labels each decision path and its lane", () => {
    const route = createStep("switch", "route");
    const amount = {
      op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 1000 }] },
    };
    (route["cases"] as unknown[]).push({
      when: amount,
      steps: [],
      output: { literal: {} },
    });
    const workflow = workflowWith(route);
    const kind = descriptor("switch");
    expect(kind.outputs?.(route, context(workflow))).toEqual([
      {
        id: "case:0",
        label: conditionSummary(undefined, workflow),
        containerPath: ["cases", 0, "steps"],
      },
      {
        id: "case:1",
        label: conditionSummary(amount, workflow),
        containerPath: ["cases", 1, "steps"],
      },
      {
        id: "default",
        label: "Otherwise",
        containerPath: ["default", "steps"],
      },
    ]);
    expect(
      kind
        .containers?.(route)
        .map((c) => [c.path, c.layout, c.label(route, context(workflow))]),
    ).toEqual([
      [["cases", 0, "steps"], "lanes", "Case 1"],
      [["cases", 1, "steps"], "lanes", "Case 2"],
      [["default", "steps"], "lanes", "Otherwise"],
    ]);
  });

  it("gives a parallel step one output and lane per branch, and fail none", () => {
    const fan = createStep("parallel", "fan");
    const workflow = workflowWith(fan);
    expect(descriptor("parallel").outputs?.(fan, context(workflow))).toEqual([
      {
        id: "branch:first",
        label: "first",
        containerPath: ["branches", "first", "steps"],
      },
      {
        id: "branch:second",
        label: "second",
        containerPath: ["branches", "second", "steps"],
      },
    ]);
    const stop = createStep("fail", "stop");
    expect(
      descriptor("fail").outputs?.(stop, context(workflowWith(stop))),
    ).toEqual([]);
    expect(
      descriptor("fail").outputSchema(stop, context(workflowWith(stop))),
    ).toBeNull();
  });

  it("reads schemas from contracts and from the steps' own fields", () => {
    const lookup = {
      ...createStep("action", "lookup"),
      uses: "crm.lookup@1.0.0",
    };
    const shape = {
      ...createStep("transform", "shape"),
      value: { literal: { a: 1 } },
    };
    const approval = createStep("humanTask", "approval");
    const workflow = workflowWith(lookup, shape, approval);
    const crm: Json = {
      spec: {
        inputSchema: { type: "object", properties: { id: { type: "string" } } },
        outputSchema: {
          type: "object",
          properties: { eligible: { type: "boolean" } },
        },
      },
    };
    const known = context(workflow, { "crm.lookup@1.0.0": crm });
    expect(descriptor("action").outputSchema(lookup, known)).toEqual({
      type: "object",
      properties: { eligible: { type: "boolean" } },
    });
    expect(
      descriptor("action").fields(lookup)[0].expectedSchema?.(lookup, known),
    ).toEqual({ type: "object", properties: { id: { type: "string" } } });
    expect(
      descriptor("action").outputSchema(lookup, context(workflow)),
    ).toEqual({});
    expect(descriptor("transform").outputSchema(shape, known)).toEqual({
      type: "object",
      properties: { a: { type: "integer" } },
      required: ["a"],
      additionalProperties: false,
    });
    expect(descriptor("humanTask").outputSchema(approval, known)).toEqual({
      type: "object",
      properties: {
        decision: { type: "string", enum: ["approve", "reject"] },
        data: { type: "object", properties: {} },
      },
      required: ["decision", "data"],
      additionalProperties: false,
    });
  });

  it("summarizes steps the way the canvas does", () => {
    const wait = createStep("wait", "pause");
    const workflow = workflowWith(wait);
    expect(descriptor("wait").summary(wait, context(workflow))).toBe(
      stepSummary(wait, workflow),
    );
  });
});

describe("registration modules", () => {
  it("registers each module once and retries one that failed to load", async () => {
    const calls = { first: 0, second: 0 };
    let failSecond = true;
    const load = createKindLoader([
      async () => ({ register: () => void calls.first++ }),
      async () => {
        if (failSecond) {
          failSecond = false;
          throw new Error("Lost chunk");
        }
        return { register: () => void calls.second++ };
      },
    ]);
    await expect(load()).rejects.toThrow("Lost chunk");
    await load();
    await load();
    expect(calls).toEqual({ first: 1, second: 1 });
  });
});
