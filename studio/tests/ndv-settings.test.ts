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
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  DEFAULT_RETRY,
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { fixed } from "../src/app/editor/ndv/params/value-io";
import type { KindContext } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter } from "../src/app/model";
import {
  STOP_ONLY_REASON,
  STOP_RUN_REASON,
  aboutRows,
  onErrorText,
  placementText,
  retryOn,
  retryProblem,
  retrySwitched,
  savedInText,
  settingsChanged,
} from "../src/app/editor/ndv/settings/settings-rules";
import { createStep, freshWorkflow } from "../src/app/model";

vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

const ctx = (
  owned: Record<string, unknown> = {},
  contracts: Record<string, unknown> = {},
): KindContext => ({
  workflow: freshWorkflow(),
  features: [],
  actionContract: (uses) => (contracts[uses] as never) ?? null,
  tableContract: () => null,
  workflowContract: () => null,
  ownedAction: (uses) => (owned[uses] as never) ?? null,
});

describe("the Settings tab", () => {
  it("says why On error is fixed, and leaves it out where it doesn't apply", () => {
    expect(STOP_RUN_REASON).toBe(
      "Update the platform to continue after errors.",
    );
    expect(onErrorText("action")).toBe(STOP_RUN_REASON);
    expect(onErrorText("humanTask")).toBe(STOP_RUN_REASON);
    expect(onErrorText("switch")).toBe(STOP_ONLY_REASON);
    expect(STOP_ONLY_REASON).toBe(
      "The run fails with this step's error. You can retry it from Operate.",
    );
    expect(onErrorText("wait")).toBeNull();
    expect(onErrorText("fail")).toBeNull();
    expect(onErrorText("$trigger")).toBeNull();
  });
  it("checks the retry waits", () => {
    expect(
      retryProblem({
        maxAttempts: 3,
        initialDelaySeconds: 1,
        maxDelaySeconds: 30,
      }),
    ).toBe("");
    expect(
      retryProblem({
        maxAttempts: 3,
        initialDelaySeconds: 10,
        maxDelaySeconds: 5,
      }),
    ).toBe("The longest wait must be at least the wait between tries.");
    expect(retryProblem(undefined)).toBe("");
    // Off, the waits don't apply.
    expect(
      retryProblem({
        maxAttempts: 1,
        initialDelaySeconds: 10,
        maxDelaySeconds: 5,
      }),
    ).toBe("");
  });
  it("turns retry on with three tries and off with one, keeping the waits", () => {
    expect(retryOn(undefined)).toBe(false);
    expect(retryOn({ maxAttempts: 1 })).toBe(false);
    expect(retryOn({ maxAttempts: 3 })).toBe(true);
    expect(retrySwitched(undefined, true)).toEqual({
      maxAttempts: 3,
      initialDelaySeconds: 1,
      maxDelaySeconds: 30,
    });
    const kept = {
      maxAttempts: 5,
      initialDelaySeconds: 2,
      maxDelaySeconds: 40,
    };
    expect(retrySwitched(kept, false)).toEqual({ ...kept, maxAttempts: 1 });
    expect(retrySwitched({ ...kept, maxAttempts: 1 }, true)).toEqual({
      ...kept,
      maxAttempts: 3,
    });
  });
  it("marks Settings changed for any value off its default, or a note", () => {
    expect(settingsChanged([true, true], undefined)).toBe(false);
    expect(settingsChanged([true, false], undefined)).toBe(true);
    expect(settingsChanged([true], { text: "Check the ledger" })).toBe(true);
    expect(settingsChanged([true], { text: "  " })).toBe(false);
  });
  it("says where an owned action keeps its retry and timeout", () => {
    expect(
      savedInText("order-intake.get-customer-orders@1.0.0", "http", 1),
    ).toBe(
      "Saved in the API action order-intake.get-customer-orders@1.0.0, which only this step uses.",
    );
    expect(savedInText("order-intake.call-api@1.0.0", "http", 2)).toBe(
      "Saved in the API action order-intake.call-api@1.0.0, used by 2 steps.",
    );
    expect(savedInText("order-intake.send-email@1.0.0", "connector", 1)).toBe(
      "Saved in the action order-intake.send-email@1.0.0, which only this step uses.",
    );
  });
  it("places the step, and the trigger at the start", () => {
    const workflow = freshWorkflow();
    workflow.spec.steps = [
      createStep("transform", "first"),
      createStep("transform", "second"),
    ];
    expect(placementText(workflow, "second")).toBe(
      "Main sequence · step 2 of 2",
    );
    expect(placementText(workflow, "$trigger")).toBe("Starts the workflow");
    expect(placementText(workflow, "$workflow")).toBe("");
  });
  it("tells what runs an action and what it changes", () => {
    const step = {
      ...createStep("action", "get-orders"),
      uses: "order-intake.get-orders@1.0.0",
    };
    const recipe = {
      ...newHttpRecipe(),
      pathTemplate: "/customers/{customerId}/orders",
    };
    // Before Studio builds the action, its recipe tells the request.
    expect(
      aboutRows(step, ctx({ "order-intake.get-orders@1.0.0": recipe })),
    ).toEqual([
      ["Runs", "weave-http@2.0.0 · GET /customers/{customerId}/orders"],
      ["Side effect", "Reads data · safe to retry"],
    ]);
    expect(
      aboutRows(
        step,
        ctx({ "order-intake.get-orders@1.0.0": { ...recipe, method: "POST" } }),
      )[1],
    ).toEqual(["Side effect", "Changes data · never retried automatically"]);
    // A published action reads both from its document.
    const published = { ...step, uses: "crm.lookup@1.0.0" };
    expect(
      aboutRows(
        published,
        ctx(
          {},
          {
            "crm.lookup@1.0.0": {
              spec: {
                sideEffect: "idempotent",
                implementation: {
                  kind: "worker",
                  taskType: "crm-lookup",
                  taskVersion: "1.0.0",
                },
              },
            },
          },
        ),
      ),
    ).toEqual([
      ["Runs", "Worker task crm-lookup 1.0.0"],
      ["Side effect", "Repeating it changes nothing more · safe to retry"],
    ]);
    // An owned email action waits for its document.
    expect(
      aboutRows(
        { ...step, uses: "order-intake.send-email@1.0.0" },
        ctx({
          "order-intake.send-email@1.0.0": newConnectorRecipe(
            "weave-email@1.0.0",
            "send",
          ),
        }),
      ),
    ).toEqual([
      ["Runs", "Not known until the action's details load"],
      ["Side effect", "Not known until the action's details load"],
    ]);
    expect(aboutRows(createStep("wait", "pause"), ctx())).toEqual([]);
  });
  it("says when an owned action is published", () => {
    expect(savedInText("flow.get@1.0.0", "http", 1, true)).toBe(
      "Saved in the API action flow.get@1.0.0, which only this step uses. It's published, so your next change makes a new version.",
    );
    expect(savedInText("flow.send@1.0.0", "connector", 2, true)).toBe(
      "Saved in the action flow.send@1.0.0, used by 2 steps. It's published, so your next change makes a new version.",
    );
    expect(savedInText("flow.get@1.0.0", "http", 1)).toBe(
      "Saved in the API action flow.get@1.0.0, which only this step uses.",
    );
  });
});

describe("retry through the step form", () => {
  const USES = "untitled-workflow.get@1.0.0";
  function setup(retry: unknown) {
    const model = new StructuredCanvasAdapter();
    model.insert("action", "root", undefined, {
      id: "get",
      uses: USES,
      with: { literal: {} },
    });
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      ...(retry ? { retry } : {}),
    } as never);
    model.clearHistory();
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
      notify: () => undefined,
      refreshView: () => undefined,
      perform: (edit: () => void) => edit(),
      api: { request: async () => ({ features: [] }) },
    } as unknown as StepDetailsHost;
    let clock = 1000;
    const controller = new StepDetailsController(host, () => clock);
    const session = new FormSession(host, controller, openRequest("get"), {
      confirm: async () => true,
      announce: () => undefined,
    });
    const spec = controller.settings("get").fields[0];
    const children = spec.children!(
      controller.step("get")!,
      controller.kindContext(),
    );
    const stored = () => ownedActionsOf(model.canvas)[USES].retry;
    return {
      model,
      session,
      spec,
      children,
      stored,
      tick: () => (clock += 700),
    };
  }
  it("edits each retry setting as the action's own, also one still at its default", () => {
    const { session, children, stored } = setup({ ...DEFAULT_RETRY });
    expect(children.map((child) => child.label)).toEqual([
      "Max tries",
      "Wait between tries",
      "Longest wait",
    ]);
    expect(session.write(children[0], fixed(4))).toBe(true);
    expect(session.write(children[2], fixed(60))).toBe(true);
    expect(session.write(children[1], fixed(2))).toBe(true);
    expect(stored()).toEqual({
      maxAttempts: 4,
      initialDelaySeconds: 2,
      maxDelaySeconds: 60,
    });
  });
  it("writes three tries when Max tries is cleared or reset, so only the switch turns retry off", () => {
    const { session, children, stored } = setup({
      maxAttempts: 5,
      initialDelaySeconds: 2,
      maxDelaySeconds: 30,
    });
    session.clear(children[0]);
    expect(stored()).toEqual({
      maxAttempts: 3,
      initialDelaySeconds: 2,
      maxDelaySeconds: 30,
    });
    // A typed value below two waits in the box instead of being saved.
    expect(children[0].holdBelowMin).toBe(true);
  });
  it("turns retry on and off as one undo step each, keeping the waits", () => {
    const { model, session, spec, stored, tick } = setup(undefined);
    expect(
      session.write(spec, fixed({ ...retrySwitched(undefined, true) })),
    ).toBe(true);
    expect(stored()).toEqual(DEFAULT_RETRY);
    tick();
    expect(
      session.write(spec, fixed({ ...retrySwitched(stored(), false) })),
    ).toBe(true);
    expect(stored()).toEqual({ ...DEFAULT_RETRY, maxAttempts: 1 });
    model.undo();
    expect(stored()).toEqual(DEFAULT_RETRY);
    model.undo();
    expect(stored()).toBeUndefined();
  });
});

describe("accepted status codes through the step form", () => {
  it("reads and writes each code in the API action, not in the step", () => {
    const USES = "untitled-workflow.get@1.0.0";
    const model = new StructuredCanvasAdapter();
    model.insert("action", "root", undefined, {
      id: "get",
      uses: USES,
      with: { literal: {} },
    });
    model.canvas = withOwnedAction(model.canvas, USES, {
      ...newHttpRecipe(),
      statuses: [200, 201],
    } as never);
    model.clearHistory();
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
      notify: () => undefined,
      refreshView: () => undefined,
      perform: (edit: () => void) => edit(),
      api: { request: async () => ({ features: [] }) },
    } as unknown as StepDetailsHost;
    const controller = new StepDetailsController(host);
    const session = new FormSession(host, controller, openRequest("get"), {
      confirm: async () => true,
      announce: () => undefined,
    });
    const list = session
      .state("parameters")
      .fields.find((entry) => entry.spec.id === "statuses")!.spec;
    const second = session.itemSpec(list, 1, list.item!);
    expect(session.read(second)).toEqual(fixed(201));
    expect(session.write(second, fixed(202))).toBe(true);
    expect(ownedActionsOf(model.canvas)[USES].statuses).toEqual([200, 202]);
    expect(model.definition.spec.steps[0]).not.toHaveProperty("statuses");
  });
});
