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
import { describe, expect, it } from "vitest";
import {
  ABSENT,
  FormWriteError,
  applyChange,
  fixed,
  isDefault,
  mapped,
  readEntries,
  readList,
  readKeyed,
  writeList,
  writeKeyed,
  readParam,
  resetChanges,
  writeParam,
  type FormChange,
  type FormSubject,
} from "../src/app/editor/ndv/params/value-io";
import { EditError, applyEdits } from "../src/app/editor/ndv/edits";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import { freshWorkflow, type Step } from "../src/app/model";

const pathRef = { ref: "/input/customerId" };
const step: Step = {
  id: "get-orders",
  kind: "action",
  uses: "order-intake.get-orders@1.0.0",
  with: {
    object: {
      path: { object: { customerId: pathRef } },
      query: { literal: { limit: 25 } },
    },
  },
};
const subject = (s: Step = step, action: unknown = null): FormSubject => ({
  step: s,
  workflow: {
    ...freshWorkflow(),
    spec: { ...freshWorkflow().spec, steps: [s] },
  },
  action: action as never,
  roots: [["with"]],
});
const spec = (
  path: (string | number)[],
  extra: Partial<ParamSpec> = {},
): ParamSpec => ({
  id: path.join("."),
  path,
  type: "text",
  label: "Field",
  ...extra,
});

describe("projected input fields", () => {
  const projected = spec(["with", "amount"], {
    type: "number",
    required: true,
    referenceProjection: {
      pointer: "/input",
      values: {
        amount: { ref: "/input/amount" },
        tier: { ref: "/input/tier" },
      },
    },
  });
  it("keeps the whole reference when its displayed mapping is written unchanged", () => {
    const original = subject({
      id: "table",
      kind: "decisionTable",
      with: { ref: "/input" },
    });
    expect(
      writeParam(original, projected, mapped({ ref: "/input/amount" })),
    ).toEqual([]);
    expect(original.step!["with"]).toEqual({ ref: "/input" });
  });
  it("removes one projected option while keeping the other mappings", () => {
    const original = subject({
      id: "table",
      kind: "decisionTable",
      with: { ref: "/input" },
    });
    const option = { ...projected, path: ["with", "tier"], required: false };
    expect(isDefault(original, option)).toBe(true);
    const removed = resetChanges(original, option).reduce(
      applyChange,
      original,
    );
    expect(removed.step!["with"]).toEqual({
      object: { amount: { ref: "/input/amount" } },
    });
    expect(isDefault(removed, option)).toBe(true);
    expect(readParam(removed, projected)).toEqual(
      mapped({ ref: "/input/amount" }),
    );
  });
  it("does not apply a stale projection to another whole reference", () => {
    const original = subject({
      id: "table",
      kind: "decisionTable",
      with: { ref: "/steps/table-input" },
    });
    expect(readParam(original, projected)).toEqual(ABSENT);
    expect(() => writeParam(original, projected, fixed(20))).toThrow(
      FormWriteError,
    );
    expect(original.step!["with"]).toEqual({ ref: "/steps/table-input" });
  });
});

describe("reading field values", () => {
  it("reads plain step fields as fixed values", () => {
    const wait: Step = { id: "wait-1", kind: "wait", durationSeconds: 60 };
    expect(
      readParam({ ...subject(wait), roots: [] }, spec(["durationSeconds"])),
    ).toEqual(fixed(60));
    expect(
      readParam({ ...subject(wait), roots: [] }, spec(["missing"])),
    ).toEqual(ABSENT);
  });
  it("reads data paths inside an expression as fixed or mapped", () => {
    expect(readParam(subject(), spec(["with", "query", "limit"]))).toEqual(
      fixed(25),
    );
    expect(readParam(subject(), spec(["with", "path", "customerId"]))).toEqual(
      mapped(pathRef),
    );
    expect(readParam(subject(), spec(["with", "query", "status"]))).toEqual(
      ABSENT,
    );
  });
  it("reads an expression field itself", () => {
    const transform: Step = {
      id: "t",
      kind: "transform",
      value: { literal: "Hi" },
    };
    const s = { ...subject(transform), roots: [["value"]] };
    expect(readParam(s, spec(["value"]))).toEqual(fixed("Hi"));
  });
  it("reads workflow and action scopes", () => {
    expect(
      readParam(subject(), spec(["metadata", "name"], { scope: "workflow" })),
    ).toEqual(fixed("untitled-workflow"));
    expect(
      readParam(
        subject(step, { method: "GET" }),
        spec(["method"], { scope: "action" }),
      ),
    ).toEqual(fixed("GET"));
  });
  it("lists the entries of an object, a list or a whole mapping", () => {
    expect(readEntries(subject(), spec(["with", "query"]))).toEqual({
      kind: "object",
      keys: ["limit"],
    });
    expect(readEntries(subject(), spec(["with", "headers"]))).toEqual({
      kind: "absent",
    });
    const whole: Step = { ...step, with: { ref: "/input" } };
    expect(readEntries(subject(whole), spec(["with"]))).toEqual({
      kind: "mapped",
      expression: { ref: "/input" },
    });
  });
});

describe("writing field values", () => {
  it("writes inside an expression and keeps the untouched parts as they were", () => {
    const [change] = writeParam(
      subject(),
      spec(["with", "query", "limit"]),
      fixed(50),
    );
    expect(change.scope).toBe("step");
    expect(change.path).toEqual(["with"]);
    const written = change.value as { object: Record<string, unknown> };
    expect(written.object["path"]).toBe(
      (step["with"] as { object: Record<string, unknown> }).object["path"],
    );
    expect(written.object["query"]).toEqual({ literal: { limit: 50 } });
  });
  it("writes a mapping and deletes a key", () => {
    const [mapping] = writeParam(
      subject(),
      spec(["with", "query", "status"]),
      mapped({ ref: "/input/status" }),
    );
    expect(
      (mapping.value as { object: Record<string, unknown> }).object["query"],
    ).toEqual({
      object: { limit: { literal: 25 }, status: { ref: "/input/status" } },
    });
    const [deletion] = writeParam(
      subject(),
      spec(["with", "query", "limit"]),
      ABSENT,
    );
    expect(
      (deletion.value as { object: Record<string, unknown> }).object["query"],
    ).toEqual({
      literal: {},
    });
  });
  it("writes an expression field as a literal or a mapping, and deletes it", () => {
    const transform: Step = {
      id: "t",
      kind: "transform",
      value: { literal: "Hi" },
    };
    const s = { ...subject(transform), roots: [["value"]] };
    expect(writeParam(s, spec(["value"]), fixed("Bye"))).toEqual([
      { scope: "step", path: ["value"], value: { literal: "Bye" } },
    ]);
    expect(writeParam(s, spec(["value"]), ABSENT)).toEqual([
      { scope: "step", path: ["value"], value: undefined },
    ]);
  });
  it("writes plain, workflow and action values as JSON", () => {
    expect(writeParam(subject(), spec(["connection"]), fixed("http"))).toEqual([
      { scope: "step", path: ["connection"], value: "http" },
    ]);
    expect(
      writeParam(
        subject(),
        spec(["spec", "timeoutSeconds"], { scope: "workflow" }),
        fixed(600),
      ),
    ).toEqual([
      { scope: "workflow", path: ["spec", "timeoutSeconds"], value: 600 },
    ]);
    expect(
      writeParam(
        subject(),
        spec(["method"], { scope: "action" }),
        fixed("POST"),
      ),
    ).toEqual([{ scope: "action", path: ["method"], value: "POST" }]);
  });
  it("refuses a part of an input mapped as a whole", () => {
    const whole: Step = { ...step, with: { ref: "/input" } };
    expect(() =>
      writeParam(subject(whole), spec(["with", "query", "limit"]), fixed(1)),
    ).toThrow(
      new FormWriteError(
        "This input is mapped as a whole. Choose Map field by field first.",
      ),
    );
  });
  it("writes nothing when the key to remove is already absent", () => {
    const bare: Step = {
      id: "get-orders",
      kind: "action",
      uses: "order-intake.get-orders@1.0.0",
    };
    const s = subject(bare);
    const changes = writeParam(s, spec(["with", "query", "limit"]), ABSENT);
    expect(changes).toEqual([]);
    expect(changes.reduce(applyChange, s).step).toBe(bare);
    expect(writeParam(s, spec(["connection"]), ABSENT)).toEqual([]);
  });
  it("leaves the YAML untouched when a removed key is missing from a literal", () => {
    const literal: Step = {
      id: "get-orders",
      kind: "action",
      uses: "order-intake.get-orders@1.0.0",
      with: { literal: { query: { limit: 25 } } },
    };
    expect(
      writeParam(subject(literal), spec(["with", "query", "status"]), ABSENT),
    ).toEqual([]);
  });
});

describe("defaults", () => {
  it("treats an absent or default value as the default", () => {
    const limit = spec(["with", "query", "limit"], { default: 25 });
    expect(isDefault(subject(), limit)).toBe(true);
    expect(
      isDefault(subject(), spec(["with", "query", "limit"], { default: 10 })),
    ).toBe(false);
    expect(isDefault(subject(), spec(["with", "query", "absent"]))).toBe(true);
    expect(isDefault(subject(), spec(["with", "path", "customerId"]))).toBe(
      false,
    );
  });
  it("resets by deleting, or by writing the default of a required field", () => {
    const wait: Step = { id: "wait-1", kind: "wait", durationSeconds: 300 };
    const s = { ...subject(wait), roots: [] };
    expect(
      resetChanges(
        s,
        spec(["durationSeconds"], { default: 60, required: true }),
      ),
    ).toEqual([{ scope: "step", path: ["durationSeconds"], value: 60 }]);
    expect(resetChanges(s, spec(["durationSeconds"], { default: 60 }))).toEqual(
      [{ scope: "step", path: ["durationSeconds"], value: undefined }],
    );
  });
  it("writes the default on reset when the form asks to keep the key", () => {
    const wait: Step = { id: "wait-1", kind: "wait", durationSeconds: 300 };
    const s = { ...subject(wait), roots: [] };
    const keep = { default: 60, whenRemoved: "default" as const };
    expect(resetChanges(s, spec(["durationSeconds"], keep))).toEqual([
      { scope: "step", path: ["durationSeconds"], value: 60 },
    ]);
    expect(
      resetChanges(
        s,
        spec(["durationSeconds"], { default: 60, whenRemoved: "delete" }),
      ),
    ).toEqual([{ scope: "step", path: ["durationSeconds"], value: undefined }]);
    // Without a default there is nothing to write.
    expect(
      resetChanges(s, spec(["durationSeconds"], { whenRemoved: "default" })),
    ).toEqual([{ scope: "step", path: ["durationSeconds"], value: undefined }]);
  });
  it("writes the default of an expression field as a fixed value", () => {
    const keep = spec(["with"], { default: {}, whenRemoved: "default" });
    expect(resetChanges(subject(), keep)).toEqual([
      { scope: "step", path: ["with"], value: { literal: {} } },
    ]);
  });
});

describe("applying a change the way a saved edit does", () => {
  const stepOf: Step = {
    id: "get-orders",
    kind: "action",
    uses: "order-intake.get-orders@1.0.0",
    connection: "old",
    durationSeconds: 60,
    rows: ["a"],
  };
  const bare: Step = {
    id: "get-orders",
    kind: "action",
    uses: "order-intake.get-orders@1.0.0",
  };
  const message = "That list has no item at that position.";

  it("applies step changes exactly as applyEdits does", () => {
    const changes: FormChange[] = [
      { scope: "step", path: ["connection"], value: "http" },
      { scope: "step", path: ["durationSeconds"], value: undefined },
      { scope: "step", path: ["rows", 0], value: "z" },
    ];
    const applied = changes.reduce(applyChange, subject(stepOf));
    const saved = applyEdits(
      subject(stepOf).workflow,
      "get-orders",
      changes.map(({ path, value }) => ({ path, value })),
    );
    expect(applied.step).toEqual(saved.spec.steps[0]);
  });
  it("applies workflow changes exactly as applyEdits does", () => {
    const applied = applyChange(subject(stepOf), {
      scope: "workflow",
      path: ["metadata", "name"],
      value: "renamed",
    });
    const saved = applyEdits(subject(stepOf).workflow, "get-orders", [
      { scope: "workflow", path: ["metadata", "name"], value: "renamed" },
    ]);
    expect(applied.workflow).toEqual(saved);
  });
  it("refuses a list position that a saved edit refuses", () => {
    const beyond: FormChange = { scope: "step", path: ["rows", 2], value: "b" };
    expect(() => applyChange(subject(stepOf), beyond)).toThrow(
      new EditError(message),
    );
    expect(() => applyChange(subject(bare), beyond)).toThrow(
      new EditError(message),
    );
    expect(() =>
      applyEdits(subject(bare).workflow, "get-orders", [
        { path: ["rows", 2], value: "b" },
      ]),
    ).toThrow(new EditError(message));
  });
});

describe("lists and keyed rows", () => {
  const mail: Step = {
    id: "send",
    kind: "action",
    uses: "flow.send@1.0.0",
    with: {
      object: {
        to: { array: [{ literal: "a@x.test" }, { ref: "/input/email" }] },
      },
    },
  };
  const to = spec(["with", "to"], { type: "list" });
  it("reads and rewrites a list inside an expression", () => {
    expect(readList(subject(mail), to)).toEqual([
      fixed("a@x.test"),
      mapped({ ref: "/input/email" }),
    ]);
    const [change] = writeList(subject(mail), to, (items) => [
      items[1],
      items[0],
      fixed("c@x.test"),
    ]);
    expect(change.value).toEqual({
      object: {
        to: {
          array: [
            { ref: "/input/email" },
            { literal: "a@x.test" },
            { literal: "c@x.test" },
          ],
        },
      },
    });
  });
  it("reads and rewrites keyed rows in their order", () => {
    const shape: Step = {
      id: "shape",
      kind: "transform",
      value: {
        object: {
          customer: { ref: "/input/customerId" },
          amount: { ref: "/input/amount" },
        },
      },
    };
    const value = { ...spec(["value"], { type: "keyValue" }) };
    const s = { ...subject(shape), roots: [["value"]] };
    expect(readKeyed(s, value).map(([key]) => key)).toEqual([
      "customer",
      "amount",
    ]);
    const [change] = writeKeyed(s, value, (entries) => [
      entries[1],
      entries[0],
      ["note", fixed("hi")],
    ]);
    expect(
      Object.keys((change.value as { object: Record<string, unknown> }).object),
    ).toEqual(["amount", "customer", "note"]);
  });
  it("rewrites a plain list as JSON", () => {
    const task: Step = {
      id: "review",
      kind: "humanTask",
      decisions: ["approve", "reject"],
    };
    const answers = spec(["decisions"], { type: "list" });
    const [change] = writeList(
      { ...subject(task), roots: [] },
      answers,
      (items) => [...items, fixed("escalate")],
    );
    expect(change).toEqual({
      scope: "step",
      path: ["decisions"],
      value: ["approve", "reject", "escalate"],
    });
  });
});

it("retains opaque row expressions and refuses duplicate keyed writes", () => {
  const expression = { ref: "/input/name", literal: "opaque" };
  const shape: Step = {
    id: "shape",
    kind: "transform",
    value: {
      object: { a: expression, b: { literal: { nested: [1, true, null] } } },
    },
  };
  const s = { ...subject(shape), roots: [["value"]] };
  const field = spec(["value"], { type: "keyValue" });
  const [change] = writeKeyed(s, field, (entries) => [
    ...entries,
    ["c", fixed("added")],
  ]);
  expect(change.value).toEqual({
    object: {
      a: expression,
      b: { literal: { nested: [1, true, null] } },
      c: { literal: "added" },
    },
  });
  expect(() =>
    writeKeyed(s, field, (entries) => [...entries, ["a", fixed("duplicate")]]),
  ).toThrow(FormWriteError);
});

it("preserves untouched nested expression shapes when adding or reordering rows", () => {
  const original = { object: { nested: { literal: { a: [1, true, null] } } } };
  const s = {
    ...subject({
      id: "shape",
      kind: "transform",
      value: { array: [original, { ref: "/input/name" }] },
    }),
    roots: [["value"]],
  };
  const list = spec(["value"], { type: "list" });
  expect(
    writeList(s, list, (items) => [items[1], items[0], fixed("new")])[0].value,
  ).toEqual({ array: [{ ref: "/input/name" }, original, { literal: "new" }] });
});
