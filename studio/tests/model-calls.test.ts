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
// A workflow with a step that calls another workflow opens for editing, and
// the call's input and business key follow the steps they read.
import { describe, expect, it } from "vitest";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
} from "../src/app/model";

function opened() {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    { id: "fetch", kind: "transform", value: { literal: {} } },
    {
      id: "notify",
      kind: "callWorkflow",
      uses: "notify-customer@1.0.0",
      mode: "wait",
      with: { object: { customerId: { ref: "/steps/fetch/output/id" } } },
      businessKey: { ref: "/steps/fetch/output/order" },
    } as unknown as Step,
  ];
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow), "json");
  return model;
}

describe("a call step in the workflow model", () => {
  it("opens for editing", () => {
    const model = opened();
    expect(model.error).toBe("");
    expect(model.readonly).toBe(false);
    expect(model.nodes().map((node) => node.step.id)).toEqual([
      "fetch",
      "notify",
    ]);
  });

  it("rewrites its input and business key when the step they read is renamed", () => {
    const model = opened();
    expect(model.renameStep("fetch", "load")).toBe(2);
    const call = model.definition.spec.steps[1];
    expect(call["with"]).toEqual({
      object: { customerId: { ref: "/steps/load/output/id" } },
    });
    expect(call["businessKey"]).toEqual({ ref: "/steps/load/output/order" });
  });
});
