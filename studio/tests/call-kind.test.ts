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
// The Call a workflow step in the editor: its tile, its output for the steps
// after it, what it still needs, and what later steps can read from it.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { incompleteChip } from "../src/app/designer/canvas-summary";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  ndvRegistry,
  type Json,
  type KindContext,
  type StepKindDescriptor,
} from "../src/app/editor/ndv/registry";
import { stepOutputSchema, visibleRefs } from "../src/app/forms/core/scope";
import { freshWorkflow, type Step, type Workflow } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const NOTIFY = "notify-customer@1.0.0";
const notifyOutput = {
  type: "object",
  properties: { messageId: { type: "string" }, accepted: { type: "boolean" } },
  required: ["messageId"],
};
const call = (extra: Record<string, unknown> = {}): Step =>
  ({
    id: "notify",
    kind: "callWorkflow",
    mode: "wait",
    with: { literal: {} },
    uses: NOTIFY,
    ...extra,
  }) as Step;
const workflowWith = (...steps: Step[]): Workflow => {
  const workflow = freshWorkflow();
  workflow.metadata.name = "order-intake";
  workflow.spec.steps.push(...steps);
  return workflow;
};
const context = (workflow: Workflow): KindContext => ({
  workflow,
  features: ["flow.callWorkflow"],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: (uses) =>
    uses === NOTIFY
      ? ({
          inputSchema: { type: "object" },
          outputSchema: notifyOutput,
          callable: {},
        } as Json)
      : null,
});
const kind = (): StepKindDescriptor => ndvRegistry.kind("callWorkflow")!;

describe("the Call a workflow step", () => {
  it("is a flow step drawn as a stacked tile, gated by the platform's calls", () => {
    expect(kind()).toMatchObject({
      kind: "callWorkflow",
      label: "Call a workflow",
      role: "sub-workflow",
      category: "flow",
      icon: "callWorkflow",
      feature: "flow.callWorkflow",
    });
  });

  it("starts as a call that waits, with an empty input and no workflow", () => {
    expect(kind().create("call-workflow-1")).toEqual({
      id: "call-workflow-1",
      kind: "callWorkflow",
      mode: "wait",
      with: { literal: {} },
    });
    expect(kind().idPrefix).toBe("call-workflow");
  });

  it("names the workflow and version it calls on its tile, and says when it doesn't wait", () => {
    const ctx = context(workflowWith(call()));
    expect(kind().summary(call(), ctx)).toBe("notify-customer 1.0.0");
    expect(kind().summary(call({ mode: "detach" }), ctx)).toBe(
      "notify-customer 1.0.0 · Doesn't wait",
    );
    expect(kind().summary(call({ uses: undefined }), ctx)).toBe("");
  });

  it("needs a workflow to call", () => {
    expect(incompleteChip(call({ uses: undefined }))).toBe(
      "Choose a workflow to call",
    );
    expect(incompleteChip(call({ uses: "" }))).toBe(
      "Choose a workflow to call",
    );
    expect(incompleteChip(call())).toBe("");
  });

  it("outputs what the called workflow returns, the result status around it, or nothing", () => {
    const ctx = context(workflowWith(call()));
    expect(kind().outputSchema(call(), ctx)).toEqual(notifyOutput);
    expect(kind().outputSchema(call({ mode: "detach" }), ctx)).toEqual({
      type: "null",
    });
    expect(
      kind().outputSchema(call({ onFailure: "continue" }), ctx),
    ).toMatchObject({ required: ["status", "runId"] });
    // A version Studio can't read outputs any value until it can.
    expect(kind().outputSchema(call({ uses: "other@1.0.0" }), ctx)).toEqual({});
  });

  it("can be pinned only when it waits for a result", () => {
    const pinnable = kind().pinnable as (step: Step) => boolean;
    expect(pinnable(call())).toBe(true);
    expect(pinnable(call({ mode: "detach" }))).toBe(false);
  });

  it("offers the called workflow's result to the steps after it", () => {
    const after = {
      id: "log",
      kind: "transform",
      value: { literal: {} },
    } as Step;
    const workflow = workflowWith(call(), after);
    const output = (uses: string) =>
      uses === NOTIFY ? notifyOutput : undefined;
    expect(
      stepOutputSchema(workflow, "notify", { workflowOutput: output }),
    ).toEqual(notifyOutput);
    const paths = visibleRefs(workflow, "log", "/value", {
      workflowOutput: output,
    }).map((entry) => entry.ref);
    expect(paths).toContain("/steps/notify/output/messageId");
    // Without the called workflow's result, the output is any value, as the compiler sees it.
    expect(stepOutputSchema(workflow, "notify")).toEqual({});
    const detached = workflowWith(call({ mode: "detach" }), after);
    expect(
      stepOutputSchema(detached, "notify", { workflowOutput: output }),
    ).toEqual({ type: "null" });
  });
});
