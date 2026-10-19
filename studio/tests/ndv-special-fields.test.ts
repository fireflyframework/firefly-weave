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
// The URL of Call an API and a decision's conditions, as one open form sees
// them: which fields exist, what a drop writes, and what it refuses.
import "@angular/compiler";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
// The schema field's module loads the schema designer, whose dialog listens on document.
vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import { Injector, runInInjectionContext, signal } from "@angular/core";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import {
  guardedInference,
  STALE_EXAMPLE,
} from "../src/app/editor/ndv/params/schema-field";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when it loads.
    documentElement: { style: {} },
  }),
);
beforeAll(() => loadKindRegistrations());
afterAll(() => vi.unstubAllGlobals());

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
    notify: (text: string) => notes.push(text),
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
    inferSchema: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
    refreshView: () => undefined,
  } as unknown as StepDetailsHost & { editingLocked: boolean };
  const controller = new StepDetailsController(host);
  const form = new FormSession(host, controller, openRequest(target), {
    confirm: async () => true,
    announce: (text: string) => announcements.push(text),
  });
  return { model, form, notes, announcements, host };
}

const customerPath = {
  object: { path: { object: { customerId: { ref: "/input/customerId" } } } },
};

/** The lookup step as a Call an API step whose action this workflow owns. */
function apiSession(pathTemplate: string, stepWith: unknown = customerPath) {
  const f = session("lookup");
  const step = f.model.nodes().find((node) => node.step.id === "lookup")!.step;
  f.model.update("lookup", JSON.stringify({ ...step, with: stepWith }));
  f.model.canvas = withOwnedAction(f.model.canvas, "sql.lookup@1.0.0", {
    kind: "http",
    method: "GET",
    pathTemplate,
    statuses: [200],
    timeoutSeconds: 30,
  });
  const url = f.form.form("parameters").fields.find((s) => s.id === "url")!;
  const saved = () =>
    JSON.stringify({ definition: f.model.definition, canvas: f.model.canvas });
  const pathValue = (key: string) =>
    f.form.read({
      id: "probe",
      path: ["with", "path", key],
      type: "text",
      label: key,
    });
  return { ...f, url, saved, pathValue };
}

const drag = (ref: string) => ({
  ref,
  breadcrumb: "untrusted breadcrumb",
  schema: {},
});
const end = { caret: null };

describe("the URL of Call an API", () => {
  it("offers one field per placeholder, bound in the step's input", () => {
    const f = apiSession("/customers/{customerId}/orders/{orderId}");
    const parts = f.form.urlParts(f.url);
    expect(
      parts.map((part) => [
        part.id,
        part.path,
        part.type,
        part.mapping,
        part.required,
      ]),
    ).toEqual([
      [
        "url.path.customerId",
        ["with", "path", "customerId"],
        "text",
        "both",
        true,
      ],
      ["url.path.orderId", ["with", "path", "orderId"], "text", "both", true],
    ]);
    expect(f.form.read(parts[0])).toEqual({
      mode: "mapped",
      expression: { ref: "/input/customerId" },
    });
    expect(f.form.write(parts[1], { mode: "fixed", value: "o-1" })).toBe(true);
    expect(f.pathValue("orderId")).toEqual({ mode: "fixed", value: "o-1" });
    // A path value without a placeholder is not a field of this form.
    const other: ParamSpec = {
      ...parts[1],
      id: "url.path.other",
      path: ["with", "path", "other"],
      label: "other",
    };
    expect(f.form.resolve(other)).toBeNull();
    expect(f.form.write(other, { mode: "fixed", value: "x" })).toBe(false);
    // The path is always a template: no Fixed | Mapped of its own.
    expect(f.form.mode(f.url)).toBeNull();
    expect(f.form.mapTargets().map((target) => target.id)).toEqual(
      expect.arrayContaining([
        "url",
        "url.path.customerId",
        "url.path.orderId",
      ]),
    );
  });

  it("drops data into the path as a placeholder and maps it, in one undo step", () => {
    const f = apiSession("/customers/{customerId}/orders");
    const before = f.saved();
    expect(f.form.mappingReason(f.url)).toBeNull();
    expect(f.form.dropFit(f.url, drag("/input/email")).fit).toBe("fits");
    expect(f.form.applyDrop(f.url, drag("/input/email"), end)).toBe(true);
    expect(f.form.read(f.url)).toEqual({
      mode: "fixed",
      value: "/customers/{customerId}/orders/{email}",
    });
    expect(f.pathValue("email")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/email" },
    });
    expect(f.pathValue("customerId")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/customerId" },
    });
    expect(f.announcements).toEqual(["Mapped Input › email to URL"]);
    f.model.undo();
    expect(f.saved()).toBe(before);
  });

  it("puts the placeholder at the caret, as a segment of its own", () => {
    const f = apiSession("/customers/orders", { object: {} });
    expect(
      f.form.applyDrop(f.url, drag("/input/customerId"), { caret: 11 }),
    ).toBe(true);
    expect(f.form.read(f.url)).toEqual({
      mode: "fixed",
      value: "/customers/{customerId}/orders",
    });
  });

  it("never takes a name the step's input still holds", () => {
    const f = apiSession("/customers/");
    expect(f.form.applyDrop(f.url, drag("/input/customerId"), end)).toBe(true);
    expect(f.form.read(f.url)).toEqual({
      mode: "fixed",
      value: "/customers/{customerId-2}",
    });
    expect(f.pathValue("customerId")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/customerId" },
    });
    expect(f.pathValue("customerId-2")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/customerId" },
    });
  });

  it("refuses drops while the path values are mapped as a whole, and changes nothing", () => {
    for (const stepWith of [
      { object: { path: { ref: "/input" } } },
      { ref: "/input" },
    ]) {
      const f = apiSession("/customers/", stepWith);
      const reason =
        "The path values are mapped as a whole. Edit them with Edit as YAML in the More menu.";
      expect(f.form.urlParts(f.url)).toEqual([]);
      expect(f.form.mappingReason(f.url)).toBe(reason);
      expect(f.form.dropFit(f.url, drag("/input/email"))).toEqual({
        fit: "refused",
        text: reason,
      });
      const before = f.saved();
      expect(f.form.applyDrop(f.url, drag("/input/email"), end)).toBe(false);
      expect(f.saved()).toBe(before);
      expect(f.announcements).toEqual([reason]);
      expect(f.form.mapTargets().some((target) => target.id === "url")).toBe(
        false,
      );
    }
  });

  it("refuses data the step can't read and a path that isn't this workflow's", () => {
    const f = apiSession("/customers/");
    const before = f.saved();
    expect(f.form.dropFit(f.url, drag("/steps/lookup/output")).fit).toBe(
      "refused",
    );
    expect(f.form.applyDrop(f.url, drag("/steps/lookup/output"), end)).toBe(
      false,
    );
    (f.host as { editingLocked: boolean }).editingLocked = true;
    expect(f.form.applyDrop(f.url, drag("/input/email"), end)).toBe(false);
    expect(f.saved()).toBe(before);
    expect(f.announcements.join()).not.toContain("Mapped");
  });

  it("keeps the path's values in step with its placeholders while typing, in one undo step", () => {
    const f = apiSession("/customers/{customerId}/orders", {
      object: {
        path: {
          object: {
            customerId: { ref: "/input/customerId" },
            legacy: { literal: "kept" },
          },
        },
      },
    });
    const before = f.saved();
    const snapshot = f.form.urlSnapshot(f.url);
    expect(
      f.form.typeUrl(f.url, "/customers/{customerI/orders", snapshot),
    ).toBe(true);
    expect(f.pathValue("customerId")).toEqual({ mode: "absent" });
    expect(f.pathValue("legacy")).toEqual({ mode: "fixed", value: "kept" });
    expect(f.form.urlParts(f.url)).toEqual([]);
    expect(
      f.form.typeUrl(f.url, "/customers/{customerId}/orders", snapshot),
    ).toBe(true);
    expect(f.pathValue("customerId")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/customerId" },
    });
    // An emptied path stays a path: the action keeps its pathTemplate.
    expect(f.form.typeUrl(f.url, "", snapshot)).toBe(true);
    expect(f.form.read(f.url)).toEqual({ mode: "fixed", value: "" });
    expect(f.pathValue("legacy")).toEqual({ mode: "fixed", value: "kept" });
    f.model.undo();
    expect(f.saved()).toBe(before);
  });

  it("types only into a live, editable path", () => {
    const f = apiSession("/customers/{customerId}");
    const snapshot = f.form.urlSnapshot(f.url);
    const before = f.saved();
    (f.host as { editingLocked: boolean }).editingLocked = true;
    expect(f.form.typeUrl(f.url, "/x", snapshot)).toBe(false);
    (f.host as { editingLocked: boolean }).editingLocked = false;
    expect(f.form.typeUrl({ ...f.url, label: "Other" }, "/x", snapshot)).toBe(
      false,
    );
    expect(f.saved()).toBe(before);
  });
});

describe("a decision's conditions", () => {
  function conditions() {
    const f = session("route-by-value");
    const paths = f.form.form("parameters").fields[0];
    const cases = f.form
      .children(paths)
      .shown.find((entry) => entry.spec.id === "cases")!.spec;
    const item = f.form.itemSpec(cases, 1, cases.item!);
    const when = f.form
      .children(item)
      .shown.find((entry) => entry.spec.id === "case.when")!.spec;
    return { ...f, when };
  }

  it("take dropped data the path can read, and nothing else", () => {
    const f = conditions();
    expect(f.form.mode(f.when)).toBeNull();
    expect(f.form.dropFit(f.when, drag("/input/amount"))).toEqual({
      fit: "fits",
      text: "Fits",
    });
    expect(f.form.conditionDrop(f.when, drag("/input/amount"))).toMatchObject({
      entry: { ref: "/input/amount", label: "Amount", typeLabel: "Number" },
    });
    const outside = drag("/steps/route-by-value/output");
    expect(f.form.dropFit(f.when, outside).fit).toBe("refused");
    expect(f.form.conditionDrop(f.when, outside)).toEqual({
      reason: "This field cannot read that data.",
    });
    expect(f.form.mapTargets().some((spec) => spec.id === "case.when")).toBe(
      true,
    );
  });

  it("take into their formula only dropped data the path can read", () => {
    const f = conditions();
    const injector = Injector.create({ providers: [] });
    const field = runInInjectionContext(injector, () => new FormulaField());
    field.session = signal(f.form) as never;
    field.spec = signal(f.when) as never;
    field.controlId = signal("when-control") as never;
    const before = JSON.stringify(f.model.definition);
    field.dropText({ ref: "/steps/route-by-value/output", pos: 0 });
    expect(JSON.stringify(f.model.definition)).toBe(before);
    expect(f.announcements).toEqual(["This field cannot read that data."]);
    field.dropText({ ref: "/input/amount", pos: 0 });
    expect(JSON.stringify(f.model.definition)).not.toBe(before);
    injector.destroy();
  });

  it("place mapped data through the rows on screen, which hold unfinished conditions", () => {
    const f = conditions();
    const before = JSON.stringify(f.model.definition);
    // Without rows on screen nothing is written.
    expect(f.form.applyDrop(f.when, drag("/input/amount"), end)).toBe(false);
    expect(f.announcements).toEqual([
      "Open the path's conditions to map data into them.",
    ]);
    const placed: string[] = [];
    const detach = f.form.attachConditionRows(f.when, (entry) => {
      placed.push(entry.ref);
      return true;
    });
    expect(f.form.applyDrop(f.when, drag("/input/amount"), end)).toBe(true);
    expect(
      f.form.applyDrop(f.when, drag("/steps/route-by-value/output"), end),
    ).toBe(false);
    expect(placed).toEqual(["/input/amount"]);
    detach();
    expect(f.form.applyDrop(f.when, drag("/input/amount"), end)).toBe(false);
    expect(placed).toEqual(["/input/amount"]);
    expect(JSON.stringify(f.model.definition)).toBe(before);
  });

  it("refuse drops while the workflow can't change", () => {
    const f = conditions();
    (f.host as { editingLocked: boolean }).editingLocked = true;
    expect(f.form.conditionDrop(f.when, drag("/input/amount"))).toEqual({
      reason: "A simulation is running. Stop it to edit.",
    });
    expect(f.form.dropFit(f.when, drag("/input/amount")).fit).toBe("refused");
  });
});

describe("fields from an example", () => {
  it("keep an answer only while the field it was pasted into is unchanged", async () => {
    const answer = { schema: { type: "object" } };
    let current = true;
    const started: number[] = [];
    let resolve: (value: typeof answer) => void = () => undefined;
    const run = () =>
      new Promise<typeof answer>((done) => {
        resolve = done;
      });
    const infer = guardedInference(run, () => {
      started.push(Date.now());
      return () => current;
    });
    const kept = infer([{ id: 1 }]);
    resolve(answer);
    await expect(kept).resolves.toBe(answer);
    const late = infer([{ id: 1 }]);
    current = false;
    resolve(answer);
    await expect(late).rejects.toThrow(STALE_EXAMPLE);
    expect(started).toHaveLength(2);
  });
});
