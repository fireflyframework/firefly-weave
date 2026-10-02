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
    expect((source["cases"] as { when: unknown }[])[0].when).toEqual({
      literal: true,
    });
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
