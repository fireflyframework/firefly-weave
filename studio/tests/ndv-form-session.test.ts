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
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import type { Workflow } from "../src/app/model";
import {
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import { ParamField } from "../src/app/editor/ndv/params/param-field";
import { ParameterForm } from "../src/app/editor/ndv/params/param-form";
import { ListField } from "../src/app/editor/ndv/params/list-field";
import type { Choice, ParamSpec } from "../src/app/editor/ndv/registry";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { StructuredCanvasAdapter } from "../src/app/model";

beforeAll(() => loadKindRegistrations());
function setup(prompt: unknown = { ref: "/input/name" }) {
  const model = new StructuredCanvasAdapter();
  model.insert("llm", "root", undefined, {
    id: "summarize",
    profile: "assistant",
    prompt,
  });
  model.clearHistory();
  let resolve!: (ok: boolean) => void;
  let current = true;
  const errors: string[] = [];
  const host = {
    model,
    editingLocked: false,
    profile: null,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    diagnostics: null,
    diagnosticsDefinition: null,
    label: (kind: string) => kind,
    can: () => true,
    notify: (text: string) => errors.push(text),
    refreshView: () => undefined,
    perform: (edit: () => void) => edit(),
    api: { request: async () => ({ features: [] }) },
  } as unknown as {
    -readonly [K in keyof StepDetailsHost]: StepDetailsHost[K];
  };
  const controller = new StepDetailsController(host);
  const session = new FormSession(
    host,
    controller,
    openRequest("summarize"),
    {
      confirm: () =>
        new Promise<boolean>((done) => {
          resolve = done;
        }),
      announce: () => undefined,
    },
    () => current,
  );
  const spec = controller
    .parameters("summarize")
    .fields.find((field) => field.id === "prompt")!;
  return {
    host,
    model,
    controller,
    session,
    spec,
    errors,
    answer: () => resolve(true),
    close: () => {
      current = false;
    },
  };
}
describe("parameter form ownership", () => {
  it("replaces a mapping with the default when the confirmation still belongs to the field", async () => {
    const fixture = setup();
    const pending = fixture.session.setMode(fixture.spec, "fixed");
    fixture.answer();
    await pending;
    expect(fixture.session.read(fixture.spec).mode).not.toBe("mapped");
    fixture.model.undo();
    expect(fixture.session.read(fixture.spec)).toEqual({
      mode: "mapped",
      expression: { ref: "/input/name" },
    });
  });
  for (const change of [
    "close",
    "reopen",
    "switch model",
    "edit",
    "read only",
  ] as const) {
    it(`discards a confirmation after ${change}`, async () => {
      const f = setup();
      const pending = f.session.setMode(f.spec, "fixed");
      if (change === "close") f.close();
      if (change === "reopen") f.model.clearHistory();
      if (change === "switch model") {
        const other = new StructuredCanvasAdapter();
        other.insert("llm", "root", undefined, {
          id: "summarize",
          profile: "assistant",
          prompt: { ref: "/input/other" },
        });
        f.host.model = other;
      }
      if (change === "edit")
        f.session.write(f.spec, {
          mode: "mapped",
          expression: { ref: "/input/new" },
        });
      if (change === "read only") f.host.editingLocked = true;
      const before = JSON.stringify(f.host.model.definition);
      f.answer();
      await pending;
      expect(JSON.stringify(f.host.model.definition)).toBe(before);
    });
  }
});

describe("asynchronous parameter choices", () => {
  function fieldFixture() {
    const f = setup();
    const pending: {
      resolve: (choices: Choice[]) => void;
      reject: (error: Error) => void;
    }[] = [];
    const spec: ParamSpec = {
      ...f.spec,
      type: "select",
      choices: () =>
        new Promise((resolve, reject) => pending.push({ resolve, reject })),
    };
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const field = runInInjectionContext(injector, () => new ParamField());
    field.session = signal(f.session) as never;
    field.spec = signal(spec) as never;
    field.entry = signal({
      spec,
      option: false,
      readOnly: null,
      disabled: null,
    }) as never;
    return { ...f, field, spec, pending, destroy: () => injector.destroy() };
  }
  it("clears obsolete choices and discards the old provider response when the field changes", async () => {
    const f = fieldFixture();
    f.field.loadChoices();
    f.pending[0].resolve([{ value: "old", label: "Old" }]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([{ value: "old", label: "Old" }]);
    f.field.spec = signal({ ...f.spec, path: ["context"] }) as never;
    expect(f.field.loadChoices()).toEqual([]);
    expect(f.pending).toHaveLength(2);
    f.field.spec = signal({ ...f.spec, path: ["other"] }) as never;
    f.field.loadChoices();
    f.pending[1].resolve([{ value: "late", label: "Late" }]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([]);
    f.pending[2].resolve([{ value: "current", label: "Current" }]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([
      { value: "current", label: "Current" },
    ]);
  });
  for (const change of [
    "close",
    "reopen",
    "switch model",
    "profile",
    "destroy",
  ] as const) {
    it(`discards late choices after ${change}`, async () => {
      const f = fieldFixture();
      f.field.loadChoices();
      if (change === "close") f.close();
      if (change === "reopen") f.model.clearHistory();
      if (change === "switch model")
        f.host.model = new StructuredCanvasAdapter();
      if (change === "profile") f.host.profile = { id: "other" } as never;
      if (change === "destroy") f.destroy();
      f.pending[0].resolve([{ value: "late", label: "Late" }]);
      await Promise.resolve();
      expect(f.field.loadChoices()).toEqual([]);
    });
  }
  it("shows a recoverable failure and accepts a successful retry", async () => {
    const f = fieldFixture();
    f.field.loadChoices();
    f.pending[0].reject(new Error("offline"));
    await Promise.resolve();
    expect(f.field.line()).toMatchObject({
      kind: "error",
      text: "Couldn't load the list.",
      fixLabel: "Retry",
    });
    f.field.line().fix!();
    f.field.loadChoices();
    f.pending[1].resolve([{ value: "ready", label: "Ready" }]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([{ value: "ready", label: "Ready" }]);
    expect(f.field.line().text).not.toContain("Couldn't load");
  });
});

describe("deferred literal template edits", () => {
  for (const change of ["close", "reopen", "edit", "read only"] as const) {
    it(`does not commit literal text on blur after ${change}`, () => {
      const f = setup();
      const injector = Injector.create({ providers: [] });
      const field = runInInjectionContext(injector, () => new FormulaField());
      field.session = signal(f.session) as never;
      field.spec = signal(f.spec) as never;
      field.beginEdit();
      field.typed({ target: { value: "Plain text" } } as unknown as Event);
      if (change === "close") f.close();
      if (change === "reopen") f.model.clearHistory();
      if (change === "edit")
        f.session.write(f.spec, {
          mode: "mapped",
          expression: { ref: "/input/new" },
        });
      if (change === "read only") f.host.editingLocked = true;
      const before = JSON.stringify(f.model.definition);
      field.leave();
      expect(JSON.stringify(f.model.definition)).toBe(before);
      injector.destroy();
    });
  }
});

describe("active mapped field ownership", () => {
  const template = (prefix: string) => ({
    op: { name: "concat", args: [{ literal: prefix }, { ref: "/input/name" }] },
  });
  async function fieldFixture() {
    const f = setup(template("Hello "));
    f.host.api = {
      request: async () => ({ features: ["text.concat"] }),
    } as never;
    await f.controller.loadFeatures();
    const injector = Injector.create({ providers: [] });
    const field = runInInjectionContext(injector, () => new FormulaField());
    const session = signal(f.session);
    const spec = signal(f.spec);
    field.session = session as never;
    field.spec = spec as never;
    return { ...f, field, sessionInput: session, specInput: spec, injector };
  }
  const type = (field: FormulaField, value: string) =>
    field.typed({ target: { value } } as unknown as Event);
  it("reconciles focused Undo and Redo and accepts continued typing", async () => {
    const f = await fieldFixture();
    f.field.beginEdit();
    type(f.field, "First {{ input.email }}");
    f.model.undo();
    expect(f.field.templateText()).toBe("Hello {{ input.name }}");
    f.model.redo();
    expect(f.field.templateText()).toBe("First {{ input.email }}");
    f.model.undo();
    type(f.field, "Next {{ input.customerId }}");
    expect(f.session.read(f.spec)).toMatchObject({
      expression: {
        op: { args: [{ literal: "Next " }, { ref: "/input/customerId" }] },
      },
    });
    f.injector.destroy();
  });
  it("discards another session's invalid template and picker buffers", async () => {
    const a = await fieldFixture();
    const b = setup(template("Other "));
    a.field.beginEdit();
    type(a.field, "Invalid {{ input.name");
    a.field.leave();
    a.field.picking.set(true);
    a.field.typedRef = "/input/previousDraft";
    a.sessionInput.set(b.session);
    a.specInput.set(b.spec);
    const before = b.session.read(b.spec);
    a.field.commitTyped();
    expect(b.session.read(b.spec)).toEqual(before);
    expect(a.field.templateText()).toBe("Other {{ input.name }}");
    expect(a.field.templateError()).toBe("");
    expect(a.field.picking()).toBe(false);
    expect(a.field.typedRef).toBe("");
    a.injector.destroy();
  });
  it("does not represent an extended ref expression as a pill", async () => {
    const f = await fieldFixture();
    const expression = { ref: "/input/name", literal: "hidden" };
    f.session.write(f.spec, { mode: "mapped", expression });
    expect(f.field.currentRef()).toBe("");
    expect(f.field.view()).toBe("formula");
    expect(f.field.formulaText()).toBe(JSON.stringify(expression));
    f.injector.destroy();
  });
  it("resets invalid local state when the current descriptor changes", async () => {
    const f = await fieldFixture();
    f.field.beginEdit();
    type(f.field, "Invalid {{ input.name");
    f.field.leave();
    f.field.beginPick();
    f.field.typedRef = "/input/previousDraft";
    f.specInput.set({ ...f.spec, label: "Updated prompt" });
    const before = f.session.read(f.spec);
    f.field.commitTyped();
    expect(f.session.read(f.spec)).toEqual(before);
    expect(f.field.templateText()).toBe("Hello {{ input.name }}");
    expect(f.field.templateError()).toBe("");
    expect(f.field.picking()).toBe(false);
    expect(f.field.typedRef).toBe("");
    f.injector.destroy();
  });
});

it("keeps a published action's rendered identity while fresh providers still load current catalog choices", async () => {
  const f = setup();
  f.model.insert("action", "root", undefined, {
    id: "lookup",
    uses: "sql.lookup@1.0.0",
  });
  f.host.actionVersions = [{ name: "sql.lookup", version: "1.0.0" }] as never;
  const session = new FormSession(f.host, f.controller, openRequest("lookup"), {
    confirm: async () => true,
    announce: () => undefined,
  });
  const first = session
    .state("parameters")
    .fields.find((entry) => entry.spec.id === "action")!;
  const injector = Injector.create({
    providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
  });
  const form = runInInjectionContext(injector, () => new ParameterForm());
  const field = runInInjectionContext(injector, () => new ParamField());
  form.session = signal(session) as never;
  field.session = signal(session) as never;
  const spec = signal(first.spec);
  const entry = signal(first);
  field.spec = spec as never;
  field.entry = entry as never;
  const key = form.fieldKey(first);
  field.loadChoices();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  expect(field.loadChoices()).toEqual([
    { value: "sql.lookup@1.0.0", label: "sql.lookup@1.0.0" },
  ]);
  f.session.write(f.spec, {
    mode: "mapped",
    expression: { ref: "/input/email" },
  });
  const next = session
    .state("parameters")
    .fields.find((entry) => entry.spec.id === "action")!;
  expect(next.spec.choices).not.toBe(first.spec.choices);
  expect(form.fieldKey(next)).toBe(key);
  spec.set(next.spec);
  entry.set(next);
  expect(field.loadChoices()).toEqual([]);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  expect(field.loadChoices()).toEqual([
    { value: "sql.lookup@1.0.0", label: "sql.lookup@1.0.0" },
  ]);
  injector.destroy();
});

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
    notify: (text: string) => void notes.push(text),
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
    announce: () => undefined,
  });
  return { model, form, notes };
}

describe("form sessions", () => {
  it("adds, moves and refuses to remove decision paths through the decision's branches", () => {
    const { model, form } = session("route-by-value");
    const cases = form.state("parameters").fields[0].spec.children!(
      form.controller.step("route-by-value")!,
      form.controller.kindContext(),
    )[0];
    expect(form.structure(cases)).toBe("cases");
    expect(form.removeReason(cases, 0)).toBe("Move or delete its steps first.");
    form.listAdd(cases);
    expect((model.definition.spec.steps[2]["cases"] as unknown[]).length).toBe(
      3,
    );
    form.listMove(cases, 2, 0);
    expect(
      (model.definition.spec.steps[2]["cases"] as { when?: unknown }[])[0].when,
    ).toBeUndefined();
  });
  it("labels a path with its condition and the step it leads to", () => {
    const { form } = session("route-by-value");
    expect(form.rowInfo(["cases", 0])).toEqual({
      chip: "amount > 1000",
      next: "notify-sales",
    });
    expect(form.rowInfo(["cases", 1])).toEqual({
      chip: "tier is gold",
      next: "summarize",
    });
    expect(form.rowInfo(["default"])).toEqual({
      chip: "Otherwise",
      next: "fan-out",
    });
  });
  it("renames an answer with its paths", () => {
    const { model, form } = session("notify-sales");
    const answers = form
      .state("parameters")
      .fields.find((f) => f.spec.id === "answers")!.spec;
    expect(form.structure(answers)).toBe("answers");
    form.listRename(answers, 0, "accept");
    const task = model.nodes().find((n) => n.step.id === "notify-sales")!.step;
    expect(task["decisions"]).toEqual(["accept", "reject"]);
  });
  it("collapses optional children at their default into add buttons", () => {
    const { form } = session("route-by-value");
    const paths = form.state("parameters").fields[0].spec;
    const otherwise = paths.children!(
      form.controller.step("route-by-value")!,
      form.controller.kindContext(),
    )[1];
    const { shown, collapsed } = form.children(otherwise);
    expect(shown).toEqual([]);
    expect(collapsed.map((c) => c.label)).toEqual(["Path result"]);
    form.expandChild(collapsed[0]);
    expect(form.children(otherwise).shown.map((e) => e.spec.label)).toEqual([
      "Path result",
    ]);
  });
});

describe("nested collection ownership", () => {
  function nested() {
    const f = setup();
    const child: ParamSpec = {
      id: "nested",
      path: ["message"],
      type: "text",
      label: "Message",
      mapping: "both",
    };
    const group: ParamSpec = {
      id: "group",
      path: ["prompt"],
      type: "fields",
      label: "Context",
      children: () => [child],
    };
    const descriptor = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [group] }),
    });
    f.session.write(group, {
      mode: "mapped",
      expression: { object: { message: { ref: "/input/name" } } },
    });
    expect(f.errors).toEqual([]);
    expect(f.session.read(group)).toEqual({
      mode: "mapped",
      expression: { object: { message: { ref: "/input/name" } } },
    });
    return { ...f, group, child: f.session.childSpec(child, group.path) };
  }
  it("returns a live nested mapping to Fixed and Undo restores it", async () => {
    const f = nested();
    const pending = f.session.setMode(f.child, "fixed");
    f.answer();
    await pending;
    expect(f.session.read(f.child).mode).not.toBe("mapped");
    f.model.undo();
    expect(f.session.read(f.child)).toEqual({
      mode: "mapped",
      expression: { ref: "/input/name" },
    });
  });
  it("maps a nested Fixed field without inventing path authority", async () => {
    const f = nested();
    f.session.clear(f.child);
    await f.session.setMode(f.child, "mapped");
    expect(f.session.mode(f.child)).toBe("mapped");
    const arbitrary = { ...f.child, path: ["prompt", "unknown"] };
    await f.session.setMode(arbitrary, "mapped");
    expect(f.session.mode(arbitrary)).toBe("fixed");
  });
  it("maps a whole structured descriptor and returns to rows using its original authority", async () => {
    const f = nested();
    f.session.mapWhole(f.group);
    expect(f.session.mode(f.group)).toBe("mapped");
    f.session.write(f.group, { mode: "mapped", expression: { ref: "/input" } });
    const pending = f.session.useRows(f.group);
    f.answer();
    await pending;
    expect(f.session.entries(f.group)).toEqual({ kind: "object", keys: [] });
  });
  for (const change of [
    "edit",
    "readOnly",
    "hidden",
    "schema",
    "close",
  ] as const)
    it(`refuses a delayed nested replacement after ${change}`, async () => {
      const f = nested();
      const pending = f.session.setMode(f.child, "fixed");
      if (change === "edit")
        f.session.write(f.child, {
          mode: "mapped",
          expression: { ref: "/input/new" },
        });
      if (change === "readOnly") f.host.editingLocked = true;
      if (change === "hidden")
        f.group.children = () => [
          { ...f.child, path: ["message"], showWhen: () => false },
        ];
      if (change === "schema")
        f.group.children = () => [
          { ...f.child, path: ["message"], type: "number" },
        ];
      if (change === "close") f.close();
      const before = JSON.stringify(f.model.definition);
      f.answer();
      await pending;
      expect(JSON.stringify(f.model.definition)).toBe(before);
    });
  it("preserves nested feature-gating", () => {
    const f = nested();
    f.group.children = () => [
      { ...f.child, path: ["message"], feature: "future.feature", default: "" },
    ];
    const parts = f.session.children(f.group);
    expect(parts.shown[0].disabled).toBe(
      "Update the platform to use this (future.feature).",
    );
  });
  for (const change of ["edit", "readOnly", "close"] as const)
    it(`refuses Use rows after ${change}`, async () => {
      const f = nested();
      f.session.write(f.group, {
        mode: "mapped",
        expression: { ref: "/input" },
      });
      const pending = f.session.useRows(f.group);
      if (change === "edit")
        f.session.write(f.group, {
          mode: "mapped",
          expression: { ref: "/input/new" },
        });
      if (change === "readOnly") f.host.editingLocked = true;
      if (change === "close") f.close();
      const before = JSON.stringify(f.model.definition);
      f.answer();
      await pending;
      expect(JSON.stringify(f.model.definition)).toBe(before);
    });
  it("refuses Use rows when a generated group schema changes during confirmation", async () => {
    const f = nested();
    f.session.write(f.group, { mode: "mapped", expression: { ref: "/input" } });
    const pending = f.session.useRows(f.group);
    f.group.children = () => [
      { ...f.child, path: ["message"], type: "number" },
    ];
    const before = JSON.stringify(f.model.definition);
    f.answer();
    await pending;
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  it("enforces maximum answer count at the mutation boundary", () => {
    const f = session("notify-sales");
    const answers = f.form
      .state("parameters")
      .fields.find((e) => e.spec.id === "answers")!.spec;
    f.form.setList(answers, () =>
      Array.from({ length: 33 }, (_, i) => ({
        mode: "fixed" as const,
        value: `answer-${i}`,
      })),
    );
    expect(f.form.list(answers)).toHaveLength(2);
  });
});

describe("collection mutation and row lifetime", () => {
  function rows() {
    const f = setup();
    const list: ParamSpec = {
      id: "rows",
      path: ["context"],
      type: "list",
      label: "Rows",
      item: {
        id: "item",
        path: [],
        type: "text",
        label: "Item",
        mapping: "both",
      },
    };
    const original = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...original,
      form: () => ({ fields: [list] }),
    });
    f.session.write(list, {
      mode: "mapped",
      expression: { array: [{ ref: "/input/a" }, { ref: "/input/b" }] },
    });
    return { ...f, list, child: f.session.itemSpec(list, 0, list.item!) };
  }
  it("refuses a pending replacement after moving a same-ID row", async () => {
    const f = rows();
    const pending = f.session.setMode(f.child, "fixed");
    f.session.listMove(f.list, 0, 1);
    const before = JSON.stringify(f.model.definition);
    f.answer();
    await pending;
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.session.list(f.list)).toEqual([
      { mode: "mapped", expression: { ref: "/input/b" } },
      { mode: "mapped", expression: { ref: "/input/a" } },
    ]);
  });
  it("remounts rows after reorder and removal while preserving identities on child typing", () => {
    const f = rows();
    const key = f.session.rowKey(f.list, 0);
    f.session.write(f.child, { mode: "fixed", value: "typing" });
    expect(f.session.rowKey(f.list, 0)).toBe(key);
    f.session.listMove(f.list, 0, 1);
    const moved = f.session.rowKey(f.list, 0);
    expect(moved).not.toBe(key);
    f.model.undo();
    expect(f.session.rowKey(f.list, 0)).not.toBe(moved);
    const undo = f.session.rowKey(f.list, 0);
    f.session.listRemove(f.list, 0);
    expect(f.session.rowKey(f.list, 0)).not.toBe(undo);
  });
  it("blocks nested writes under a read-only parent", () => {
    const f = rows();
    f.list.readOnly = () => "Managed by the platform.";
    const before = JSON.stringify(f.model.definition);
    f.session.write(f.child, { mode: "fixed", value: "wrong" });
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });
  it("keeps a canceled whole mapping unchanged", async () => {
    const f = rows();
    f.session.write(f.list, { mode: "mapped", expression: { ref: "/input" } });
    const form = new FormSession(
      f.host,
      f.controller,
      openRequest("summarize"),
      { confirm: async () => false, announce: () => undefined },
    );
    await form.useRows(f.list);
    expect(form.read(f.list)).toEqual({
      mode: "mapped",
      expression: { ref: "/input" },
    });
  });
});

it("does not transfer a row's empty Mapped draft after reorder", async () => {
  const f = setup();
  const list: ParamSpec = {
    id: "rows",
    path: ["context"],
    type: "list",
    label: "Rows",
    item: {
      id: "item",
      path: [],
      type: "text",
      label: "Item",
      mapping: "both",
    },
  };
  const original = f.controller.descriptor("summarize")!;
  f.controller.descriptor = () => ({
    ...original,
    form: () => ({ fields: [list] }),
  });
  f.session.write(list, { mode: "fixed", value: ["first", "second"] });
  const child = f.session.itemSpec(list, 0, list.item!);
  await f.session.setMode(child, "mapped");
  expect(f.session.mode(child)).toBe("mapped");
  f.session.listMove(list, 0, 1);
  expect(f.session.mode(child)).toBe("fixed");
});

it("keeps a keyed row's name when its value returns to Fixed", async () => {
  const f = setup();
  const group: ParamSpec = {
    id: "values",
    path: ["context"],
    type: "keyValue",
    label: "Values",
  };
  const original = f.controller.descriptor("summarize")!;
  f.controller.descriptor = () => ({
    ...original,
    form: () => ({ fields: [group] }),
  });
  f.session.write(group, {
    mode: "mapped",
    expression: { object: { customer: { ref: "/input/name" } } },
  });
  const child = f.session.keyedSpec(group, "customer");
  const pending = f.session.setMode(child, "fixed");
  f.answer();
  await pending;
  expect(f.session.keyed(group)).toEqual([
    ["customer", { mode: "fixed", value: "" }],
  ]);
});

it("clears row drafts when Undo restores a different row at the same path", async () => {
  const f = setup();
  const list: ParamSpec = {
    id: "rows",
    path: ["context"],
    type: "list",
    label: "Rows",
    item: {
      id: "item",
      path: [],
      type: "text",
      label: "Item",
      mapping: "both",
    },
  };
  const descriptor = f.controller.descriptor("summarize")!;
  f.controller.descriptor = () => ({
    ...descriptor,
    form: () => ({ fields: [list] }),
  });
  f.session.write(list, { mode: "fixed", value: ["first", "second"] });
  f.session.rowKey(list, 0);
  f.session.listMove(list, 0, 1);
  f.session.rowKey(list, 0);
  const child = f.session.itemSpec(list, 0, list.item!);
  await f.session.setMode(child, "mapped");
  f.model.undo();
  f.session.rowKey(list, 0);
  expect(f.session.mode(child)).toBe("fixed");
});

it("resolves generated nested object children inside a list without repeating their relative prefix", async () => {
  const { paramsFromSchema } = await import(
    "../src/app/editor/ndv/params/schema-params"
  );
  const f = setup();
  const schema = {
    type: "object",
    required: ["items"],
    properties: {
      items: {
        type: "array",
        items: {
          type: "object",
          required: ["address"],
          properties: {
            address: {
              type: "object",
              required: ["city"],
              properties: {
                city: { type: "string" },
                secret: { type: "string", "x-secret": true },
              },
            },
          },
        },
      },
    },
  };
  const form = paramsFromSchema(schema, ["context"], { idPrefix: "input." });
  const descriptor = f.controller.descriptor("summarize")!;
  f.controller.descriptor = () => ({ ...descriptor, form: () => form });
  const list = form.fields[0];
  f.session.write(list, {
    mode: "mapped",
    expression: {
      array: [
        { object: { address: { object: { city: { ref: "/input/name" } } } } },
      ],
    },
  });
  const row = f.session.itemSpec(list, 0, list.item!);
  const address = f.session.children(row).shown[0].spec;
  const children = f.session.children(address).shown;
  expect(children.map((entry) => entry.spec.label)).toEqual(["City"]);
  const city = children[0].spec;
  expect(city.path).toEqual(["context", "items", 0, "address", "city"]);
  const pending = f.session.setMode(city, "fixed");
  f.answer();
  await pending;
  expect(f.session.read(city).mode).not.toBe("mapped");
});

describe("session collection expression preservation", () => {
  const original = { object: { nested: { literal: { a: [1, true, null] } } } };
  const opaque = {
    op: { name: "custom.opaque", args: [{ ref: "/input/name" }] },
    extension: true,
  };
  function fixture(type: "list" | "keyValue") {
    const f = setup();
    const spec: ParamSpec = {
      id: "values",
      path: ["prompt"],
      type,
      label: "Values",
      mapping: "both",
      item: {
        id: "item",
        path: [],
        type: "json",
        label: "Item",
        mapping: "both",
      },
    };
    const descriptor = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [spec] }),
    });
    const expression =
      type === "list"
        ? { array: [original, opaque, { ref: "/input/name" }] }
        : { object: { original, opaque, reference: { ref: "/input/name" } } };
    f.session.write(spec, { mode: "mapped", expression });
    return { ...f, spec, expression };
  }
  for (const operation of ["move", "add", "remove"] as const) {
    it(`retains exact untouched list expressions when rows ${operation}`, () => {
      const f = fixture("list");
      if (operation === "move") f.session.listMove(f.spec, 2, 0);
      if (operation === "add") f.session.listAdd(f.spec);
      if (operation === "remove") f.session.listRemove(f.spec, 2);
      const stored = f.controller.step("summarize")!["prompt"] as {
        array: unknown[];
      };
      expect(stored.array[operation === "move" ? 1 : 0]).toEqual(original);
      expect(stored.array[operation === "move" ? 2 : 1]).toEqual(opaque);
      expect(f.errors).toEqual([]);
      f.model.undo();
      expect(f.controller.step("summarize")!["prompt"]).toEqual(f.expression);
    });
  }
  for (const operation of ["rename", "move", "add", "remove"] as const) {
    it(`retains exact untouched keyed expressions when rows ${operation}`, () => {
      const f = fixture("keyValue");
      if (operation === "rename")
        f.session.listRename(f.spec, "reference", "renamed");
      if (operation === "move") f.session.listMove(f.spec, 2, 0);
      if (operation === "add")
        f.session.setKeyed(f.spec, (entries) => [
          ...entries,
          ["added", { mode: "fixed", value: "New" }],
        ]);
      if (operation === "remove") f.session.listRemove(f.spec, "reference");
      const stored = f.controller.step("summarize")!["prompt"] as {
        object: Record<string, unknown>;
      };
      expect(stored.object["original"]).toEqual(original);
      expect(stored.object["opaque"]).toEqual(opaque);
      expect(f.errors).toEqual([]);
      f.model.undo();
      expect(f.controller.step("summarize")!["prompt"]).toEqual(f.expression);
    });
  }
  for (const type of ["list", "keyValue"] as const) {
    it(`runs a ${type} update once and refuses excessive counts without writing`, () => {
      const f = fixture(type);
      f.spec.maxItems = 3;
      let calls = 0;
      const before = JSON.stringify(f.model.definition);
      if (type === "list")
        f.session.setList(f.spec, (items) => {
          calls++;
          return [...items, { mode: "fixed", value: "extra" }];
        });
      else
        f.session.setKeyed(f.spec, (entries) => {
          calls++;
          return [...entries, ["extra", { mode: "fixed", value: "extra" }]];
        });
      expect(calls).toBe(1);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.errors).toEqual(["Keep at most 3."]);
      f.spec.maxItems = 4;
      calls = 0;
      if (type === "list")
        f.session.setList(f.spec, (items) => {
          calls++;
          return [...items, { mode: "fixed", value: "extra" }];
        });
      else
        f.session.setKeyed(f.spec, (entries) => {
          calls++;
          return [...entries, ["extra", { mode: "fixed", value: "extra" }]];
        });
      expect(calls).toBe(1);
      expect(
        type === "list"
          ? f.session.list(f.spec).length
          : f.session.keyed(f.spec).length,
      ).toBe(4);
    });
  }
});

describe("grouped list item state and permissions", () => {
  function fixture(feature?: string) {
    const f = setup();
    let reason: string | null = null;
    const spec: ParamSpec = {
      id: "rows",
      path: ["prompt"],
      type: "list",
      label: "Rows",
      mapping: "both",
      item: {
        id: "item",
        path: [],
        type: "fields",
        label: "Item",
        mapping: "both",
        readOnly: () => reason,
        feature,
        children: () => [
          {
            id: "name",
            path: ["name"],
            type: "text",
            label: "Name",
            required: true,
            mapping: "both",
          },
        ],
      },
    };
    const descriptor = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [spec] }),
    });
    f.session.write(spec, {
      mode: "mapped",
      expression: { array: [{ object: { name: { literal: "Before" } } }] },
    });
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const list = runInInjectionContext(injector, () => new ListField());
    list.session = signal(f.session) as never;
    list.spec = signal(spec) as never;
    const row = f.session.itemSpec(spec, 0, spec.item!);
    const child = f.session.children(row).shown[0].spec;
    return {
      ...f,
      spec,
      row,
      child,
      list,
      injector,
      lock: (next: string | null) => {
        reason = next;
      },
    };
  }
  it("uses the live item reason and refuses nested writes until the item unlocks", () => {
    const f = fixture();
    f.lock("This item is locked.");
    expect(f.session.readOnly(f.list.entry(f.row))).toBe(
      "This item is locked.",
    );
    expect(f.session.line(f.row, f.list.entry(f.row))).toEqual({
      kind: "reason",
      text: "This item is locked.",
    });
    const before = JSON.stringify(f.model.definition);
    f.session.write(f.child, { mode: "fixed", value: "Refused" });
    expect(JSON.stringify(f.model.definition)).toBe(before);
    f.lock(null);
    f.session.write(f.child, { mode: "fixed", value: "Allowed" });
    expect(f.session.read(f.child)).toEqual({
      mode: "fixed",
      value: "Allowed",
    });
    f.injector.destroy();
  });
  it("reports an item feature reason and permits nested edits only when that feature exists", () => {
    const f = fixture("text.concat");
    expect(f.session.readOnly(f.list.entry(f.row))).toBe(
      "Update the platform to use this (text.concat).",
    );
    const before = JSON.stringify(f.model.definition);
    f.session.write(f.child, { mode: "fixed", value: "Refused" });
    expect(JSON.stringify(f.model.definition)).toBe(before);
    const context = f.controller.kindContext.bind(f.controller);
    f.controller.kindContext = () => ({
      ...context(),
      features: ["text.concat"],
    });
    expect(f.session.readOnly(f.list.entry(f.row))).toBeNull();
    f.session.write(f.child, { mode: "fixed", value: "Allowed" });
    expect(f.session.read(f.child)).toEqual({
      mode: "fixed",
      value: "Allowed",
    });
    f.injector.destroy();
  });
});
