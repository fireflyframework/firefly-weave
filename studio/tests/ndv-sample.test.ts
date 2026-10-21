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
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import { withPin, emptyTestData } from "../src/app/editor/state/test-data-ops";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import { StructuredCanvasAdapter } from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const CONTRACT = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "crm.lookup", version: "1.0.0" },
  spec: {
    outputSchema: {
      type: "object",
      properties: {
        customerId: { type: "string" },
        token: { type: "string", "x-secret": true },
      },
    },
  },
};

/** A controller over a workflow lookup → route → later, with test data in memory that the host accepts. */
function setup() {
  const model = new StructuredCanvasAdapter();
  model.insert("action", "root", undefined, {
    id: "lookup",
    uses: "crm.lookup@1.0.0",
    with: { literal: {} },
  });
  model.insert("transform", "root", undefined, {
    id: "route",
    value: { ref: "/steps/lookup/output" },
  });
  model.insert("action", "root", undefined, {
    id: "later",
    uses: "crm.lookup@1.0.0",
    with: { literal: {} },
  });
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
      return { entries: paths.map((path) => ({ path, ok: true })) } as T;
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
    catalogContracts: new Map([["crm.lookup@1.0.0", CONTRACT]]),
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

describe("step details read test data", () => {
  it("previews read a pinned step's output, never a secret field", async () => {
    const { controller, testData } = setup();
    const at = "2026-10-20T10:00:00Z";
    const pin = {
      output: { customerId: "c-1", token: "abc" },
      source: "manual" as const,
      pinnedAt: at,
      configHash: "fnv1a64:0000000000000001",
      contractHash: "fnv1a64:0000000000000002",
      upstreamHash: "fnv1a64:0000000000000003",
    };
    await testData.change(withPin(emptyTestData(at), "lookup", pin, at), [
      "/pins/lookup",
    ]);
    expect(controller.previewData().output("lookup")).toEqual({
      customerId: "c-1",
    });
  });
  it("the sample context lists visible steps' pins only", async () => {
    const { controller } = setup();
    expect(await controller.generateOutput("lookup")).toBeNull();
    expect(await controller.generateOutput("later")).toBeNull();
    const context = controller.ndvContext("route").sample.context(["value"]);
    expect(Object.keys(context?.steps ?? {})).toEqual(["lookup"]);
  });
  it("the test event is the test input", async () => {
    const { controller, testData } = setup();
    expect(await controller.setTestEvent({ amount: 1 })).toBeNull();
    expect(testData.data().input).toEqual({
      value: { amount: 1 },
      source: "manual",
    });
    await controller.setTestEvent({ amount: 2 }, "schema");
    expect(testData.data().input?.source).toBe("schema");
  });
  it("a rename in step details moves the step's pin", async () => {
    const { controller, testData } = setup();
    await controller.generateOutput("lookup");
    controller.renamePending("lookup", "find");
    expect(Object.keys(testData.data().pins ?? {})).toEqual(["find"]);
  });
});
