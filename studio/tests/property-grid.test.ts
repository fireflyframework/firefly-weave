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
import { describe, it, expect } from "vitest";
import {
  PropertyDraft,
  StepPropertyGrid,
  ExpressionEditor,
} from "../src/app/property-grid";
import { createStep, freshWorkflow } from "../src/app/model";
describe("step property draft", () => {
  it("keeps required examples out of new step values", () => {
    for (const [kind, key] of [
      ["signal", "name"],
      ["fail", "message"],
      ["decisionTable", "uses"],
    ] as const) {
      const step = createStep(kind, "new-step");
      expect(step[key]).toBe("");
      expect(new PropertyDraft(step).errors.has(key)).toBe(true);
    }
  });
  it("explains durations in the chosen unit and parallel limits", () => {
    const grid = new StepPropertyGrid();
    grid.step = createStep("wait", "wait");
    grid.ngOnChanges();
    expect(grid.durationHelp(grid.fields[0])).toBe(
      "The run pauses for 1 minute, then continues to the next step.",
    );
    grid.step = createStep("signal", "signal");
    grid.ngOnChanges();
    const timeout = grid.fields.find(
      (field) => field.path[0] === "timeoutSeconds",
    )!;
    expect(grid.durationHelp(timeout)).toBe(
      "If nothing arrives within 1 hour, the run ends as timed out.",
    );
    grid.durationUnits.set("timeoutSeconds", "min");
    grid.change(timeout, 120);
    expect(grid.durationHelp(timeout)).toBe(
      "If nothing arrives within 2 minutes, the run ends as timed out.",
    );
    grid.step = createStep("parallel", "parallel");
    grid.ngOnChanges();
    expect(grid.fields[0].label).toBe("Run at most");
  });
  it("blocks invalid positive integers and preserves input", () => {
    const source = createStep("wait", "one"),
      draft = new PropertyDraft(source);
    expect(draft.set(["durationSeconds"], 0)).toBe(false);
    expect(draft.valid).toBe(false);
    expect(source["durationSeconds"]).toBe(60);
    expect(draft.set(["durationSeconds"], 90)).toBe(true);
    expect(draft.snapshot()["durationSeconds"]).toBe(90);
  });
  it("preserves unknown keys and nested branch steps", () => {
    const source = createStep("switch", "switch-1");
    source["extra"] = { keep: true };
    (source["cases"] as { steps: unknown[] }[])[0].steps.push(
      createStep("wait", "child"),
    );
    const draft = new PropertyDraft(source);
    expect(draft.set(["cases", 0, "when"], { ref: "/input/approved" })).toBe(
      true,
    );
    expect(draft.snapshot()["extra"]).toEqual({ keep: true });
    expect(
      (draft.snapshot()["cases"] as { steps: unknown[] }[])[0].steps,
    ).toEqual([createStep("wait", "child")]);
    // The source is untouched: its new case still has no condition.
    expect((source["cases"] as { when?: unknown }[])[0].when).toBeUndefined();
  });
  it("lets a case without a condition apply; the validator reports it", () => {
    const draft = new PropertyDraft(createStep("switch", "decision-1"));
    expect(draft.valid).toBe(true);
    expect(draft.set(["cases", 0, "when"], { literal: "nope" })).toBe(true);
    expect(draft.set(["cases", 0, "when"], { ref: "bad" })).toBe(false);
    expect(draft.set(["cases", 0, "when"], undefined)).toBe(true);
  });
  it("validates decisions and pointers and preserves literal types", () => {
    const draft = new PropertyDraft(createStep("humanTask", "review"));
    expect(draft.set(["decisions"], ["approve", "approve"])).toBe(false);
    expect(draft.set(["decisions"], ["approve", "reject"])).toBe(true);
    expect(draft.set(["title"], { ref: "input/title" })).toBe(false);
    expect(draft.set(["title"], { literal: "Review" })).toBe(true);
    expect(
      draft.set(["context"], {
        literal: { count: 2, enabled: true, names: ["a"] },
      }),
    ).toBe(true);
    expect(draft.snapshot()["context"]).toEqual({
      literal: { count: 2, enabled: true, names: ["a"] },
    });
  });
  it("blocks invalid human title/context literals", () => {
    const draft = new PropertyDraft(createStep("humanTask", "review"));
    expect(draft.set(["title"], { literal: { wrong: true } })).toBe(false);
    expect(draft.set(["title"], { literal: "x".repeat(513) })).toBe(false);
    expect(draft.set(["title"], { literal: "Review" })).toBe(true);
    expect(draft.set(["context"], { literal: "wrong" })).toBe(false);
  });
  it("emits only valid independent snapshots and resets selection buffers", () => {
    const grid = new StepPropertyGrid();
    grid.step = createStep("wait", "one");
    grid.ngOnChanges();
    const committed: unknown[] = [];
    grid.stepChange.subscribe((v) => committed.push(v));
    grid.change(grid.fields[0], 0);
    expect(committed).toHaveLength(0);
    grid.change(grid.fields[0], 120);
    expect(committed).toHaveLength(1);
    expect(grid.step["durationSeconds"]).toBe(60);
    grid.buffers.set("durationSeconds", "invalid");
    grid.step = createStep("wait", "two");
    grid.ngOnChanges();
    expect(grid.text(grid.fields[0])).toBe("60");
    grid.readOnly = true;
    grid.change(grid.fields[0], 300);
    expect(committed).toHaveLength(1);
  });
  it("resets drafts and errors on node selection", () => {
    const draft = new PropertyDraft(createStep("wait", "one"));
    draft.set(["durationSeconds"], -1);
    draft.reset(createStep("wait", "two"));
    expect(draft.valid).toBe(true);
    expect(draft.snapshot()).toEqual(createStep("wait", "two"));
  });
});

it("workflow option fields validate slots/timeouts and preserve source input", () => {
  const source = freshWorkflow(),
    draft = new PropertyDraft(source);
  expect(
    draft.set(["spec", "connections"], {
      crm: { connector: "crm@1.0.0", required: true },
    }),
  ).toBe(true);
  expect(draft.set(["spec", "timeoutSeconds"], 0)).toBe(false);
  expect(draft.set(["spec", "timeoutSeconds"], 120)).toBe(true);
  expect(source.spec["connections"]).toBeUndefined();
});
it("operation arguments and references are validated", () => {
  const draft = new PropertyDraft(createStep("transform", "map"));
  expect(
    draft.set(["value"], {
      op: { name: "eq", args: [{ ref: "/input/approved" }, { literal: true }] },
    }),
  ).toBe(true);
  expect(
    draft.set(["value"], { op: { name: "eq", args: [{ literal: 1 }] } }),
  ).toBe(false);
  expect(
    draft.set(["value"], { op: { name: "exists", args: [{ literal: 1 }] } }),
  ).toBe(false);
  expect(
    draft.set(["value"], {
      op: { name: "exists", args: [{ ref: "/input/key" }] },
    }),
  ).toBe(true);
});

it("matches native semantic versions including prerelease and build", () => {
  const draft = new PropertyDraft(createStep("action", "call"));
  expect(draft.set(["uses"], "crm@1.0.0-beta.1+build.2")).toBe(true);
  expect(draft.set(["uses"], "crm@01.0.0")).toBe(false);
  expect(draft.set(["uses"], "crm@1.0.0-01")).toBe(false);
});
it("mapped expression unchanged key keeps its field", () => {
  const editor = new ExpressionEditor();
  editor.value = { object: { keep: { literal: 1 } } };
  editor.ngOnChanges();
  editor.rename("keep", "keep");
  expect(editor.current).toEqual({ object: { keep: { literal: 1 } } });
});

it("mapped object rename preserves reserved Unicode-capable JSON keys", () => {
  const editor = new ExpressionEditor();
  editor.value = { object: { customer: { literal: 1 } } };
  editor.ngOnChanges();
  editor.rename("customer", "__proto__");
  const body = editor.current["object"] as Record<string, unknown>;
  expect(Object.hasOwn(body, "__proto__")).toBe(true);
  expect(JSON.stringify(editor.current)).toBe(
    '{"object":{"__proto__":{"literal":1}}}',
  );
});

it("a colliding mapped key stays invalid when a sibling changes", () => {
  const editor = new ExpressionEditor();
  editor.value = {
    object: { first: { literal: 1 }, second: { literal: 2 } },
  };
  editor.ngOnChanges();
  const validity: boolean[] = [];
  const emitted: unknown[] = [];
  editor.validityChange.subscribe((v) => validity.push(v));
  editor.valueChange.subscribe((v) => emitted.push(v));
  editor.rename("first", "second");
  expect(validity.at(-1)).toBe(false);
  editor.update("second", { literal: 3 });
  expect(validity.at(-1)).toBe(false);
  expect(emitted).toHaveLength(0);
  expect(editor.error).toBe("Use a unique nonempty key.");
  editor.rename("first", "third");
  expect(validity.at(-1)).toBe(true);
  expect(emitted.at(-1)).toEqual({
    object: { third: { literal: 1 }, second: { literal: 3 } },
  });
});

it("an echoed value keeps local validity; an external change resets it", () => {
  const editor = new ExpressionEditor();
  editor.value = { object: { a: { literal: 1 } } };
  editor.ngOnChanges();
  editor.childValidity("a", false);
  const generation = editor.generation;
  editor.value = structuredClone(editor.value);
  editor.ngOnChanges();
  expect(editor.invalid.has("a")).toBe(true);
  expect(editor.generation).toBe(generation);
  editor.value = { object: { b: { literal: 2 } } };
  editor.ngOnChanges();
  expect(editor.invalid.size).toBe(0);
  expect(editor.generation).toBe(generation + 1);
});

it("switching from Fields to a typed value and back from Data keeps that value", () => {
  const editor = new ExpressionEditor();
  editor.value = { literal: {} };
  editor.ngOnChanges();
  editor.setMode("literal");
  editor.replaceBody("kept text");
  editor.setMode("ref");
  editor.setMode("literal");
  expect(editor.current).toEqual({ literal: "kept text" });
});

it("editing a literal list keeps literals plain and encodes data as an expression", () => {
  const editor = new ExpressionEditor();
  editor.value = { literal: [{ amount: 7 }, "kept"] };
  editor.ngOnChanges();
  editor.update("0", { literal: { amount: 9 } });
  expect(editor.current).toEqual({ literal: [{ amount: 9 }, "kept"] });
  editor.update("0", { ref: "/input/amount" });
  expect(editor.current).toEqual({
    array: [{ ref: "/input/amount" }, { literal: "kept" }],
  });
});

it("emits a valid sibling edit without replacing an invalid field draft", () => {
  const grid = new StepPropertyGrid();
  grid.step = createStep("fail", "failed");
  grid.ngOnChanges();
  const emitted: unknown[] = [];
  grid.stepChange.subscribe((value) => emitted.push(value));
  const code = grid.fields.find((field) => field.path[0] === "code")!;
  const message = grid.fields.find((field) => field.path[0] === "message")!;
  grid.change(code, "bad code");
  expect(emitted).toEqual([]);
  grid.change(message, "Payment rejected");
  expect(emitted.at(-1)).toMatchObject({
    code: grid.step["code"],
    message: "Payment rejected",
  });
  expect(grid.draft.get(code.path)).toBe("bad code");
  expect(grid.error(code)).toBe("");
  grid.touchedFields.add(grid.key(code));
  expect(grid.error(code)).toContain("Use letters");
});

it("emits the valid Fields sibling while preserving a nested invalid draft", () => {
  const grid = new StepPropertyGrid();
  grid.step = { ...createStep("action", "lookup"), uses: "lookup@1.0.0" };
  grid.ngOnChanges();
  const field = grid.fields.find((item) => item.path[0] === "with")!;
  const editor = new ExpressionEditor();
  editor.value = { literal: { a: 0, b: "" } };
  editor.ngOnChanges();
  const emitted: unknown[] = [];
  const validity: boolean[] = [];
  editor.validityChange.subscribe((valid) => grid.validity(field, valid));
  editor.valueChange.subscribe((value) => grid.change(field, value));
  grid.stepChange.subscribe((value) => emitted.push(value));
  grid.validityChange.subscribe((valid) => validity.push(valid));
  const [a, b] = editor.fieldDraft.rows;
  editor.childValidity("field-" + a.id, false);
  editor.updateField(b, { literal: "hello" });
  expect(emitted.at(-1)).toMatchObject({
    with: { literal: { a: 0, b: "hello" } },
  });
  expect(validity.at(-1)).toBe(false);
  expect(editor.invalid.has("field-" + a.id)).toBe(true);
  expect(grid.error(field)).toBe(""); // The nested editor owns its inline error.
});
