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
import {
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import { ParamField } from "../src/app/editor/ndv/params/param-field";
import { ParameterForm } from "../src/app/editor/ndv/params/param-form";
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
