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
import { beforeAll, describe, expect, it } from "vitest";
import { getAt } from "../src/app/forms/core/json";
import { EditError, applyEdits, stepPath } from "../src/app/editor/ndv/edits";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import type { Edit } from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow, type Workflow } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const notifyPath = ["spec", "steps", 1, "branches", "second", "steps", 0];
function nested(): Workflow {
  const workflow = freshWorkflow();
  const fan = createStep("parallel", "fan");
  const branches = fan["branches"] as Record<string, { steps: unknown[] }>;
  branches["second"].steps.push(createStep("action", "notify"));
  workflow.spec.steps.push(createStep("transform", "shape"), fan);
  return workflow;
}

describe("step details edits", () => {
  it("finds a step inside a parallel branch", () => {
    expect(stepPath(nested(), "notify")).toEqual(notifyPath);
    expect(stepPath(nested(), "shape")).toEqual(["spec", "steps", 0]);
    expect(stepPath(nested(), "missing")).toBeNull();
  });

  it("edits the step's own fields and leaves the input untouched", () => {
    const before = nested();
    const mapped = { object: { id: { ref: "/input/id" } } };
    const after = applyEdits(before, "notify", [
      { path: ["with"], value: mapped },
    ]);
    expect(getAt(after, [...notifyPath, "with"])).toEqual(mapped);
    expect(getAt(before, [...notifyPath, "with"])).toEqual({ literal: {} });
  });

  it("writes workflow-level data with the workflow scope", () => {
    const profile = { provider: "ollama", model: "qwen3:4b" };
    const after = applyEdits(nested(), "notify", [
      {
        path: ["spec", "llmProfiles", "support"],
        value: profile,
        scope: "workflow",
      },
    ]);
    expect(after.spec["llmProfiles"]).toEqual({ support: profile });
  });

  it("deletes a key or a list item and keeps the emptied parent", () => {
    const changes: Edit[] = [
      { path: ["with"], value: { object: { id: { ref: "/input/id" } } } },
      { path: ["with", "object", "id"], value: undefined },
    ];
    const after = applyEdits(nested(), "notify", changes);
    expect(getAt(after, [...notifyPath, "with"])).toEqual({ object: {} });

    const workflow = freshWorkflow();
    const route = createStep("switch", "route");
    (route["cases"] as unknown[]).push({ steps: [], output: { literal: 2 } });
    workflow.spec.steps.push(route);
    const fewer = applyEdits(workflow, "route", [
      { path: ["cases", 0], value: undefined },
    ]);
    expect(getAt(fewer, ["spec", "steps", 0, "cases"])).toEqual([
      { steps: [], output: { literal: 2 } },
    ]);
  });

  it("applies changes in order", () => {
    const after = applyEdits(nested(), "shape", [
      { path: ["value"], value: { literal: 1 } },
      { path: ["value"], value: { literal: 2 } },
    ]);
    expect(getAt(after, ["spec", "steps", 0, "value"])).toEqual({ literal: 2 });
  });

  it.each([
    [{ path: [], value: 1 }],
    [{ path: ["id"], value: "renamed" }],
    [{ path: ["kind"], value: "wait" }],
    [{ path: ["spec", "steps", 0], value: {}, scope: "workflow" }],
    [{ path: ["apiVersion"], value: "weave/v2", scope: "workflow" }],
    [{ path: ["spec"], value: { steps: [] }, scope: "workflow" }],
    [{ path: ["metadata"], value: undefined, scope: "workflow" }],
  ] satisfies [Edit][])(
    "refuses an edit that bypasses rename, the canvas or the document shape: %j",
    (change) => {
      expect(() => applyEdits(nested(), "notify", [change])).toThrow(EditError);
    },
  );

  it("refuses a step that isn't in the workflow", () => {
    expect(() =>
      applyEdits(nested(), "missing", [{ path: ["with"], value: {} }]),
    ).toThrow(new EditError("Step missing isn't in this workflow."));
  });

  it("refuses a list item past the end of the list with an EditError", () => {
    const workflow = freshWorkflow();
    workflow.spec.steps.push(createStep("switch", "route"));
    expect(() =>
      applyEdits(workflow, "route", [{ path: ["cases", 5], value: {} }]),
    ).toThrow(EditError);
  });
});

describe("steps inside a container", () => {
  function routed(): Workflow {
    const workflow = freshWorkflow();
    const route = createStep("switch", "route");
    (route["cases"] as { steps: unknown[] }[])[0].steps.push(
      createStep("action", "notify"),
    );
    workflow.spec.steps.push(route);
    return workflow;
  }
  const nestedStep = ["spec", "steps", 0, "cases", 0, "steps", 0];

  it.each([
    ["id", "renamed"],
    ["kind", "wait"],
  ])("refuses to change the %s of a step in a case", (field, value) => {
    expect(() =>
      applyEdits(routed(), "route", [
        { path: ["cases", 0, "steps", 0, field], value },
      ]),
    ).toThrow(EditError);
  });

  it.each([
    ["id", "renamed"],
    ["kind", "wait"],
  ])(
    "refuses to change the %s of a step in a parallel branch",
    (field, value) => {
      expect(() =>
        applyEdits(nested(), "fan", [
          { path: ["branches", "second", "steps", 0, field], value },
        ]),
      ).toThrow(EditError);
    },
  );

  it("still writes the other fields of a step in a case, and the container itself", () => {
    const mapped = { object: { id: { ref: "/input/id" } } };
    const edited = applyEdits(routed(), "route", [
      { path: ["cases", 0, "steps", 0, "with"], value: mapped },
    ]);
    expect(getAt(edited, [...nestedStep, "with"])).toEqual(mapped);
    expect(getAt(edited, [...nestedStep, "id"])).toBe("notify");

    const emptied = applyEdits(routed(), "route", [
      { path: ["cases", 0, "steps"], value: [] },
    ]);
    expect(getAt(emptied, ["spec", "steps", 0, "cases", 0, "steps"])).toEqual(
      [],
    );
  });
});

describe("list positions", () => {
  const toolsPath = ["spec", "steps", 0, "tools"];

  it("starts a one-item list when the list is absent and the edit appends", () => {
    const after = applyEdits(nested(), "shape", [
      { path: ["tools", "-"], value: { name: "orders-get" } },
    ]);
    expect(getAt(after, toolsPath)).toEqual([{ name: "orders-get" }]);
  });

  it("keeps both items when an absent list is appended to twice", () => {
    const after = applyEdits(nested(), "shape", [
      { path: ["tools", "-"], value: { name: "orders-get" } },
      { path: ["tools", "-"], value: { name: "notify-customer" } },
    ]);
    expect(getAt(after, toolsPath)).toEqual([
      { name: "orders-get" },
      { name: "notify-customer" },
    ]);
  });

  it("starts a one-item list when the list is absent and the edit writes item 0", () => {
    const after = applyEdits(nested(), "shape", [
      { path: ["tools", 0], value: { name: "orders-get" } },
    ]);
    expect(getAt(after, toolsPath)).toEqual([{ name: "orders-get" }]);
  });

  it("refuses a later item when the list is absent", () => {
    expect(() =>
      applyEdits(nested(), "shape", [
        { path: ["tools", 1], value: { name: "orders-get" } },
      ]),
    ).toThrow(EditError);
  });

  it.each([
    ["an append", "-"],
    ["item 0", 0],
  ] satisfies [string, string | number][])(
    "refuses %s under a field that holds an object",
    (_name, segment) => {
      expect(() =>
        applyEdits(nested(), "notify", [
          { path: ["with", segment], value: { literal: 1 } },
        ]),
      ).toThrow(EditError);
    },
  );

  it("refuses a list position under a field that holds text", () => {
    expect(() =>
      applyEdits(nested(), "notify", [{ path: ["uses", "-"], value: "a" }]),
    ).toThrow(EditError);
  });

  it("appends to, and writes inside, a list that exists", () => {
    const workflow = freshWorkflow();
    workflow.spec.steps.push(createStep("switch", "route"));
    const casesPath = ["spec", "steps", 0, "cases"];
    const second = { steps: [], output: { literal: 2 } };
    const third = { steps: [], output: { literal: 3 } };
    const replaced = { steps: [], output: { literal: 9 } };

    const appended = applyEdits(workflow, "route", [
      { path: ["cases", "-"], value: second },
      { path: ["cases", 2], value: third },
      { path: ["cases", 0], value: replaced },
    ]);
    expect(getAt(appended, casesPath)).toEqual([replaced, second, third]);
    expect(() =>
      applyEdits(workflow, "route", [{ path: ["cases", 2], value: third }]),
    ).toThrow(EditError);
  });
});
