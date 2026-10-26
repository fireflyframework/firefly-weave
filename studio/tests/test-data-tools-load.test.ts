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
// The test data tools load after the first page: a load that fails is tried
// again, then said in plain words, and a later call tries afresh instead of
// keeping the failure.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
afterAll(() => vi.unstubAllGlobals());

type Loader = {
  loadTools(): Promise<unknown>;
  requireTestData(): Promise<unknown>;
};

function fixture(importer: () => Promise<unknown>) {
  const app = Object.create(App.prototype) as App;
  const said: string[] = [];
  Object.assign(app, {
    cdr: { markForCheck() {} },
    tools: null,
    toolsLoading: null,
    importTools: importer,
    notify(text: string) {
      said.push(text);
    },
  });
  // A signal the App reads; a stand-in that counts.
  Object.assign(app, { toolsReady: Object.assign(() => 0, { update() {} }) });
  const ready: boolean[] = [];
  Object.assign(app, {
    testData: null,
    testDataLoading: null,
    testDataReady: Object.assign(() => ready.at(-1) ?? false, {
      set: (value: boolean) => ready.push(value),
    }),
  });
  return { app: app as unknown as Loader, said, ready };
}

describe("loading the test data tools", () => {
  it("tries once more after a failure", async () => {
    let calls = 0;
    const tools = { printsOf() {} };
    const { app, said } = fixture(async () => {
      calls++;
      if (calls === 1)
        throw new TypeError("Failed to fetch dynamically imported module");
      return tools;
    });
    await expect(app.loadTools()).resolves.toBe(tools);
    expect(calls).toBe(2);
    expect(said).toEqual([]);
  });
  it("says so in plain words when it still fails, and tries afresh next time", async () => {
    let fail = true;
    const tools = { printsOf() {} };
    const { app, said } = fixture(async () => {
      if (fail)
        throw new TypeError("Failed to fetch dynamically imported module");
      return tools;
    });
    await expect(app.loadTools()).rejects.toThrow();
    expect(said).toEqual([
      "Studio couldn't load what test data needs. Reload the page to try again.",
    ]);
    fail = false;
    await expect(app.loadTools()).resolves.toBe(tools);
  });
});

describe("the test data service", () => {
  it("is made by the tools, so it loads with them", async () => {
    const tools = await import("../src/app/editor/state/test-data-tools");
    const { TestDataService } = await import(
      "../src/app/editor/state/test-data-service"
    );
    const service = tools.createTestDataService({
      api: { request: async <T>() => ({ entries: [] }) as T },
      backend: {
        keys: async () => [],
        get: async () => null,
        set: async () => true,
        remove: async () => true,
      },
      now: () => new Date("2026-10-26T10:00:00Z"),
      inputs: () => ({ source: "", definitions: [], schemaOf: () => null }),
    });
    expect(service).toBeInstanceOf(TestDataService);
    expect(tools.testDataOps.emptyTestData("2026-10-26T10:00:00Z").kind).toBe(
      "weave.studio/testData",
    );
    expect(typeof tools.readTestData).toBe("function");
    expect(tools.CHECK_WORDS["WV-SCHEMA-SECRET_VALUE"]).toBeTruthy();
  });
  it("is made once, only when the tools load, and then the designer may open", async () => {
    let made = 0;
    const service = { kind: "service" };
    const { app, ready } = fixture(async () => ({
      createTestDataService: () => (made++, service),
    }));
    expect(made).toBe(0);
    const [first, second] = await Promise.all([
      app.requireTestData(),
      app.requireTestData(),
    ]);
    expect(first).toBe(service);
    expect(second).toBe(service);
    await expect(app.requireTestData()).resolves.toBe(service);
    expect(made).toBe(1);
    expect(ready).toEqual([true]);
  });
  it("tries afresh after the tools fail to load", async () => {
    let fail = true;
    const service = { kind: "service" };
    const { app, ready } = fixture(async () => {
      if (fail)
        throw new TypeError("Failed to fetch dynamically imported module");
      return { createTestDataService: () => service };
    });
    await expect(app.requireTestData()).rejects.toThrow();
    expect(ready).toEqual([]);
    fail = false;
    await expect(app.requireTestData()).resolves.toBe(service);
    expect(ready).toEqual([true]);
  });
});
