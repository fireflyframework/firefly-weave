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
// On error in Settings: who may choose it, what each choice writes, and the
// error path that goes when a step stops continuing with error output.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { describeDiagnostic } from "../src/app/designer/diagnostic-copy";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { newHttpRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { readParam, writeParam } from "../src/app/editor/ndv/params/value-io";
import type { KindContext } from "../src/app/editor/ndv/registry";
import {
  HUMAN_HINT,
  RESULT_STATUS_HINT,
  SIGNAL_HINT,
  UNKNOWN_OUTCOME_HINT,
  endedOnError,
  onErrorFields,
  onErrorKinds,
  onErrorRow,
  onErrorValue,
  removedPathToast,
  writeOnError,
  type OnErrorContext,
} from "../src/app/editor/ndv/settings/on-error";
import {
  STOP_ONLY_REASON,
  STOP_RUN_REASON,
} from "../src/app/editor/ndv/settings/settings-rules";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
} from "../src/app/model";
import type { ToastAction } from "../src/app/toast";

beforeAll(() => loadKindRegistrations());

/** The kinds the platform's language manifest lists for On error. */
const KINDS = [
  "action",
  "llm",
  "transform",
  "decisionTable",
  "callWorkflow",
  "humanTask",
  "signal",
  "transaction",
  "agent",
];
function context(
  features: string[] = ["flow.onError"],
  kinds: string[] | null = KINDS,
  owned: Record<string, unknown> = {},
): OnErrorContext {
  return {
    workflow: freshWorkflow(),
    features,
    actionContract: () => null,
    tableContract: () => null,
    workflowContract: () => null,
    ownedAction: (uses) =>
      (owned[uses] as ReturnType<NonNullable<KindContext["ownedAction"]>>) ??
      null,
    onErrorKinds: kinds,
  };
}
const step = (kind: string, extra: Record<string, unknown> = {}): Step =>
  ({ id: "submit", kind, ...extra }) as Step;
const path = (...ids: string[]) => ({
  steps: ids.map((id) => ({ id, kind: "transform", value: { literal: 1 } })),
  output: { ref: "/steps/submit/error/message" },
});

describe("who may choose On error", () => {
  it("offers the three choices to every kind the platform lists", () => {
    for (const kind of [
      "action",
      "llm",
      "transform",
      "decisionTable",
      "callWorkflow",
    ])
      expect(onErrorRow(step(kind), context()), kind).toEqual({
        kind: "choices",
        value: "stop",
        hints: [],
      });
  });

  it("keeps Stop the run read-only where the platform can't continue after errors", () => {
    for (const ctx of [context([]), context(["flow.onError"], null)])
      for (const kind of ["action", "transform", "humanTask", "signal"])
        expect(onErrorRow(step(kind), ctx), kind).toEqual({
          kind: "read-only",
          reason: STOP_RUN_REASON,
        });
  });

  it("always stops the run for a decision, a parallel step and a loop", () => {
    for (const kind of ["switch", "parallel", "forEach"])
      expect(onErrorRow(step(kind), context()), kind).toEqual({
        kind: "read-only",
        reason: STOP_ONLY_REASON,
      });
  });

  it("has no row for a wait, a Stop with error, or a call that doesn't wait", () => {
    expect(onErrorRow(step("wait"), context())).toBeNull();
    expect(onErrorRow(step("fail"), context())).toBeNull();
    const detached = step("callWorkflow", { mode: "detach" });
    expect(onErrorRow(detached, context())).toBeNull();
    expect(onErrorRow(detached, context([]))).toBeNull();
  });

  it("says when it applies and what it can't change", () => {
    const owned = {
      "intake.submit@1.0.0": { ...newHttpRecipe(), method: "POST" },
      "intake.lookup@1.0.0": { ...newHttpRecipe(), method: "GET" },
    };
    const ctx = context(["flow.onError"], KINDS, owned);
    const action = (uses: string) => step("action", { uses });
    expect(onErrorRow(action("intake.submit@1.0.0"), ctx)).toMatchObject({
      hints: [UNKNOWN_OUTCOME_HINT],
    });
    expect(onErrorRow(action("intake.lookup@1.0.0"), ctx)).toMatchObject({
      hints: [],
    });
    expect(
      onErrorRow(step("callWorkflow", { onFailure: "continue" }), ctx),
    ).toMatchObject({ hints: [RESULT_STATUS_HINT] });
    expect(onErrorRow(step("signal"), ctx)).toMatchObject({
      hints: [SIGNAL_HINT],
    });
    expect(onErrorRow(step("humanTask"), ctx)).toMatchObject({
      hints: [HUMAN_HINT],
    });
  });

  it("reads the step's choice, and stop for anything else", () => {
    expect(onErrorValue(step("action", { onError: "continue" }))).toBe(
      "continue",
    );
    expect(onErrorValue(step("action", { onError: "errorOutput" }))).toBe(
      "errorOutput",
    );
    expect(onErrorValue(step("action"))).toBe("stop");
    expect(onErrorValue(step("action", { onError: "retry" }))).toBe("stop");
  });

  it("reads the kinds from the manifest's onError step field", () => {
    expect(
      onErrorKinds({
        step_fields: [
          { name: "undo", kinds: ["action"] },
          { name: "onError", kinds: ["action", "llm", 3] },
        ],
      }),
    ).toEqual(["action", "llm"]);
    expect(onErrorKinds({ features: ["flow.onError"] })).toBeNull();
    expect(onErrorKinds(null)).toBeNull();
  });

  it("adds On error to Settings, and Result on error with it", () => {
    const ids = (current: Step, ctx = context()) =>
      onErrorFields(current, ctx).map((spec) => spec.id);
    expect(ids(step("action"))).toEqual(["onError", "resultOnError"]);
    expect(ids(step("switch"))).toEqual(["onError"]);
    expect(ids(step("wait"))).toEqual([]);
    const [onError, result] = onErrorFields(step("action"), context());
    expect(onError).toMatchObject({ path: ["onError"], label: "On error" });
    expect(onError.choices).toEqual([
      {
        value: "stop",
        label: "Stop the run",
        description:
          "The run stops with an incident you can retry or resolve in Operate.",
      },
      {
        value: "continue",
        label: "Continue",
        description:
          "The run goes on. This step's output is empty (null), and later steps can read its error.",
      },
      {
        value: "errorOutput",
        label: "Continue with error output",
        description:
          "Failures take this step's Error output on the canvas; its steps run, then the run goes on after this step.",
      },
    ]);
    expect(result).toMatchObject({
      path: ["errorPath", "output"],
      label: "Result on error",
      mapping: "both",
      hint: "What later steps read as this step's output after the error path.",
    });
    const ctx = context();
    expect(result.showWhen!(step("action"), ctx)).toBe(false);
    expect(
      result.showWhen!(
        step("action", { onError: "errorOutput", errorPath: path() }),
        ctx,
      ),
    ).toBe(true);
    const [readOnly] = onErrorFields(step("parallel"), ctx);
    expect(readOnly.readOnly!(step("parallel"), ctx)).toBe(STOP_ONLY_REASON);
  });
});

describe("what each choice writes", () => {
  it("adds an empty error path whose result is null for Continue with error output", () => {
    for (const from of [
      step("action"),
      step("action", { onError: "continue" }),
    ])
      expect(writeOnError(from, "errorOutput")).toEqual({
        patch: [
          { path: ["onError"], value: "errorOutput" },
          {
            path: ["errorPath"],
            value: { steps: [], output: { literal: null } },
          },
        ],
        removed: 0,
        names: [],
      });
    // An error path the step already holds stays.
    expect(
      writeOnError(
        step("action", { onError: "errorOutput", errorPath: path("tell") }),
        "errorOutput",
      ).patch,
    ).toEqual([{ path: ["onError"], value: "errorOutput" }]);
  });

  it("removes the error path for Continue, counting every step it held", () => {
    const nested = path("notify-team", "flag-request");
    nested.steps.push({
      id: "route",
      kind: "switch",
      cases: [
        {
          when: { literal: true },
          steps: [{ id: "deep", kind: "wait" }],
          output: null,
        },
      ],
      default: { steps: [], output: null },
    } as never);
    expect(
      writeOnError(
        step("action", { onError: "errorOutput", errorPath: nested }),
        "continue",
      ),
    ).toEqual({
      patch: [
        { path: ["onError"], value: "continue" },
        { path: ["errorPath"], value: undefined },
      ],
      removed: 4,
      names: ["notify-team", "flag-request", "route", "deep"],
    });
  });

  it("writes Stop the run by leaving the setting out, never as stop", () => {
    const write = writeOnError(
      step("action", { onError: "errorOutput", errorPath: path("tell") }),
      "stop",
    );
    expect(write.patch).toEqual([
      { path: ["onError"], value: undefined },
      { path: ["errorPath"], value: undefined },
    ]);
    expect(write.removed).toBe(1);
    expect(JSON.stringify(write.patch)).not.toContain('"stop"');
  });

  it("names the removed steps in the toast", () => {
    expect(removedPathToast(0, [])).toBeNull();
    expect(removedPathToast(1, ["notify-team"])).toEqual({
      text: "Removed the error path (1 step)",
      detail: "notify-team was in it.",
    });
    expect(removedPathToast(2, ["notify-team", "flag-request"])).toEqual({
      text: "Removed the error path (2 steps)",
      detail: "notify-team and flag-request were in it.",
    });
    expect(removedPathToast(3, ["a", "b", "c"])?.detail).toBe(
      "a, b and c were in it.",
    );
    expect(
      removedPathToast(5, ["notify-team", "flag-request", "c", "d", "e"])
        ?.detail,
    ).toBe("notify-team, flag-request and 3 more were in it.");
  });

  it("ends On error when a call stops waiting for its result", () => {
    const call = (extra: Record<string, unknown>) =>
      step("callWorkflow", { uses: "notify-customer@1.0.0", ...extra });
    expect(
      endedOnError(
        call({
          mode: "detach",
          onError: "errorOutput",
          errorPath: path("tell"),
        }),
      ),
    ).toMatchObject({
      patch: [
        { path: ["onError"], value: undefined },
        { path: ["errorPath"], value: undefined },
      ],
      removed: 1,
    });
    expect(
      endedOnError(call({ mode: "wait", onError: "continue" })),
    ).toBeNull();
    expect(endedOnError(call({ mode: "detach" }))).toBeNull();
    expect(endedOnError(step("action", { onError: "continue" }))).toBeNull();
  });
});

describe("the compiler's CALL_MODE finding, by the field it is at", () => {
  it("names On error at /onError and If it fails at /onFailure", () => {
    const at = (path: string) =>
      describeDiagnostic({
        code: "WV-COMP-CALL_MODE",
        severity: "error",
        message: "onError applies only when the call waits for its result.",
        path,
      });
    expect(at("/spec/steps/0/onError")).toMatchObject({
      text: "On error applies only when the call waits for its result.",
      hint: "Set When it runs to Wait for its result, or set On error to Stop the run.",
    });
    expect(at("/spec/steps/0/onFailure").text).toBe(
      "This step can continue after the called run fails only when it waits for the called workflow's result.",
    );
  });
});

/** Step details on a workflow with submit (an action) and notify (a call). */
function setup(manifest: Record<string, unknown>) {
  const model = new StructuredCanvasAdapter();
  model.insert("action", "root", undefined, {
    id: "submit",
    uses: "intake.submit@1.0.0",
    with: { literal: {} },
  });
  model.insert("callWorkflow" as never, "root", undefined, {
    id: "notify",
    uses: "notify-customer@1.0.0",
    with: { literal: {} },
    mode: "wait",
    onError: "errorOutput",
    errorPath: path("notify-team", "flag-request"),
  });
  model.canvas = withOwnedAction(model.canvas, "intake.submit@1.0.0", {
    ...newHttpRecipe(),
    method: "POST",
  });
  model.clearHistory();
  const toasts: { text: string; action?: ToastAction; detail?: string }[] = [];
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: (
      text: string,
      action?: ToastAction,
      _tone?: "neutral" | "danger",
      detail?: string,
    ) => void toasts.push({ text, action, detail }),
    undo: () => model.undo(),
    undoIfUnchanged: async (still: () => boolean) => {
      if (!still()) return "changed" as const;
      model.undo();
      return "undone" as const;
    },
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: { request: async () => manifest } as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
  } as unknown as ControllerHost;
  const controller = new StepDetailsController(host, () => 1000);
  return { model, controller, toasts };
}
const MANIFEST = {
  features: ["flow.onError", "flow.callWorkflow"],
  step_fields: [{ name: "onError", kinds: KINDS }],
};
const stepOf = (model: StructuredCanvasAdapter, id: string) =>
  model.definition.spec.steps.find((item) => item.id === id)!;

describe("On error in step details", () => {
  it("comes after the kind's own settings once the platform's manifest is read", async () => {
    const { controller } = setup(MANIFEST);
    const before = controller.settings("submit").fields.map((spec) => spec.id);
    expect(before.at(-1)).toBe("onError");
    expect(controller.settings("submit").fields[0].id).toBe("retry");
    await controller.loadFeatures();
    const ids = controller.settings("submit").fields.map((spec) => spec.id);
    expect(ids.slice(0, 1)).toEqual(["retry"]);
    expect(ids.slice(-2)).toEqual(["onError", "resultOnError"]);
    expect(controller.onErrorContext().onErrorKinds).toEqual(KINDS);
  });

  it("writes Continue with error output as one undo step", async () => {
    const { model, controller } = setup(MANIFEST);
    await controller.loadFeatures();
    const before = model.source;
    const write = writeOnError(stepOf(model, "submit"), "errorOutput");
    controller.commit(
      "submit",
      write.patch.map((edit) => ({ scope: "step" as const, ...edit })),
      "onError",
    );
    expect(stepOf(model, "submit")).toMatchObject({
      onError: "errorOutput",
      errorPath: { steps: [], output: { literal: null } },
    });
    expect(model.owners().get("submit/errorPath")).toEqual([]);
    model.undo();
    expect(model.source).toBe(before);
  });

  it("maps Result on error to data like any expression field", async () => {
    const { model, controller } = setup(MANIFEST);
    await controller.loadFeatures();
    const result = controller
      .settings("notify")
      .fields.find((spec) => spec.id === "resultOnError")!;
    const subject = controller.subject("notify");
    expect(readParam(subject, result)).toEqual({
      mode: "mapped",
      expression: { ref: "/steps/submit/error/message" },
    });
    controller.commit(
      "notify",
      writeParam(subject, result, {
        mode: "mapped",
        expression: { ref: "/steps/flag-request/output" },
      }),
      result.id,
    );
    expect(stepOf(model, "notify")["errorPath"]).toMatchObject({
      output: { ref: "/steps/flag-request/output" },
    });
  });

  it("removes On error and the error path when a call stops waiting, in the same undo step", async () => {
    const { model, controller, toasts } = setup(MANIFEST);
    await controller.loadFeatures();
    const before = model.source;
    const mode = controller
      .parameters("notify")
      .fields.find((spec) => spec.id === "mode")!;
    controller.commit(
      "notify",
      [{ scope: "step", path: mode.path, value: "detach" }],
      mode.id,
    );
    const notify = stepOf(model, "notify");
    expect(notify["mode"]).toBe("detach");
    expect(notify).not.toHaveProperty("onError");
    expect(notify).not.toHaveProperty("errorPath");
    expect(model.stepIdSet().has("notify-team")).toBe(false);
    expect(toasts.at(-1)).toMatchObject({
      text: "Removed the error path (2 steps)",
      detail: "notify-team and flag-request were in it.",
    });
    await toasts.at(-1)!.action!.run();
    expect(model.source).toBe(before);
  });
});
