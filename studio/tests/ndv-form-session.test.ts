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
  ChangeDetectorRef,
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import { TestEventPane } from "../src/app/editor/ndv/panes/test-event-pane";
import { InputPane } from "../src/app/editor/ndv/panes/input-pane";
import { TaskForm } from "../src/app/task-form";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { ResourceField } from "../src/app/editor/ndv/params/resource-field";
import { ParamField } from "../src/app/editor/ndv/params/param-field";
import { ParameterForm } from "../src/app/editor/ndv/params/param-form";
import { ListField } from "../src/app/editor/ndv/params/list-field";
import type { Choice, ParamSpec } from "../src/app/editor/ndv/registry";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { StructuredCanvasAdapter } from "../src/app/model";

vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
afterAll(() => vi.unstubAllGlobals());

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
  it("discards a pending resource list when its catalog is replaced", async () => {
    const f = fieldFixture();
    f.field.spec = signal({
      ...f.spec,
      id: "action",
      type: "resource",
    }) as never;
    f.field.loadChoices();
    f.host.actionVersions = [{ name: "replacement", version: "2.0.0" }];
    expect(f.field.loadChoices()).toEqual([]);
    expect(f.pending).toHaveLength(2);
    f.pending[0].resolve([{ value: "old@1.0.0", label: "Old" }]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([]);
    f.pending[1].resolve([
      { value: "replacement@2.0.0", label: "Replacement" },
    ]);
    await Promise.resolve();
    expect(f.field.loadChoices()).toEqual([
      { value: "replacement@2.0.0", label: "Replacement" },
    ]);
    f.destroy();
  });
  it("does not apply an old Retry action to a replacement choices owner", async () => {
    const f = fieldFixture();
    f.field.loadChoices();
    f.pending[0].reject(new Error("offline"));
    await Promise.resolve();
    const retry = f.field.line().fix!;
    f.field.spec = signal({ ...f.spec, path: ["replacement"] }) as never;
    f.field.loadChoices();
    expect(f.pending).toHaveLength(2);
    retry();
    expect(f.pending).toHaveLength(2);
    f.destroy();
  });
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

it("keeps an action field and its loaded choices across renders, then refreshes a changed catalog", async () => {
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
  expect(next.spec.choices).toBe(first.spec.choices);
  expect(form.fieldKey(next)).toBe(key);
  spec.set(next.spec);
  entry.set(next);
  expect(field.loadChoices()).toEqual([
    { value: "sql.lookup@1.0.0", label: "sql.lookup@1.0.0" },
  ]);
  f.host.actionVersions = [{ name: "sql.lookup", version: "2.0.0" }] as never;
  expect(field.loadChoices()).toEqual([]);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  expect(field.loadChoices()).toEqual([
    { value: "sql.lookup@2.0.0", label: "sql.lookup@2.0.0" },
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

describe("structural reset safety", () => {
  function fixture(target = "route-by-value") {
    const f = session(target);
    const fields = f.form.state("parameters").fields;
    const collection =
      target === "route-by-value"
        ? f.form
            .children(fields[0].spec)
            .shown.find((entry) => entry.spec.id === "cases")!.spec
        : fields.find((entry) => entry.spec.id === "branches")!.spec;
    const row = f.form.itemSpec(
      collection,
      target === "route-by-value" ? 0 : "ledger",
      collection.item!,
    );
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const field = runInInjectionContext(injector, () => new ParamField());
    field.session = signal(f.form) as never;
    field.spec = signal(row) as never;
    field.entry = signal(f.form.childEntry(row)) as never;
    field.row = signal(true) as never;
    return { ...f, collection, row, field, injector };
  }
  function replace(
    f: ReturnType<typeof fixture>,
    changes: Record<string, unknown>,
  ) {
    f.model.update(
      f.form.target,
      JSON.stringify({ ...f.form.controller.step(f.form.target), ...changes }),
    );
  }
  it("disables a populated path's Reset with the removal reason and refuses its callback invoked directly", () => {
    const f = fixture();
    const reset = f.field
      .menuItems()
      .find((item) => item.label === "Reset to default")!;
    expect(reset.disabled).toBe(true);
    expect(reset.detail).toBe("Move or delete its steps first.");
    const before = JSON.stringify(f.model.definition);
    reset.run();
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual(["Move or delete its steps first."]);
    f.injector.destroy();
  });
  for (const target of ["route-by-value", "fan-out"]) {
    it(`refuses direct populated structural row reset in ${target}`, () => {
      const f = fixture(target);
      const before = JSON.stringify(f.model.definition);
      f.form.clear(f.row);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.notes).toEqual(["Move or delete its steps first."]);
      f.injector.destroy();
    });
    it(`refuses whole populated collection reset in ${target}`, () => {
      const f = fixture(target);
      const before = JSON.stringify(f.model.definition);
      f.form.clear(f.collection);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.notes).toEqual(["Move or delete its steps first."]);
      f.injector.destroy();
    });
  }
  it("keeps the last required empty path and presents its minimum-count reason", () => {
    const f = fixture();
    replace(f, {
      cases: [{ when: { literal: true }, steps: [], output: { literal: {} } }],
    });
    const reset = f.field
      .menuItems()
      .find((item) => item.label === "Reset to default")!;
    expect(reset.disabled).toBe(true);
    expect(reset.detail).toBe("Keep at least 1.");
    const before = JSON.stringify(f.model.definition);
    f.form.clear(f.row);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual(["Keep at least 1."]);
    f.injector.destroy();
  });
  it("keeps the last required named branch", () => {
    const f = fixture("fan-out");
    replace(f, {
      branches: { ledger: { steps: [], output: { literal: { note: true } } } },
    });
    const before = JSON.stringify(f.model.definition);
    f.form.clear(f.row);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual(["Keep at least 1."]);
    f.injector.destroy();
  });
  it("preserves nested steps when Otherwise is reset", () => {
    const f = fixture();
    const otherwise = f.form
      .children(f.form.state("parameters").fields[0].spec)
      .shown.find((entry) => entry.spec.id === "otherwise")!.spec;
    const before = JSON.stringify(f.model.definition);
    f.form.clear(otherwise);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual(["Move or delete its steps first."]);
    f.injector.destroy();
  });
  it("allows an empty Otherwise group to reset its result and Undo restores it", () => {
    const f = fixture();
    replace(f, {
      default: { steps: [], output: { literal: { changed: true } } },
    });
    const otherwise = f.form
      .children(f.form.state("parameters").fields[0].spec)
      .shown.find((entry) => entry.spec.id === "otherwise")!.spec;
    const before = JSON.stringify(f.model.definition);
    f.form.clear(otherwise);
    expect(f.form.controller.step(f.form.target)!["default"]).toEqual({
      steps: [],
      output: { literal: {} },
    });
    expect(f.notes).toEqual([]);
    f.model.undo();
    expect(JSON.stringify(f.model.definition)).toBe(before);
    f.injector.destroy();
  });
  for (const target of ["route-by-value", "fan-out"]) {
    it(`uses guarded removal for an empty structural row in ${target} and Undo restores it`, () => {
      const f = fixture(target);
      const row = f.form.itemSpec(
        f.collection,
        target === "route-by-value" ? 1 : "audit",
        f.collection.item!,
      );
      const before = JSON.stringify(f.model.definition);
      f.form.clear(row);
      const step = f.form.controller.step(target)!;
      expect(
        target === "route-by-value"
          ? (step["cases"] as unknown[]).length
          : Object.keys(step["branches"] as object).length,
      ).toBe(1);
      expect(f.model.nodes().map((node) => node.step.id)).toContain(
        target === "route-by-value" ? "notify-sales" : "wait-a-minute",
      );
      expect(f.notes).toEqual([]);
      f.model.undo();
      expect(JSON.stringify(f.model.definition)).toBe(before);
      f.injector.destroy();
    });
    it(`refuses wholesale empty collection replacement in ${target} and directs edits to its controls`, () => {
      const f = fixture(target);
      replace(
        f,
        target === "route-by-value"
          ? {
              cases: [
                { when: { literal: true }, steps: [], output: { literal: {} } },
                {
                  when: { literal: false },
                  steps: [],
                  output: { literal: {} },
                },
              ],
            }
          : {
              branches: {
                custom: { steps: [], output: { literal: { note: true } } },
              },
            },
      );
      const before = JSON.stringify(f.model.definition);
      f.form.clear(f.collection);
      expect(JSON.stringify(f.model.definition)).toBe(before);
      expect(f.notes).toEqual([
        target === "route-by-value"
          ? "Use Add path or a path's Remove button."
          : "Use Add branch or a branch's Remove button.",
      ]);
      f.injector.destroy();
    });
  }
  it("resets a populated path's condition and output without deleting its steps", () => {
    const f = fixture();
    const cases = structuredClone(
      f.form.controller.step(f.form.target)!["cases"] as Record<
        string,
        unknown
      >[],
    );
    cases[0]["output"] = { literal: { changed: true } };
    replace(f, { cases });
    const children = f.form.children(f.row).shown;
    f.form.clear(children.find((entry) => entry.spec.id === "case.when")!.spec);
    f.form.clear(
      children.find((entry) => entry.spec.id === "case.output")!.spec,
    );
    const stored = (
      f.form.controller.step(f.form.target)!["cases"] as Record<
        string,
        unknown
      >[]
    )[0];
    expect(stored["steps"]).toEqual(cases[0]["steps"]);
    expect(stored["when"]).toBeUndefined();
    expect(stored["output"]).toEqual({ literal: {} });
    expect(f.notes).toEqual([]);
    f.injector.destroy();
  });
  it("keeps a whole collection already at its default as a no-op", () => {
    const f = fixture("fan-out");
    replace(f, { branches: f.collection.default });
    const before = JSON.stringify(f.model.definition);
    const revision = f.model.revision;
    f.form.clear(f.collection);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.model.revision).toBe(revision);
    expect(f.notes).toEqual([]);
    f.injector.destroy();
  });
  it("retains model reference protection when an empty branch row is reset", () => {
    const f = fixture("fan-out");
    f.model.update(
      "summarize",
      JSON.stringify({
        ...f.form.controller.step("summarize"),
        prompt: { ref: "/steps/fan-out/output/audit" },
      }),
    );
    const row = f.form.itemSpec(f.collection, "audit", f.collection.item!);
    const before = JSON.stringify(f.model.definition);
    f.form.clear(row);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual([
      "Update references to this branch in source first.",
    ]);
    f.injector.destroy();
  });
  it("does not replace or rename empty referenced branches through whole Reset", () => {
    const f = fixture("fan-out");
    replace(f, {
      branches: {
        ledger: { steps: [], output: { literal: {} } },
        audit: { steps: [], output: { literal: {} } },
      },
    });
    f.model.update(
      "summarize",
      JSON.stringify({
        ...f.form.controller.step("summarize"),
        prompt: { ref: "/steps/fan-out/output/audit" },
      }),
    );
    const before = JSON.stringify(f.model.definition);
    f.form.clear(f.collection);
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual(["Use Add branch or a branch's Remove button."]);
    f.injector.destroy();
  });
  it("does not reset through an obsolete structural item descriptor", () => {
    const f = fixture();
    const before = JSON.stringify(f.model.definition);
    f.form.clear({ ...f.row, label: "Obsolete" });
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.notes).toEqual([]);
    f.injector.destroy();
  });
  it("allows an ordinary object-list data row containing a steps property to reset", () => {
    const f = setup();
    const list: ParamSpec = {
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
        children: () => [],
      },
    };
    const descriptor = f.controller.descriptor("summarize")!;
    f.controller.descriptor = () => ({
      ...descriptor,
      form: () => ({ fields: [list] }),
    });
    f.session.write(list, {
      mode: "fixed",
      value: [{ steps: ["API data"] }, { steps: ["Other data"] }],
    });
    f.session.clear(f.session.itemSpec(list, 0, list.item!));
    expect(f.session.read(list)).toEqual({
      mode: "fixed",
      value: [{ steps: ["Other data"] }],
    });
    expect(f.errors).toEqual([]);
  });
});

describe("manual sample form lifetime", () => {
  function fixture(schema?: unknown) {
    const f = setup();
    f.model.updateWorkflow({
      ...f.model.definition,
      spec: {
        ...f.model.definition.spec,
        inputSchema: schema ?? {
          type: "object",
          required: ["enabled"],
          properties: {
            enabled: { type: "boolean" },
            name: { type: "string" },
          },
        },
      },
    });
    const injector = Injector.create({
      providers: [
        { provide: ElementRef, useValue: new ElementRef({}) },
        { provide: ChangeDetectorRef, useValue: { markForCheck() {} } },
      ],
    });
    const pane = runInInjectionContext(injector, () => new TestEventPane());
    pane.session = signal(f.session) as never;
    const owner = pane.forms()[0];
    const form = runInInjectionContext(injector, () => new TaskForm());
    form.schema = signal(owner.schema) as never;
    form.schemaRoot = signal(owner.root) as never;
    form.initialData = signal(owner.initial) as never;
    form.dataChange.subscribe((data) => pane.changed(data, owner));
    return { ...f, injector, pane, owner, form };
  }
  it.each([false, true])(
    "preserves the original reference root when wrapped (array=%s)",
    (array) => {
      const Value = { type: "string", title: "Display name" };
      const f = fixture(
        array
          ? {
              $defs: { Value },
              type: "array",
              items: { $ref: "#/$defs/Value" },
            }
          : { $defs: { Value }, $ref: "#/$defs/Value" },
      );
      f.form.ngOnChanges({ schema: {} as never });
      const field = f.form.rootFields()[0];
      expect(field.kind).toBe(array ? "list" : "text");
      if (array) expect(f.form.member(field)?.kind).toBe("text");
      else expect(field.label).toBe("Display name");
      f.form.write(["value"], array ? ["Alice"] : "Alice");
      expect(f.controller.testEvent()).toEqual(array ? ["Alice"] : "Alice");
      expect(f.pane.forms()[0]).toBe(f.owner);
      expect(f.pane.forms()[0].root).toBe(f.owner.root);
      f.injector.destroy();
    },
  );
  it("keeps schema and initial data identities through its own edits", () => {
    const f = fixture();
    f.pane.changed({ name: "A" }, f.owner);
    expect(f.pane.forms()[0]).toBe(f.owner);
    f.pane.changed({ name: "Alice" }, f.owner);
    expect(f.pane.forms()[0].initial).toBe(f.owner.initial);
    expect(f.controller.testEvent()).toEqual({ name: "Alice" });
    f.injector.destroy();
  });
  it.each(["close", "workflow", "profile", "account", "generate", "destroy"])(
    "refuses a real TaskForm default microtask after %s",
    async (change) => {
      const f = fixture();
      const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
      f.form.ngOnChanges({ schema: {} as never });
      if (change === "close") f.close();
      if (change === "workflow") f.model.clearHistory();
      if (change === "profile") f.host.profile = { name: "Other" } as never;
      if (change === "account") f.host.api = {} as never;
      if (change === "generate") f.pane.generate();
      if (change === "destroy") f.injector.destroy();
      await Promise.resolve();
      expect(f.controller.testEvent()).toEqual(
        change === "generate" ? { enabled: true, name: "text" } : undefined,
      );
      if (change !== "destroy") f.injector.destroy();
      expect(warning.mock.calls).toEqual([]);
      warning.mockRestore();
    },
  );
  it("accepts current TaskForm default microtasks", async () => {
    const f = fixture();
    f.form.ngOnChanges({ schema: {} as never });
    await Promise.resolve();
    expect(f.controller.testEvent()).toEqual({ enabled: false });
    expect(f.pane.forms()[0]).toBe(f.owner);
    f.injector.destroy();
  });
});

describe("pane input source reconciliation", () => {
  it("drops a removed predecessor and reconciles when the target changes", () => {
    const f = setup();
    f.model.insert("transform", "root", undefined, {
      id: "last",
      value: { ref: "/input" },
    });
    const session = new FormSession(f.host, f.controller, openRequest("last"), {
      confirm: async () => true,
      announce() {},
    });
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const pane = runInInjectionContext(injector, () => new InputPane());
    const input = signal(session);
    pane.session = input as never;
    expect(pane.selectedSource()).toBe("step:summarize");
    pane.source.set("step:summarize");
    expect(pane.selectedSource()).toBe("step:summarize");
    f.model.remove("summarize");
    expect(pane.selectedSource()).toBe("input");
    f.model.undo();
    expect(pane.selectedSource()).toBe("input");
    input.set(f.session);
    expect(pane.selectedSource()).toBe("input");
    injector.destroy();
  });
});

describe("resource control ownership", () => {
  function resource() {
    const f = session("lookup"),
      form = f.form;
    const injector = Injector.create({
      providers: [
        {
          provide: ElementRef,
          useValue: new ElementRef({ querySelector: () => null }),
        },
      ],
    });
    const field = runInInjectionContext(injector, () => new ResourceField());
    const spec = signal(form.controller.parameters("lookup").fields[0]);
    field.session = signal(form) as never;
    field.spec = spec as never;
    field.controlId = signal("resource") as never;
    const choices = signal<Choice[]>([
      { value: "sql.lookup@1.0.0", label: "Lookup" },
    ]);
    field.choices = choices as never;
    return { ...f, form, field, spec, choices, injector };
  }
  it("keeps invalid name text through mode changes and list refresh, then accepts one Undoable write", () => {
    const f = resource();
    const before = f.model.source;
    f.field.choose("name");
    f.field.edit({ target: { value: "incomplete" } } as unknown as Event);
    f.field.typed();
    f.field.choose("list");
    f.choices.set([]);
    f.field.choose("name");
    expect(f.field.text()).toBe("incomplete");
    expect(f.field.problem()).toContain("name@1.2.0");
    expect(f.model.source).toBe(before);
    f.field.edit({
      target: { value: "orders.get@1.0.0-rc.2+build.1" },
    } as unknown as Event);
    f.field.typed();
    expect(f.form.read(f.spec())).toEqual({
      mode: "fixed",
      value: "orders.get@1.0.0-rc.2+build.1",
    });
    expect(f.field.hasDraft()).toBe(false);
    f.model.undo();
    expect(f.field.text()).toBe("sql.lookup@1.0.0");
    f.injector.destroy();
  });
  it("keeps the active text control when its list arrives during typing", () => {
    const f = resource();
    f.choices.set([]);
    expect(f.field.mode()).toBe("name");
    f.field.edit({ target: { value: "unfinished" } } as unknown as Event);
    f.choices.set([{ value: "sql.lookup@1.0.0", label: "Lookup" }]);
    expect(f.field.mode()).toBe("name");
    expect(f.field.text()).toBe("unfinished");
    f.injector.destroy();
  });
  it("retains a focused read-only resource mode without allowing an edit", () => {
    const f = resource();
    const before = f.model.source;
    f.choices.set([]);
    f.field.readOnly = signal("Read only") as never;
    (f.form.host as { editingLocked: boolean }).editingLocked = true;
    expect(f.field.mode()).toBe("name");
    f.field.retainMode();
    f.choices.set([{ value: "sql.lookup@1.0.0", label: "Lookup" }]);
    expect(f.field.mode()).toBe("name");
    f.field.choose("list");
    f.field.edit({ target: { value: "orders.get@1.0.0" } } as unknown as Event);
    f.field.typed();
    expect(f.field.mode()).toBe("name");
    expect(f.field.text()).toBe("sql.lookup@1.0.0");
    expect(f.model.source).toBe(before);
    f.injector.destroy();
  });
  for (const change of ["reopen", "destroy", "descriptor"] as const)
    it(`does not retain a resource mode for a focus event after ${change}`, () => {
      const f = resource();
      f.choices.set([]);
      expect(f.field.mode()).toBe("name");
      if (change === "reopen") f.model.clearHistory();
      if (change === "destroy") f.injector.destroy();
      if (change === "descriptor")
        f.spec.set({ ...f.spec(), label: "New action" });
      f.field.retainMode();
      f.choices.set([{ value: "sql.lookup@1.0.0", label: "Lookup" }]);
      expect(f.field.mode()).toBe("list");
      if (change !== "destroy") f.injector.destroy();
    });
  it("keeps a drive ID literal and refuses incomplete URLs without replacing it", () => {
    const f = resource();
    f.model.canvas = withOwnedAction(f.model.canvas, "sql.lookup@1.0.0", {
      kind: "connector",
      connector: "weave-google-drive@1.0.0",
      action: "read",
    });
    const spec = f.form.controller
      .parameters("lookup")
      .fields.find((field) => field.id === "itemId")!;
    f.spec.set(spec);
    f.field.edit({ target: { value: "01ABC" } } as unknown as Event);
    f.field.typed();
    expect(f.form.read(spec)).toEqual({ mode: "fixed", value: "01ABC" });
    f.field.choose("url");
    f.field.edit({ target: { value: "https://" } } as unknown as Event);
    f.field.typed();
    f.field.choose("id");
    f.field.choose("url");
    const parent = runInInjectionContext(f.injector, () => new ParamField());
    parent.session = signal(f.form) as never;
    parent.spec = f.spec as never;
    parent.entry = signal(f.form.resolve(spec)!) as never;
    Object.assign(parent, { resource: () => f.field });
    parent.setMode("mapped");
    expect(f.form.mode(spec)).toBe("fixed");
    expect(f.field.text()).toBe("https://");
    expect(f.form.read(spec)).toEqual({ mode: "fixed", value: "01ABC" });
    f.field.edit({
      target: { value: "https://drive.example/item/1" },
    } as unknown as Event);
    f.field.typed();
    expect(f.form.read(spec)).toEqual({
      mode: "fixed",
      value: "https://drive.example/item/1",
    });
    f.injector.destroy();
  });
  for (const change of [
    "read only",
    "reopen",
    "destroy",
    "descriptor",
  ] as const)
    it(`does not commit a resource buffer after ${change}`, () => {
      const f = resource();
      f.field.choose("name");
      f.field.edit({
        target: { value: "orders.get@1.0.0" },
      } as unknown as Event);
      if (change === "read only")
        (f.form.host as { editingLocked: boolean }).editingLocked = true;
      if (change === "reopen") f.model.clearHistory();
      if (change === "destroy") f.injector.destroy();
      if (change === "descriptor")
        f.spec.set({ ...f.spec(), label: "New action" });
      const before = f.model.source;
      f.field.typed();
      expect(f.model.source).toBe(before);
      if (change !== "destroy") f.injector.destroy();
    });
  it("does not announce a copied document after its viewer owner changes", async () => {
    const f = resource();
    (f.form.host.catalogContracts as Map<string, Record<string, unknown>>).set(
      "sql.lookup@1.0.0",
      {
        spec: { sideEffect: "read_only" },
      },
    );
    let finish!: () => void;
    const copied: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: (text: string) => {
          copied.push(text);
          return new Promise<void>((resolve) => {
            finish = resolve;
          });
        },
      },
    });
    const announce = vi.spyOn(f.form, "announce");
    f.field.open();
    const pending = f.field.copy();
    expect(copied).toEqual(["spec:\n  sideEffect: read_only\n"]);
    (f.form.host as { profile: unknown }).profile = { id: "replacement" };
    finish();
    await pending;
    expect(announce).not.toHaveBeenCalled();
    f.injector.destroy();
  });
  it("does not retain a document viewer across an account replacement", () => {
    const f = resource();
    (f.form.host.catalogContracts as Map<string, Record<string, unknown>>).set(
      "sql.lookup@1.0.0",
      {
        spec: { sideEffect: "read_only" },
      },
    );
    f.field.open();
    expect(f.field.viewer()?.yaml).toContain("read_only");
    (f.form.host as { profile: unknown }).profile = { id: "replacement" };
    expect(f.field.viewer()).toBeNull();
    expect(f.field.document()).toBeNull();
    f.injector.destroy();
  });
});
