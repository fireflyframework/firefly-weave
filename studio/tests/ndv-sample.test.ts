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
import { signal } from "@angular/core";
import type { LastResults } from "../src/app/editor/state/last-results";

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

describe("step details read the last execution", () => {
  const at = "2026-10-20T10:00:00Z";
  const pinOf = (output: unknown) => ({
    output: output as never,
    source: "manual" as const,
    pinnedAt: at,
    configHash: "fnv1a64:0000000000000001",
    contractHash: "fnv1a64:0000000000000002",
    upstreamHash: "fnv1a64:0000000000000003",
  });
  /** A controller whose host has executed lookup and route, with `execution` fingerprints for lookup. */
  function executed(lookupExecution?: string) {
    const made = setup();
    const prints = made.controller.prints();
    const results = signal<LastResults | null>({
      schemaVersion: 1,
      at,
      status: "completed",
      steps: {
        lookup: {
          status: "completed",
          execution: (lookupExecution ??
            prints.get("lookup")!.execution) as never,
          input: { customerId: "c-run" },
          output: { customerId: "c-run", token: "abc" },
        },
        route: {
          status: "completed",
          execution: prints.get("route")!.execution,
          output: { customerId: "c-run" },
        },
      },
    });
    made.host.execution = { results } as never;
    return { ...made, results };
  }
  it("resolvedInput and output come from the same precedence", async () => {
    const { controller, testData } = executed();
    const output = () => controller.ndvContext("lookup").sample.output();
    // A last result, secret fields left out, current.
    expect(output()).toEqual({
      state: "available",
      value: { customerId: "c-run" },
      source: "simulated",
      stale: null,
    });
    expect(controller.previewData().output("lookup")).toEqual({
      customerId: "c-run",
    });
    expect(
      controller.ndvContext("route").sample.context(["value"])?.steps,
    ).toEqual({ lookup: { output: { customerId: "c-run" } } });
    expect(controller.ndvContext("lookup").sample.resolvedInput()).toEqual({
      state: "available",
      value: { customerId: "c-run" },
      source: "simulated",
      stale: null,
    });
    // A pin goes before the last result, everywhere.
    await testData.change(
      withPin(emptyTestData(at), "lookup", pinOf({ customerId: "c-pin" }), at),
      ["/pins/lookup"],
    );
    expect(output()).toMatchObject({
      value: { customerId: "c-pin" },
      source: "pinned",
    });
    expect(controller.previewData().output("lookup")).toEqual({
      customerId: "c-pin",
    });
  });
  it("a last result shows as simulated, and an out-of-date one is still used", () => {
    const { controller } = executed("fnv1a64:00000000000000ff");
    expect(controller.ndvContext("lookup").sample.output()).toEqual({
      state: "available",
      value: { customerId: "c-run" },
      source: "simulated",
      stale: "upstream",
    });
    expect(controller.previewData().output("lookup")).toEqual({
      customerId: "c-run",
    });
  });
  it("a result too large to copy field by field still shows, unless its fields mark a secret", () => {
    const { controller, results, model } = executed();
    model.insert("transform", "root", undefined, {
      id: "plain",
      value: { literal: [] },
    });
    const rows = Array.from({ length: 1200 }, (_, i) => ({ i }));
    const current = results()!;
    const plain = controller.prints().get("plain")!.execution;
    const lookup = controller.prints().get("lookup")!.execution;
    results.set({
      ...current,
      steps: {
        plain: { status: "completed", execution: plain, output: rows },
        lookup: {
          status: "completed",
          execution: lookup,
          output: Array.from({ length: 1200 }, () => ({
            token: "abc",
          })) as never,
        },
      },
    });
    expect(controller.stepData("plain")?.value).toEqual(rows);
    // lookup's fields mark token secret: withheld rather than shown unchecked.
    expect(controller.stepData("lookup")).toBeUndefined();
  });
});

describe("test answers for signals and people", () => {
  function answering() {
    const made = setup();
    made.model.insert("signal", "root", undefined, {
      id: "paid",
      name: "order-paid",
      timeoutSeconds: 600,
      payloadSchema: {
        type: "object",
        properties: { code: { type: "string", "x-secret": true } },
      },
    });
    return made;
  }
  it("script returns the iteration's answer before the step's", async () => {
    const { controller, testData } = answering();
    const at = "2026-10-20T10:00:00Z";
    await testData.change(
      {
        ...emptyTestData(at),
        signals: {
          paid: { payload: { who: "step" } },
          "paid[1]": { payload: { who: "second" } },
        },
      },
      ["/signals/paid", "/signals/paid[1]"],
    );
    expect(controller.ndvContext("paid").sample.script()).toEqual({
      payload: { who: "step" },
    });
    expect(controller.scriptOf("paid[1]")).toEqual({
      payload: { who: "second" },
    });
  });
  it("setScript keeps an answer, and refuses a secret keeping nothing", async () => {
    const { controller, testData } = answering();
    const sample = controller.ndvContext("paid").sample;
    expect(await sample.setScript({ payload: { paid: true } })).toEqual({
      ok: true,
    });
    expect(testData.data().signals?.["paid"]).toEqual({
      payload: { paid: true },
      source: "manual",
    });
    const refused = await sample.setScript({ payload: { code: "7731" } });
    expect(refused.ok).toBe(false);
    expect(JSON.stringify(testData.data())).not.toContain("7731");
    expect(await sample.setScript(null)).toEqual({ ok: true });
    expect(testData.data().signals).toBeUndefined();
  });
});
