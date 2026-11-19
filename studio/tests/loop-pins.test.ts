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
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { TestDataService } from "../src/app/editor/state/test-data-service";
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
    can: () => true,
    testData,
    openTestData: () => false,
  };
  const controller = new StepDetailsController(host, () => 1000);
  return { model, host, controller, testData };
}

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

  it("moves an item's pin with the step's rename", async () => {
    const { controller, model, testData } = setup();
    await controller.pin("send[2]", { to: "item 3" }, "manual");
    model.renameStep("send", "notify");
    controller.renamePending("send", "notify");
    expect(Object.keys(testData.data().pins ?? {})).toEqual(["notify[2]"]);
    expect(controller.stepData("notify[2]")?.value).toEqual({ to: "item 3" });
  });
});
