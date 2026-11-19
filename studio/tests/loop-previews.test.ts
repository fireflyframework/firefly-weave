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
// Inside a loop's body, step details preview with the picked item: the
// sample context holds the item, its position and every loop around it, a
// field's "= value" and the Result panel follow the picker, and outside a
// loop the current item still can't be previewed.
import "@angular/compiler";
import {
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import { InputPane } from "../src/app/editor/ndv/panes/input-pane";
import { beforeAll, describe, expect, it } from "vitest";
import { formulaPreviews, WAITING } from "../src/app/editor/formula/preview";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import type { Json, ParamSpec } from "../src/app/editor/ndv/registry";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-lines, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {orders: {type: array}}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {ref: /input/orders}
      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps:
          - id: lines
            kind: forEach
            items: {ref: /item/lines}
            concurrency: 1
            maxItems: 1000
            collect: all
            body:
              steps:
                - {id: send, kind: transform, value: {ref: /item/sku}}
              output: {ref: /item}
        output: {ref: /item}
    - {id: after, kind: transform, value: {ref: /input/orders}}
  output: {literal: null}
`;
const ORDERS = [
  { id: "a", lines: [{ sku: "x" }, { sku: "y" }] },
  { id: "b", lines: [{ sku: "z" }] },
];

function opened(
  input: Json | undefined = { orders: ORDERS },
  source = SOURCE,
  results: Record<string, Json> = {},
  /** Results from an earlier execution than the steps before them: out of date. */
  stale: readonly string[] = [],
) {
  const model = new StructuredCanvasAdapter();
  model.setSource(source, "yaml");
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: () => undefined,
    undo: () => model.undo(),
    undoIfUnchanged: async () => "changed" as const,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: { request: async () => ({ features: ["flow.forEach"] }) },
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
    // The last execution's results, by instance key, as last-results.ts keeps them.
    execution: {
      results: signal({
        schemaVersion: 1,
        at: "2026-11-19T10:00:00Z",
        status: "completed",
        steps: Object.fromEntries(
          Object.entries(results).map(([key, output]) => [
            key,
            {
              output,
              ...(stale.includes(key) ? { execution: "earlier" } : {}),
            },
          ]),
        ),
      }),
      last: signal(null),
      running: () => null,
    },
  } as unknown as ControllerHost;
  // With the browser's formula previews, as step details mount them.
  const controller: StepDetailsController = new StepDetailsController(
    host,
    () => 1000,
    formulaPreviews(() => controller.previewData()),
  );
  // The trigger's test input: the data the loops go through.
  controller.testEvent = () => input;
  return { host, model, controller };
}
const value = (controller: StepDetailsController, step: string, ref: string) =>
  controller.ndvContext(step).evaluate({ ref }, ["value"]);

describe("data inside a loop's body", () => {
  it("gives a step the picked item, its position and every loop around it", () => {
    const { controller } = opened();
    controller.iterations.pick("orders", 1);
    expect(controller.ndvContext("send").sample.context(["value"])).toEqual({
      input: { orders: ORDERS },
      steps: {},
      item: { sku: "z" },
      index: 0,
      loops: {
        orders: { item: ORDERS[1], index: 1 },
        lines: { item: { sku: "z" }, index: 0 },
      },
    });
  });

  it("previews a field with the picked item, and follows the picker", () => {
    const { controller } = opened();
    expect(value(controller, "send", "/item/sku")).toEqual({
      ok: true,
      value: "x",
    });
    controller.iterations.pick("lines", 1);
    expect(value(controller, "send", "/item/sku")).toEqual({
      ok: true,
      value: "y",
    });
    // Another order keeps the line picked: order b has no second line.
    controller.iterations.pick("orders", 1);
    expect(value(controller, "send", "/item/sku")).toMatchObject({
      ok: false,
      error: { code: WAITING },
    });
    controller.iterations.pick("lines", 0);
    expect(value(controller, "send", "/item/sku")).toEqual({
      ok: true,
      value: "z",
    });
    expect(value(controller, "send", "/loops/orders/item/id")).toEqual({
      ok: true,
      value: "b",
    });
    expect(value(controller, "send", "/index")).toEqual({ ok: true, value: 0 });
  });

  it("says why when the picked item isn't in the data", () => {
    const { controller } = opened();
    controller.iterations.pick("orders", 5);
    // No item, and no position for an item that isn't there: lines sits
    // right inside orders, send inside both.
    expect(controller.loopData("lines")).toBeNull();
    expect(controller.loopData("send")).toBeNull();
    expect(
      controller.ndvContext("send").sample.context(["value"]),
    ).toStrictEqual({
      input: { orders: ORDERS },
      steps: {},
    });
    // Inside a loop, the item is picked: what's missing is the data.
    expect(value(controller, "send", "/item/sku")).toEqual({
      ok: false,
      error: {
        code: WAITING,
        message:
          "Can't compute yet: the picked item isn't in the data. Execute previous steps.",
      },
    });
  });

  it("still can't preview the current item outside a loop", () => {
    const { controller } = opened();
    expect(value(controller, "after", "/item")).toEqual({
      ok: false,
      error: {
        code: WAITING,
        message:
          "Can't compute yet: the current item exists only while the loop runs.",
      },
    });
  });

  it("names the picked item as the Result panel's source", async () => {
    const { host, controller } = opened();
    const session = new FormSession(host, controller, openRequest("send"), {
      confirm: async () => true,
      announce() {},
    });
    const spec = { id: "value", path: ["value"], type: "json" } as ParamSpec;
    const mapped = (ref: string) =>
      session.previewSource(spec, { mode: "mapped", expression: { ref } });
    // Before the count is known: the item alone.
    expect(mapped("/item/sku")).toBe("Item 1");
    await controller.iterations.evaluate("lines");
    expect(mapped("/item/sku")).toBe("Item 1 of 2");
    await controller.iterations.evaluate("orders");
    controller.iterations.pick("orders", 1);
    await controller.iterations.evaluate("orders");
    expect(mapped("/loops/orders/item/id")).toBe("Item 2 of 2");
    // A formula that reads no item keeps the data's own source.
    expect(mapped("/input/orders")).toBe("From test input");
  });
});

describe("an earlier step inside the same loop", () => {
  const CHECKED = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: checked, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {orders: {type: array}}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {ref: /input/orders}
      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item/id}}
          - {id: send, kind: transform, value: {ref: /steps/check/output}}
        output: {ref: /item}
  output: {literal: null}
`;
  it("previews with its output for the picked item", () => {
    const { host, controller } = opened(
      { orders: ORDERS },
      CHECKED,
      { "check[0]": "from item 1", "check[1]": "from item 2" },
      ["check[1]"],
    );
    controller.iterations.pick("orders", 1);
    expect(value(controller, "send", "/steps/check/output")).toEqual({
      ok: true,
      value: "from item 2",
    });
    expect(
      controller.ndvContext("send").sample.context(["value"])?.steps,
    ).toEqual({ check: { output: "from item 2" } });
    controller.iterations.pick("orders", 0);
    expect(value(controller, "send", "/steps/check/output")).toEqual({
      ok: true,
      value: "from item 1",
    });
    // The Input pane shows check's output for the picked item too.
    const session = new FormSession(host, controller, openRequest("send"), {
      confirm: async () => true,
      announce() {},
    });
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const pane = runInInjectionContext(injector, () => new InputPane());
    pane.session = signal(session) as never;
    controller.iterations.pick("orders", 1);
    expect(pane.dataOf("step:check")).toBe("from item 2");
    expect(pane.notRun("step:check")).toBeNull();
    expect(pane.sourceTag("step:check")).toBe("Simulated");
    // Item 2's result is out of date, and the pane says so.
    expect(pane.staleSource("step:check")).toBe(true);
    injector.destroy();
    // Data kept for the step itself holds for every item.
    const shared = opened({ orders: ORDERS }, CHECKED, {
      check: "for every item",
    }).controller;
    shared.iterations.pick("orders", 1);
    expect(value(shared, "send", "/steps/check/output")).toEqual({
      ok: true,
      value: "for every item",
    });
  });
});

describe("the Input pane inside a loop's body", () => {
  it("shows the picked item's values and names the item", async () => {
    const { host, controller } = opened();
    const session = new FormSession(host, controller, openRequest("send"), {
      confirm: async () => true,
      announce() {},
    });
    const injector = Injector.create({
      providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
    });
    const pane = runInInjectionContext(injector, () => new InputPane());
    pane.session = signal(session) as never;
    expect(pane.dataOf("item")).toEqual({ sku: "x" });
    expect(pane.dataOf("index")).toBe(0);
    expect(pane.dataOf("loop:orders")).toEqual(ORDERS[0]);
    expect(pane.dataOf("loop-index:orders")).toBe(0);
    controller.iterations.pick("orders", 1);
    controller.iterations.pick("lines", 0);
    expect(pane.dataOf("item")).toEqual({ sku: "z" });
    expect(pane.dataOf("loop:orders")).toEqual(ORDERS[1]);
    await controller.iterations.evaluate("orders");
    expect(pane.sourceTag("loop:orders")).toBe("Item 2 of 2");
    expect(pane.sourceTag("item")).toBe("Item 1");
    injector.destroy();
  });
});
