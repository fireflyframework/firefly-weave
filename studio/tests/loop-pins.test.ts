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
// Pins inside a loop's body: Pin pins the picked item under its instance
// key, "Pin for every item" the step; an item's pin wins for that item only,
// a pin past the test data limit is refused, and renames move item pins.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  StepDetailsController,
  pinSubject,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import { itemPicks } from "../src/app/editor/state/iterations";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import { TEST_DATA_MAX_BYTES } from "../src/app/editor/state/test-data";
import { StructuredCanvasAdapter } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: notify, version: 1.0.0}
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
          - {id: send, kind: action, uses: crm.send@1.0.0, with: {literal: {}}}
        output: {ref: /item}
  output: {literal: null}
`;

function setup() {
  const model = new StructuredCanvasAdapter();
  model.setSource(SOURCE, "yaml");
  model.clearHistory();
  const values = new Map<string, string>();
  const backend: StoreBackend = {
    keys: async () => [...values.keys()],
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
  const api = {
    request: async <T>(path: string, _method?: string, body?: unknown) => {
      if (path !== "/studio/local/testdata/check") return { features: [] } as T;
      const data = (body as { testData: Record<string, unknown> }).testData;
      const paths = [
        ...(data["input"] ? ["/input"] : []),
        ...Object.keys((data["pins"] as object) ?? {}).map(
          (key) => `/pins/${key}`,
        ),
      ];
      const entries: { path: string; ok: boolean; code?: string }[] = paths.map(
        (path) => ({ path, ok: true }),
      );
      // Test answers: a payload with `code` stands for a value marked secret.
      for (const [key, entry] of Object.entries(
        (data["signals"] as Record<string, { payload?: object }>) ?? {},
      ))
        entries.push(
          entry.payload && "code" in entry.payload
            ? {
                path: `/signals/${key}`,
                ok: false,
                code: "WV-SCHEMA-SECRET_VALUE",
              }
            : { path: `/signals/${key}`, ok: true },
        );
      return { entries } as T;
    },
  };
  const testData = new TestDataService({
    api,
    backend,
    now: () => new Date("2026-10-20T10:00:00Z"),
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => ({}) as never,
    }),
  });
  const host: { -readonly [K in keyof ControllerHost]: ControllerHost[K] } = {
    model,
    perform: (edit) => edit(),
    notify: () => undefined,
    undo: () => model.undo(),
    undoIfUnchanged: async () => "undone" as const,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind) => kind,
    editingLocked: false,
    profile: null,
    api: api as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    cacheContract: () => undefined,
    refreshView: () => undefined,
    can: () => true,
    testData,
    openTestData: () => false,
  };
  const controller = new StepDetailsController(host, () => 1000);
  return { model, host, controller, testData };
}

describe("Debug in editor on a failed item", () => {
  it("picks the item the run failed at in each loop around the step", () => {
    const { model } = setup();
    expect(itemPicks(model.definition, "send[3]")).toEqual([
      { loopId: "orders", index: 3 },
    ]);
    expect(itemPicks(model.definition, "orders")).toEqual([]);
    // Nested: the outer loop's item first.
    const nested = {
      spec: {
        steps: [
          {
            id: "orders",
            kind: "forEach",
            body: {
              steps: [
                {
                  id: "lines",
                  kind: "forEach",
                  body: { steps: [{ id: "reserve", kind: "transform" }] },
                },
              ],
            },
          },
        ],
      },
    };
    expect(itemPicks(nested, "reserve[1][0]")).toEqual([
      { loopId: "orders", index: 1 },
      { loopId: "lines", index: 0 },
    ]);
    // A repeated activation's suffix isn't an item.
    expect(itemPicks(model.definition, "send[1]#2")).toEqual([
      { loopId: "orders", index: 1 },
    ]);
  });

  it("opens step details on that item once, and keeps a pick made after", () => {
    const { controller, host } = setup();
    Object.assign(host, {
      draftId: "draft-1",
      runNotice: {
        draftId: "draft-1",
        runId: "run-1",
        key: "send[3]",
        code: "SMTP_REJECTED",
        missing: false,
      },
    });
    controller.pickRunItem("send");
    expect(controller.iterations.picks("send")).toEqual([
      { loopId: "orders", index: 3 },
    ]);
    controller.iterations.pick("orders", 0);
    controller.pickRunItem("send");
    expect(controller.iterations.instanceKey("send")).toBe("send[0]");
    // Another draft's stop picks nothing.
    const other = setup();
    Object.assign(other.host, {
      draftId: "draft-2",
      runNotice: {
        draftId: "draft-1",
        runId: "run-1",
        key: "send[3]",
        code: null,
        missing: false,
      },
    });
    other.controller.pickRunItem("send");
    expect(other.controller.iterations.instanceKey("send")).toBe("send[0]");
  });
});

describe("a run log row for one item of a loop", () => {
  it("opens its step on that item each time it is chosen, and only for that step", () => {
    const { controller, host } = setup();
    Object.assign(host, { draftId: "draft-1" });
    const third = { draftId: "draft-1", key: "send[2]" };
    controller.pickOpenedItem(third, "send");
    expect(controller.iterations.instanceKey("send")).toBe("send[2]");
    // A pick made after stays while step details follow the same row.
    controller.iterations.pick("orders", 0);
    controller.pickOpenedItem(third, "send");
    expect(controller.iterations.instanceKey("send")).toBe("send[0]");
    // The row chosen again picks its item again.
    controller.pickOpenedItem({ draftId: "draft-1", key: "send[2]" }, "send");
    expect(controller.iterations.instanceKey("send")).toBe("send[2]");
    // Another step, or another draft's row, picks nothing.
    controller.pickOpenedItem({ draftId: "draft-1", key: "send[1]" }, "orders");
    controller.pickOpenedItem({ draftId: "draft-9", key: "send[1]" }, "send");
    expect(controller.iterations.instanceKey("send")).toBe("send[2]");
  });
});

describe("pins inside a loop's body", () => {
  it("pins the picked item under its instance key, or the step for every item", async () => {
    const { controller, testData } = setup();
    expect(
      await controller.pin("send[2]", { to: "item 3" }, "manual"),
    ).toBeNull();
    expect(
      await controller.pin("send", { to: "everyone" }, "manual"),
    ).toBeNull();
    expect(Object.keys(testData.data().pins ?? {}).sort()).toEqual([
      "send",
      "send[2]",
    ]);
  });

  it("pins from step details for the item picked in the loop", async () => {
    const { controller, testData } = setup();
    controller.iterations.pick("orders", 2);
    const sample = controller.ndvContext("send").sample!;
    expect(await sample.setPin({ to: "item 3" })).toEqual({ ok: true });
    expect(Object.keys(testData.data().pins ?? {})).toEqual(["send[2]"]);
    // Another item picked: its own pin, the first one kept.
    controller.iterations.pick("orders", 0);
    expect(await sample.setPin({ to: "item 1" })).toEqual({ ok: true });
    expect(Object.keys(testData.data().pins ?? {}).sort()).toEqual([
      "send[0]",
      "send[2]",
    ]);
  });

  it("uses an item's pin for that item only, and the step's for the others", async () => {
    const { controller } = setup();
    await controller.pin("send", { to: "everyone" }, "manual");
    await controller.pin("send[2]", { to: "item 3" }, "manual");
    expect(controller.stepData("send[2]")).toMatchObject({
      value: { to: "item 3" },
      source: "pinned",
    });
    expect(controller.stepData("send[1]")).toMatchObject({
      value: { to: "everyone" },
      source: "pinned",
    });
    expect(controller.pinOfTarget("send[2]")?.key).toBe("send[2]");
    // Just pinned, against its step as it is: not out of date.
    expect(controller.pinOfTarget("send[2]")?.stale).toBeNull();
  });

  it("refuses a pin past the test data limit and keeps the one there", async () => {
    const { controller, testData } = setup();
    await controller.pin("send[2]", { to: "item 3" }, "manual");
    const huge = { to: "x".repeat(TEST_DATA_MAX_BYTES) };
    expect(await controller.pin("send[2]", huge, "manual")).not.toBeNull();
    expect(controller.stepData("send[2]")?.value).toEqual({ to: "item 3" });
    expect(Object.keys(testData.data().pins ?? {})).toEqual(["send[2]"]);
  });

  it("pins an item's pin for every item in one change, with one toast whose Undo puts both pins back", async () => {
    const { controller, host, testData } = setup();
    const toasts: { text: string; run?: () => void }[] = [];
    host.notify = (text, action) =>
      void toasts.push({ text, run: action?.run });
    await controller.pin("send", { to: "everyone" }, "manual");
    // A pin kept from a real run: its source and run go with it.
    await controller.pin("send[2]", { to: "item 3" }, "test-call", {
      runId: "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
    });
    const before = structuredClone(testData.data().pins!);
    toasts.length = 0;
    expect(await controller.pinForEveryItem("send[2]")).toBeNull();
    const pins = testData.data().pins!;
    expect(Object.keys(pins)).toEqual(["send"]);
    expect(pins["send"]).toMatchObject({
      output: { to: "item 3" },
      source: "test-call",
      runId: "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b",
    });
    expect(toasts.map(({ text }) => text)).toEqual([
      "Pinned send for every item. Item 3's own pin was removed.",
    ]);
    toasts[0].run!();
    expect(testData.data().pins).toEqual(before);
  });

  it("names an item from 1 in its pin toasts, never by its instance key", async () => {
    const { controller, host } = setup();
    const toasts: string[] = [];
    host.notify = (text) => void toasts.push(text);
    await controller.pin("send[2]", { to: "a" }, "manual");
    await controller.pin("send[2]", { to: "b" }, "manual");
    controller.keepPinned("send[2]");
    await new Promise((done) => setTimeout(done, 0));
    await controller.unpin("send[2]");
    expect(toasts).toEqual([
      "Replaced the pinned output of send for item 3.",
      "Kept the pinned output of send for item 3.",
      "Unpinned send for item 3.",
    ]);
    // Inside a loop in a loop: the outer item first.
    expect(pinSubject("reserve[2][0]")).toBe("reserve for item 3 › 1");
    expect(pinSubject("send")).toBe("send");
  });

  it("moves an item's pin with the step's rename", async () => {
    const { controller, model, testData } = setup();
    await controller.pin("send[2]", { to: "item 3" }, "manual");
    model.renameStep("send", "notify");
    controller.renamePending("send", "notify");
    expect(Object.keys(testData.data().pins ?? {})).toEqual(["notify[2]"]);
    expect(controller.stepData("notify[2]")?.value).toEqual({ to: "item 3" });
  });
});
