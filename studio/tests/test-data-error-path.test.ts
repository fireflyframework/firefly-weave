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
// Removing a step's error path from Settings takes the test data of the
// steps it held, as deleting them on the canvas does, and keeps it for an
// Undo that brings the same steps back.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { writeOnError } from "../src/app/editor/ndv/settings/on-error";
import { newHttpRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { emptyTestData, withPin } from "../src/app/editor/state/test-data-ops";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import {
  StructuredCanvasAdapter,
  stepIdentities,
  type Step,
} from "../src/app/model";
import type { ToastAction } from "../src/app/toast";

beforeAll(() => loadKindRegistrations());

const AT = "2026-11-20T10:00:00Z";
const pinOf = (output: unknown) => ({
  output,
  source: "manual" as const,
  pinnedAt: AT,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
});
const transform = (id: string): Step => ({
  id,
  kind: "transform",
  value: { literal: 1 },
});

/** Step details on submit, whose error path holds notify-team and flag-request, then after. */
function setup() {
  const model = new StructuredCanvasAdapter();
  model.insert("action", "root", undefined, {
    id: "submit",
    uses: "intake.submit@1.0.0",
    with: { literal: {} },
    onError: "errorOutput",
    errorPath: {
      steps: [transform("notify-team"), transform("flag-request")],
      output: { literal: null },
    },
  });
  model.insert("transform", "root", undefined, transform("after"));
  model.canvas = withOwnedAction(model.canvas, "intake.submit@1.0.0", {
    ...newHttpRecipe(),
    method: "POST",
  });
  model.clearHistory();
  const toasts: { text: string; action?: ToastAction }[] = [];
  // The platform's manifest; the host's check accepts every pin.
  const api = {
    request: async (url: string, _method: string, body?: unknown) =>
      url === "/studio/local/testdata/check"
        ? {
            entries: Object.keys(
              (body as { testData: { pins?: object } }).testData.pins ?? {},
            ).map((id) => ({ path: `/pins/${id}`, ok: true })),
          }
        : {
            features: ["flow.onError"],
            step_fields: [{ name: "onError", kinds: ["action", "transform"] }],
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
  return { model, controller, toasts };
}

describe("test data of an error path removed from Settings", () => {
  it("goes with the path's steps, and comes back with them on Undo", async () => {
    const { model, controller } = setup();
    await controller.loadFeatures();
    const pins = withPin(
      withPin(emptyTestData(AT), "notify-team", pinOf({ sent: true }), AT),
      "after",
      pinOf({ kept: true }),
      AT,
    );
    expect(await controller.testData.change(pins, [])).toMatchObject({
      ok: true,
    });
    const submit = model.definition.spec.steps[0];
    controller.commit(
      "submit",
      // As the On error field writes it.
      writeOnError(submit, "continue").patch.map((edit) => ({
        scope: "step" as const,
        path: edit.path,
        value: edit.value,
      })),
      "onError",
    );
    expect(model.stepIdSet().has("notify-team")).toBe(false);
    // Only the removed step's pin goes.
    expect(Object.keys(controller.testData.data().pins ?? {})).toEqual([
      "after",
    ]);
    // Undo brings the same step back, and the app's Undo its pin with it.
    model.undo();
    expect(
      await controller.testData.revive(
        (id) => model.stepIdSet().has(id),
        stepIdentities(model.definition),
      ),
    ).toEqual(["/pins/notify-team"]);
    expect(controller.testData.data().pins?.["notify-team"]).toMatchObject({
      output: { sent: true },
    });
  });
});
