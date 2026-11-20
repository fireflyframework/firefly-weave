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
// Step details read and write a step's simulated failure in the test data:
// set by hand (source manual) and checked like any entry, for one item or
// every item, removed with Undo, and refused whole when it doesn't fit.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { TEST_DATA_MAX_BYTES } from "../src/app/editor/state/test-data";
import {
  TEST_DATA_TOO_LARGE,
  emptyTestData,
  testDataBytes,
  withPin,
} from "../src/app/editor/state/test-data-ops";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import { StructuredCanvasAdapter } from "../src/app/model";
import type { ToastAction } from "../src/app/toast";

beforeAll(() => loadKindRegistrations());

const AT = "2026-11-20T10:00:00Z";

function setup() {
  const model = new StructuredCanvasAdapter();
  model.insert("transform", "root", undefined, {
    id: "submit",
    value: { literal: 1 },
    onError: "continue",
  });
  model.clearHistory();
  const checked: string[][] = [];
  const api = {
    request: async (_url: string, _method: string, body?: unknown) => {
      const data = (body as { testData: Record<string, object | undefined> })
        .testData;
      const paths = ["pins", "errors"].flatMap((section) =>
        Object.keys(data[section] ?? {}).map((key) => `/${section}/${key}`),
      );
      checked.push(paths);
      return { entries: paths.map((path) => ({ path, ok: true })) };
    },
  };
  const testData = new TestDataService({
    api: api as never,
    backend: {
      keys: async () => [],
      get: async () => null,
      set: async () => true,
      remove: async () => true,
    },
    now: () => new Date(AT),
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => ({}) as never,
    }),
  });
  const toasts: { text: string; action?: ToastAction }[] = [];
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: (text: string, action?: ToastAction) =>
      void toasts.push({ text, action }),
    undo: () => model.undo(),
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api,
    testData,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
  } as unknown as ControllerHost;
  const controller = new StepDetailsController(host, () => 1000);
  return { controller, testData, toasts, checked };
}

describe("a step's simulated failure in step details", () => {
  it("is written by hand for one item or every item, checked like any entry", async () => {
    const { controller, testData, checked } = setup();
    expect(
      await controller.setFailure("submit[2]", {
        code: "REQUEST_REFUSED",
        outcome: "unknown",
      }),
    ).toEqual({ ok: true });
    expect(testData.data().errors).toEqual({
      "submit[2]": {
        code: "REQUEST_REFUSED",
        outcome: "unknown",
        source: "manual",
      },
    });
    // A failure holds no value for the host's check: it isn't asked.
    expect(checked).toEqual([]);
    expect(controller.failureOf("submit[2]")?.code).toBe("REQUEST_REFUSED");
    // Another item reads the step's failure, and there is none.
    expect(controller.failureOf("submit[3]")).toBeNull();
    await controller.setFailure("submit", { code: "TIMEOUT" });
    expect(controller.failureOf("submit[3]")).toEqual({
      code: "TIMEOUT",
      source: "manual",
    });
  });

  it("is removed with Undo, and Undo brings it back", async () => {
    const { controller, testData, toasts } = setup();
    await controller.setFailure("submit", { code: "TIMEOUT" });
    expect(await controller.setFailure("submit", null)).toEqual({ ok: true });
    expect(testData.data().errors).toBeUndefined();
    expect(toasts.at(-1)?.text).toBe(
      "Removed the simulated failure of submit.",
    );
    await toasts.at(-1)!.action!.run();
    expect(controller.failureOf("submit")?.code).toBe("TIMEOUT");
  });

  it("is refused whole when the test data has no room, keeping what was there", async () => {
    const { controller, testData } = setup();
    // Pinned output that leaves less room than the failure needs.
    const pinOf = (output: string) =>
      withPin(
        emptyTestData(AT),
        "submit",
        {
          output,
          source: "manual",
          pinnedAt: AT,
          configHash: "fnv1a64:0000000000000001",
          contractHash: "fnv1a64:0000000000000002",
          upstreamHash: "fnv1a64:0000000000000003",
        },
        AT,
      );
    const big = pinOf(
      "x".repeat(TEST_DATA_MAX_BYTES - testDataBytes(pinOf("")) - 20),
    );
    expect(await testData.change(big, [])).toMatchObject({ ok: true });
    expect(await controller.setFailure("submit", { code: "TIMEOUT" })).toEqual({
      ok: false,
      message: TEST_DATA_TOO_LARGE,
    });
    expect(testData.data()).toEqual(big);
  });
});
