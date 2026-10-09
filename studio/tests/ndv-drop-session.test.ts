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
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { newConnectorRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
beforeAll(() => loadKindRegistrations());
function session(target: string) {
  const model = new StructuredCanvasAdapter();
  model.replace(
    parse(
      readFileSync(
        new URL("./fixtures/step-details.yaml", import.meta.url),
        "utf8",
      ),
    ) as Workflow,
  );
  const notes: string[] = [];
  const actions: { label: string; run: () => void }[] = [];
  const announcements: string[] = [];
  const host = {
    model,
    error: "",
    perform(edit: () => void) {
      try {
        edit();
      } catch (error) {
        host.error = (error as Error).message;
      }
    },
    notify: (text: string, action?: { label: string; run: () => void }) => {
      notes.push(text);
      if (action) actions.push(action);
    },
    undo: () => model.undo(),
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: {} as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
    refreshView: () => undefined,
  } as unknown as StepDetailsHost;
  const controller = new StepDetailsController(host);
  const form = new FormSession(host, controller, openRequest(target), {
    confirm: async () => true,
    announce: (text: string) => announcements.push(text),
  });
  return { model, form, notes, actions, announcements, host, controller };
}
const email = {
  ref: "/input/email",
  breadcrumb: "untrusted breadcrumb",
  schema: { type: "number" },
};
const options = { caret: null };
const valueField = (f: ReturnType<typeof session>) =>
  f.form.form("parameters").fields.find((s) => s.id === "value")!;

describe("live mapping edits", () => {
  it("remaps a row reference with one Undo, but refuses unavailable replacement data", () => {
    const f = session("check-customer"),
      parent = valueField(f);
    const spec = f.form.keyedSpec(parent, "customer");
    const before = JSON.stringify(f.model.definition);
    expect(
      f.form.applyDrop(
        spec,
        { ...email, ref: "/steps/lookup/output" },
        options,
      ),
    ).toBe(false);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.form.applyDrop(spec, email, options)).toBe(true);
    expect(f.form.read(spec)).toEqual({
      mode: "mapped",
      expression: { ref: "/input/email" },
    });
    expect(f.notes).toContain(
      "Replaced the previous mapping with Input › email.",
    );
    f.actions.find((action) => action.label === "Undo")!.run();
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  it("shows a refusal before hovering over an opaque expression", () => {
    const f = session("summarize");
    const spec = f.form
      .form("parameters")
      .fields.find((s) => s.id === "prompt")!;
    f.form.write(spec, {
      mode: "mapped",
      expression: {
        op: { name: "add", args: [{ literal: 1 }, { literal: 2 }] },
      },
    });
    expect(f.form.dropFit(spec, email).fit).toBe("refused");
    expect(f.form.dropFit(spec, email).text).toContain("Edit as YAML");
  });
  it("adds a mapped row and undoes the entire addition in one step", () => {
    const f = session("check-customer"),
      spec = valueField(f);
    const before = JSON.stringify(f.model.definition);
    expect(f.form.applyDrop(spec, email, options)).toBe(true);
    expect(f.form.keyed(spec).at(-1)).toEqual([
      "email",
      { mode: "mapped", expression: { ref: "/input/email" } },
    ]);
    expect(f.announcements).toEqual(["Mapped Input › email to Fields"]);
    f.model.undo();
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  for (const ref of [
    "/steps/lookup/output",
    "/steps/notify-sales/output",
    "/input/not-present",
    "/input/a~2b",
  ]) {
    it(`refuses unavailable data ${ref}`, () => {
      const f = session("check-customer"),
        spec = valueField(f);
      const before = JSON.stringify(f.model.definition);
      expect(f.form.applyDrop(spec, { ...email, ref }, options)).toBe(false);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.announcements.join()).not.toContain("Mapped");
    });
  }
  it("maps only expression leaves inside structural paths", () => {
    const f = session("route-by-value");
    const fields = f.form.mapTargets();
    expect(fields.length).toBeGreaterThan(0);
    expect(fields.some((s) => s.path.join("/") === "cases")).toBe(false);
    expect(fields.some((s) => s.path.join("/") === "cases/0")).toBe(false);
    expect(fields.some((s) => s.path.join("/") === "cases/0/when")).toBe(true);
    for (const target of ["fan-out", "wait-for-payment"]) {
      const next = session(target);
      expect(next.form.mapTargets().some((s) => next.form.structure(s))).toBe(
        false,
      );
    }
  });
  for (const failure of [
    "readonly",
    "closed",
    "account",
    "limit",
    "whole",
    "unsupported",
  ] as const) {
    it(`does not claim a row was added after ${failure}`, () => {
      const f = session("check-customer");
      let spec = valueField(f);
      if (failure === "readonly")
        Object.assign(f.host, { editingLocked: true });
      if (failure === "closed") f.model.clearHistory();
      if (failure === "account")
        Object.assign(f.host, { stepDataScope: "other-account" });
      if (failure === "limit") {
        spec = { ...spec, maxItems: 2 };
        const descriptor = f.controller.descriptor("check-customer")!;
        f.controller.descriptor = () => ({
          ...descriptor,
          form: () => ({ fields: [spec] }),
        });
      }
      if (failure === "whole" || failure === "unsupported") {
        const step = f.controller.step("check-customer")!;
        f.model.update(
          step.id,
          JSON.stringify({
            ...step,
            value:
              failure === "whole"
                ? { ref: "/input" }
                : { object: {}, other: "keep" },
          }),
        );
      }
      const before = JSON.stringify(f.model.definition);
      expect(f.form.applyDrop(spec, email, options)).toBe(false);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.announcements.join()).not.toMatch(/Mapped|Added/);
    });
  }
  it("adds all escaped keys once and undoes the batch", () => {
    const f = session("check-customer"),
      spec = valueField(f);
    f.model.updateWorkflow({
      ...f.model.definition,
      spec: {
        ...f.model.definition.spec,
        inputSchema: {
          type: "object",
          properties: {
            "a/b": { type: "string" },
            "a~b": { type: "number" },
            customer: { type: "string" },
          },
        },
      },
    });
    const before = JSON.stringify(f.model.definition);
    expect(f.form.addAllFields(spec)).toBe(true);
    expect(f.form.keyed(spec).slice(-2)).toEqual([
      ["a/b", { mode: "mapped", expression: { ref: "/input/a~1b" } }],
      ["a~b", { mode: "mapped", expression: { ref: "/input/a~0b" } }],
    ]);
    expect(f.form.addAllFields(spec)).toBe(false);
    f.model.undo();
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  it("allows a known mismatch with truthful warning and canonical metadata", () => {
    const f = session("summarize");
    const spec: ParamSpec = {
      id: "prompt",
      path: ["prompt"],
      type: "number",
      label: "Amount",
      mapping: "both",
    };
    const descriptor = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [spec] }),
    });
    expect(f.form.applyDrop(spec, email, options)).toBe(true);
    expect(f.form.read(spec)).toEqual({
      mode: "mapped",
      expression: { ref: "/input/email" },
    });
    expect(f.announcements.join()).toContain("Text doesn't fit a Number field");
    expect(f.announcements.join()).not.toContain("untrusted");
  });
});

describe("mapping destination ownership", () => {
  for (const later of ["edit", "account", "workflow"] as const) {
    it(`does not undo a different ${later} through an old replacement toast`, () => {
      const f = session("summarize");
      const spec: ParamSpec = {
        id: "prompt",
        path: ["prompt"],
        label: "Amount",
        type: "number",
        mapping: "both",
      };
      const descriptor = f.controller.descriptor("summarize")!;
      f.controller.descriptor = () => ({
        ...descriptor,
        form: () => ({ fields: [spec] }),
      });
      f.form.write(spec, { mode: "fixed", value: 25 });
      expect(f.form.applyDrop(spec, email, options)).toBe(true);
      const undo = f.actions.find((action) => action.label === "Undo")!;
      expect(undo).toBeDefined();
      if (later === "edit")
        f.model.updateWorkflow({
          ...f.model.definition,
          metadata: { ...f.model.definition.metadata, name: "Changed name" },
        });
      if (later === "account")
        Object.assign(f.host, { stepDataScope: "another-account" });
      if (later === "workflow")
        f.model.replace({
          ...f.model.definition,
          metadata: {
            ...f.model.definition.metadata,
            name: "Another workflow",
          },
        });
      const before = JSON.stringify(f.model.definition);
      undo.run();
      expect(JSON.stringify(f.model.definition)).toBe(before);
    });
  }
  it("does not offer additions that would replace a whole mapping", () => {
    const f = session("check-customer"),
      spec = valueField(f);
    f.form.write(spec, { mode: "mapped", expression: { ref: "/input" } });
    expect(f.form.mapTargets()).toEqual([]);
  });
  it("keeps the target owner invalid after a row moves", () => {
    const f = session("check-customer"),
      spec = valueField(f);
    const child = f.form.keyedSpec(spec, "customer");
    const owns = f.form.owns(child);
    expect(owns()).toBe(true);
    f.form.listMove(spec, 0, 1);
    expect(owns()).toBe(false);
  });
  it("refuses a stale contract and preserves the current collection", () => {
    const f = session("check-customer"),
      spec = valueField(f);
    const descriptor = f.controller.descriptor("check-customer")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [{ ...spec, mapping: "fixed" }] }),
    });
    const before = JSON.stringify(f.model.definition);
    expect(f.form.applyDrop(spec, email, options)).toBe(false);
    expect(f.form.addAllFields(spec)).toBe(false);
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  it("keeps Add all fields atomic when capacity is too small", () => {
    const f = session("check-customer");
    const spec = { ...valueField(f), maxItems: 3 };
    const descriptor = f.controller.descriptor("check-customer")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [spec] }),
    });
    const before = JSON.stringify(f.model.definition);
    expect(f.form.addAllFields(spec)).toBe(false);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.announcements.join()).not.toContain("Added");
    expect(f.notes).toContain("Keep at most 3.");
  });
});

it("refuses fixed-only collections even when their storage sits under an expression root", () => {
  const f = session("check-customer");
  const spec: ParamSpec = { ...valueField(f), mapping: "fixed" };
  const descriptor = f.controller.descriptor("check-customer")!;
  f.controller.descriptor = () => ({
    ...descriptor,
    form: () => ({ fields: [spec] }),
  });
  const before = JSON.stringify(f.model.definition);
  expect(f.form.applyDrop(spec, email, options)).toBe(false);
  expect(f.form.addAllFields(spec)).toBe(false);
  expect(JSON.stringify(f.model.definition)).toBe(before);
});

describe("mapped list additions", () => {
  for (const id of ["to", "cc", "bcc"]) {
    it(`adds to the registered ${id} recipient list without whole-list mapping`, () => {
      const f = session("lookup");
      const step = f.controller.step("lookup")!;
      f.model.update(
        "lookup",
        JSON.stringify({
          ...step,
          uses: "flow.send-email@1.0.0",
          with: {
            object: {
              to: { array: [{ literal: "retain@example.invalid" }] },
              cc: { array: [{ literal: "copy@example.invalid" }] },
              bcc: { array: [{ literal: "private@example.invalid" }] },
              subject: { literal: "Test" },
              text: { literal: "Message" },
            },
          },
        }),
      );
      f.model.canvas = withOwnedAction(
        f.model.canvas,
        "flow.send-email@1.0.0",
        newConnectorRecipe("weave-email@1.0.0", "send"),
      );
      const spec = f.form
        .state("parameters")
        .fields.find((entry) => entry.spec.id === id)!.spec;
      expect(spec.mapping).toBeUndefined();
      expect(spec.item?.mapping).toBe("both");
      const before = JSON.stringify(f.model.definition);
      const rows = f.form.list(spec);
      expect(f.form.mapTargets().some((target) => target.id === id)).toBe(true);
      expect(f.form.mappingReason(spec)).toBeNull();
      expect(f.form.applyDrop(spec, email, options)).toBe(true);
      expect(f.form.list(spec)).toEqual([
        ...rows,
        { mode: "mapped", expression: { ref: "/input/email" } },
      ]);
      f.model.undo();
      expect(JSON.stringify(f.model.definition)).toBe(before);
      f.model.redo();
      expect(f.form.list(spec)).toHaveLength(rows.length + 1);
    });
  }
  function listSession(
    item: Partial<ParamSpec> = {},
    parent: Partial<ParamSpec> = {},
  ) {
    const f = session("check-customer");
    const spec: ParamSpec = {
      id: "value",
      path: ["value"],
      type: "list",
      label: "Items",
      mapping: "both",
      ...parent,
      item: {
        id: "item",
        path: [],
        type: "text",
        label: "Item",
        mapping: "both",
        ...item,
      },
    };
    const descriptor = f.controller.descriptor("check-customer")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [spec] }),
    });
    f.form.write(spec, { mode: "fixed", value: ["keep"] });
    return { ...f, spec };
  }
  for (const [name, guard] of [
    ["read-only", { readOnly: () => "Item is locked" }],
    ["unavailable feature", { feature: "not.available" }],
    ["fixed-only", { mapping: "fixed" }],
    ["action-scoped", { scope: "action" }],
    ["workflow-scoped", { scope: "workflow" }],
  ] as const) {
    it(`refuses an append to a ${name} item just as it refuses direct mapping`, () => {
      const f = listSession(guard);
      const before = JSON.stringify(f.model.definition);
      const child = f.form.itemSpec(f.spec, 0, f.spec.item!);
      expect(f.form.applyDrop(child, email, options)).toBe(false);
      expect(f.form.mappingReason(f.spec)).toBeTruthy();
      expect(f.form.applyDrop(f.spec, email, options)).toBe(false);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.announcements.join()).not.toContain("Mapped Input");
    });
  }
  it("rechecks the live item guard after displaying an eligible list", () => {
    let locked = false;
    const f = listSession({
      readOnly: () => (locked ? "Item is locked" : null),
    });
    expect(f.form.mappingReason(f.spec)).toBeNull();
    const before = JSON.stringify(f.model.definition);
    locked = true;
    expect(f.form.applyDrop(f.spec, email, options)).toBe(false);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.announcements.join()).not.toContain("Mapped Input");
  });
  it("keeps the append descriptor private and refuses out-of-scope data and full lists", () => {
    const f = listSession({}, { maxItems: 1 });
    const before = JSON.stringify(f.model.definition);
    const missing = f.form.itemSpec(f.spec, 1, f.spec.item!);
    expect(f.form.resolve(missing)).toBeNull();
    expect(f.form.applyDrop(missing, email, options)).toBe(false);
    expect(
      f.form.applyDrop(
        f.spec,
        { ...email, ref: "/steps/lookup/output" },
        options,
      ),
    ).toBe(false);
    expect(f.form.applyDrop(f.spec, email, options)).toBe(false);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.announcements.join()).not.toContain("Mapped Input");
  });
});
