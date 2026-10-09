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
import { newHttpRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import { fixed, writeParam } from "../src/app/editor/ndv/params/value-io";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { NO_PREVIEW } from "../src/app/editor/ndv/params/preview";
import { stepDetailsEnabled } from "../src/app/editor/ndv/step-details-host";
import { CANVAS_MAX_BYTES } from "../src/app/editor/state/test-data";
import { CANVAS_TOO_BIG } from "../src/app/editor/state/canvas-sidecar";
import {
  StepDetailsService,
  openRequest,
} from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter } from "../src/app/model";
import type { ToastAction } from "../src/app/toast";

beforeAll(() => loadKindRegistrations());

function setup() {
  const model = new StructuredCanvasAdapter();
  model.insert("wait", "root", undefined, { id: "pause" });
  model.insert("signal", "root", undefined, { id: "listen", name: "paid" });
  model.insert("action", "root", undefined, {
    id: "get",
    uses: "untitled-workflow.get@1.0.0",
    with: { literal: {} },
  });
  model.canvas = withOwnedAction(model.canvas, "untitled-workflow.get@1.0.0", {
    ...newHttpRecipe(),
    method: "POST",
  });
  model.clearHistory();
  const toasts: { text: string; action?: ToastAction }[] = [];
  const errors: string[] = [];
  let clock = 1000;
  const host: { -readonly [K in keyof ControllerHost]: ControllerHost[K] } = {
    model,
    perform: (edit) => {
      try {
        edit();
      } catch (error) {
        errors.push((error as Error).message);
      }
    },
    notify: (text, action) => void toasts.push({ text, action }),
    undo: () => model.undo(),
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind) => kind,
    editingLocked: false,
    profile: null,
    api: { request: async () => ({ features: ["text.concat"] }) } as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
  };
  const controller = new StepDetailsController(host, () => clock);
  return {
    model,
    host,
    controller,
    toasts,
    errors,
    tick: (ms: number) => (clock += ms),
  };
}
const durationOf = (model: StructuredCanvasAdapter) =>
  model.definition.spec.steps.find((s) => s.id === "pause")!["durationSeconds"];

describe("the step details controller", () => {
  it("opens on the Parameters tab with focus on the first field by default", () => {
    expect(openRequest("pause")).toEqual({
      target: "pause",
      tab: "parameters",
      focus: { kind: "first" },
      fresh: false,
      revealAll: false,
    });
  });
  it("writes an edit as one undo step", () => {
    const { model, controller } = setup();
    const spec = controller.parameters("pause").fields[0];
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), spec, fixed(120)),
      spec.id,
    );
    expect(durationOf(model)).toBe(120);
    model.undo();
    expect(durationOf(model)).toBe(60);
  });
  it("merges a burst of one field", () => {
    const { model, controller, tick } = setup();
    const spec = controller.parameters("pause").fields[0];
    for (const seconds of [61, 62, 63]) {
      controller.commit(
        "pause",
        writeParam(controller.subject("pause"), spec, fixed(seconds)),
        spec.id,
      );
      tick(200);
    }
    model.undo();
    expect(durationOf(model)).toBe(60);
  });
  it("starts a new undo step after 600 ms", () => {
    const { model, controller, tick } = setup();
    const spec = controller.parameters("pause").fields[0];
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), spec, fixed(61)),
      spec.id,
    );
    tick(700);
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), spec, fixed(62)),
      spec.id,
    );
    model.undo();
    expect(durationOf(model)).toBe(61);
  });
  it("starts a new undo step for another field", () => {
    const { model, controller } = setup();
    const wait = controller.parameters("pause").fields[0];
    const name = controller.parameters("listen").fields[0];
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), wait, fixed(61)),
      wait.id,
    );
    controller.commit(
      "listen",
      writeParam(controller.subject("listen"), name, fixed("received")),
      name.id,
    );
    model.undo();
    expect(durationOf(model)).toBe(61);
  });
  it("starts a new undo step after moving to another step", () => {
    const { model, controller } = setup();
    const wait = controller.parameters("pause").fields[0];
    const name = controller.parameters("listen").fields[0];
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), wait, fixed(61)),
      wait.id,
    );
    controller.commit(
      "listen",
      writeParam(controller.subject("listen"), name, fixed("received")),
      name.id,
    );
    controller.commit(
      "pause",
      writeParam(controller.subject("pause"), wait, fixed(62)),
      wait.id,
    );
    model.undo();
    expect(durationOf(model)).toBe(61);
  });
  it("writes the owned action and the step in the same undo step", () => {
    const { model, controller } = setup();
    const url = controller
      .parameters("get")
      .fields.find((f) => f.id === "url")!;
    const changes = [
      ...writeParam(controller.subject("get"), url, fixed("/orders/{id}")),
      {
        scope: "step" as const,
        path: ["with"],
        value: { object: { path: { object: { id: { ref: "/input/id" } } } } },
      },
    ];
    controller.commit("get", changes, url.id);
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ pathTemplate: "/orders/{id}" });
    model.undo();
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ pathTemplate: "" });
    expect(
      model.definition.spec.steps.find((s) => s.id === "get")!["with"],
    ).toEqual({ literal: {} });
  });
  it("clears the body when the method stops sending one, and offers Undo", () => {
    const { model, controller, toasts } = setup();
    const body = controller
      .parameters("get")
      .options!.find((f) => f.id === "body")!;
    controller.commit(
      "get",
      writeParam(
        controller.subject("get"),
        { ...body, path: ["with", "body", "note"] },
        fixed("hi"),
      ),
      "body",
    );
    const method = controller
      .parameters("get")
      .fields.find((f) => f.id === "method")!;
    const outcome = controller.commit(
      "get",
      writeParam(controller.subject("get"), method, fixed("GET")),
      method.id,
    );
    expect(outcome.removed).toEqual(["Body"]);
    expect(
      model.definition.spec.steps.find((s) => s.id === "get")!["with"],
    ).toEqual({ literal: {} });
    expect(toasts.at(-1)?.text).toBe(
      "Removed Body. Only for POST, PUT and PATCH.",
    );
    toasts.at(-1)?.action?.run();
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ method: "POST" });
  });
  it("writes nothing while the source has an error", () => {
    const { model, controller } = setup();
    model.readonly = true;
    const spec = controller.parameters("pause").fields[0];
    expect(controller.readOnlyReason()).toBe(
      "Fix the source to edit this step.",
    );
    expect(
      controller.commit(
        "pause",
        writeParam(controller.subject("pause"), spec, fixed(5)),
        spec.id,
      ).ok,
    ).toBe(false);
  });
  it("gives registered parameter components a step details context", async () => {
    const { controller } = setup();
    const ctx = controller.ndvContext("pause");
    expect(ctx.version).toBe(2);
    expect(ctx.scope(["durationSeconds"]).found).toBe(true);
    ctx.edit([{ path: ["durationSeconds"], value: 30 }], "Wait for");
    expect(controller.step("pause")!["durationSeconds"]).toBe(30);
    expect(ctx.evaluate({ ref: "/input" }, ["durationSeconds"])).toEqual({
      ok: false,
    });
    expect(await ctx.catalog.tables()).toEqual([]);
    await controller.loadFeatures();
    expect(controller.features()).toEqual(["text.concat"]);
  });
  it("remembers options added in this session, per step", () => {
    const { controller } = setup();
    controller.addOption("get", "headers");
    expect([...controller.added("get")]).toEqual(["headers"]);
    expect([...controller.added("pause")]).toEqual([]);
  });
});

describe("step details requests", () => {
  it("opens only one dialog and reapplies focus for the same step", () => {
    const service = new StepDetailsService();
    service.openWorkflowSettings();
    expect(service.isOpen()).toBe(true);
    const request = openRequest("listen", {
      tab: "settings",
      focus: { kind: "field", id: "timeout", tab: "settings" },
      fresh: true,
    });
    service.open(request);
    service.open(request);
    expect(service.request()).toEqual(request);
    expect(service.opening()).toBe(2);
    expect(service.workflowSettings()).toBe(false);
    service.close();
    expect(service.isOpen()).toBe(false);
    service.open(openRequest("$trigger"));
    service.openWorkflowSettings();
    expect(service.request()).toBeNull();
    service.closeWorkflowSettings();
    expect(service.isOpen()).toBe(false);
  });
  it("uses the host's editor preference when provided", () => {
    expect(stepDetailsEnabled({ editorNext: true })).toBe(true);
    expect(stepDetailsEnabled({ editorNext: false })).toBe(false);
  });
  it("has no resolved preview before a preview provider is connected", () => {
    expect(
      NO_PREVIEW.preview(
        { literal: 1 },
        [],
        setup().controller.ndvContext("pause"),
      ),
    ).toBeNull();
  });
});

describe("step details edits and failures", () => {
  it("does not edit while a simulation is running", () => {
    const { host, model, controller } = setup();
    const before = model.source;
    host.editingLocked = true;
    expect(controller.readOnlyReason()).toBe(
      "A simulation is running. Stop it to edit.",
    );
    expect(
      controller.commit(
        "pause",
        [{ scope: "step", path: ["durationSeconds"], value: 5 }],
        "duration",
      ).ok,
    ).toBe(false);
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
    expect(controller.ndvContext("pause").readOnly).toBe(true);
  });
  it("uses a new undo step at exactly 600 ms and after an external edit", () => {
    const { model, controller, tick } = setup();
    const write = (value: number) =>
      controller.commit(
        "pause",
        [{ scope: "step", path: ["durationSeconds"], value }],
        "duration",
      );
    write(61);
    tick(600);
    write(62);
    model.undo();
    expect(durationOf(model)).toBe(61);
    model.update(
      "listen",
      JSON.stringify({ ...controller.step("listen"), name: "other" }),
    );
    write(63);
    model.undo();
    expect(durationOf(model)).toBe(61);
    expect(controller.step("listen")!["name"]).toBe("other");
  });
  it("rolls back mixed edits over the canvas limit and preserves redo", () => {
    const { model, controller, errors } = setup();
    controller.commit(
      "pause",
      [{ scope: "step", path: ["durationSeconds"], value: 120 }],
      "duration",
    );
    model.undo();
    const before = {
      definition: structuredClone(model.definition),
      canvas: structuredClone(model.canvas),
      source: model.source,
    };
    const outcome = controller.commit(
      "get",
      [
        { scope: "step", path: ["connection"], value: "changed" },
        { scope: "workflow", path: ["metadata", "version"], value: "1.0.1" },
        {
          scope: "action",
          path: ["pathTemplate"],
          value: "x".repeat(CANVAS_MAX_BYTES),
        },
      ],
      "url",
    );
    expect(outcome).toEqual({ ok: false, removed: [] });
    expect(errors).toEqual([CANVAS_TOO_BIG]);
    expect({
      definition: model.definition,
      canvas: model.canvas,
      source: model.source,
    }).toEqual(before);
    expect(model.canUndo).toBe(false);
    expect(model.canRedo).toBe(true);
    model.redo();
    expect(durationOf(model)).toBe(120);
  });
  it("rolls back an oversized edit in a merged burst", () => {
    const { model, controller, errors } = setup();
    const write = (value: string) =>
      controller.commit(
        "get",
        [{ scope: "action", path: ["pathTemplate"], value }],
        "url",
      );
    write("/orders");
    const source = model.source;
    expect(write("x".repeat(CANVAS_MAX_BYTES)).ok).toBe(false);
    expect(errors).toEqual([CANVAS_TOO_BIG]);
    expect(model.source).toBe(source);
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ pathTemplate: "/orders" });
    model.undo();
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ pathTemplate: "" });
  });
  it("refuses edits of identity, missing steps and actions the workflow does not own", () => {
    const { model, controller, errors } = setup();
    const before = model.source;
    expect(
      controller.commit(
        "pause",
        [{ scope: "step", path: ["id"], value: "other" }],
        "id",
      ).ok,
    ).toBe(false);
    expect(
      controller.commit(
        "missing",
        [{ scope: "step", path: ["durationSeconds"], value: 5 }],
        "duration",
      ).ok,
    ).toBe(false);
    expect(
      controller.commit(
        "pause",
        [{ scope: "action", path: ["timeoutSeconds"], value: 5 }],
        "timeout",
      ).ok,
    ).toBe(false);
    expect(errors).toHaveLength(3);
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });
  it("reports invalid list positions through the shell and writes nothing", () => {
    const { model, controller, errors } = setup();
    const before = model.source;
    expect(
      controller.commit(
        "listen",
        [{ scope: "step", path: ["name", 2], value: "invalid" }],
        "name",
      ),
    ).toEqual({ ok: false, removed: [] });
    expect(errors).toEqual(["That field isn't a list, so it has no items."]);
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });
  it("keeps session options when removing one is refused", () => {
    const { model, controller } = setup();
    const headers = controller
      .parameters("get")
      .options!.find((field) => field.id === "headers")!;
    controller.addOption("get", headers.id);
    model.readonly = true;
    expect(controller.removeOption("get", headers).ok).toBe(false);
    expect([...controller.added("get")]).toEqual(["headers"]);
  });
  it("clears an option hidden after its removal was undone", () => {
    const { model, controller, toasts } = setup();
    const body = controller
      .parameters("get")
      .options!.find((field) => field.id === "body")!;
    controller.commit(
      "get",
      writeParam(
        controller.subject("get"),
        { ...body, path: ["with", "body", "note"] },
        fixed("hi"),
      ),
      "body",
    );
    expect(controller.removeOption("get", body).ok).toBe(true);
    model.undo();
    expect(controller.step("get")!["with"]).toEqual({
      literal: { body: { note: "hi" } },
    });
    const method = controller
      .parameters("get")
      .fields.find((field) => field.id === "method")!;
    expect(
      controller.commit(
        "get",
        writeParam(controller.subject("get"), method, fixed("GET")),
        method.id,
      ).removed,
    ).toEqual(["Body"]);
    expect(controller.step("get")!["with"]).toEqual({ literal: {} });
    const undo = toasts.at(-1)!.action!;
    controller.commit(
      "pause",
      [{ scope: "step", path: ["durationSeconds"], value: 80 }],
      "duration",
    );
    undo.run();
    expect(durationOf(model)).toBe(80);
    expect(
      ownedActionsOf(model.canvas)["untitled-workflow.get@1.0.0"],
    ).toMatchObject({ method: "GET" });
  });
  it("forgets touched fields and session options when another workflow opens", () => {
    const { model, controller } = setup();
    controller.touch("get", "url");
    controller.addOption("get", "headers");
    model.replace(structuredClone(model.definition));
    expect([...controller.touched("get")]).toEqual([]);
    expect([...controller.added("get")]).toEqual([]);
  });
  it("ignores a registered component's old edit callback after another workflow opens", () => {
    const { model, controller } = setup();
    const context = controller.ndvContext("pause");
    model.replace(structuredClone(model.definition));
    context.edit([{ path: ["durationSeconds"], value: 5 }], "Wait for");
    expect(durationOf(model)).toBe(60);
    expect(model.canUndo).toBe(false);
  });
});

describe("step details context", () => {
  it("edits trigger, result and workflow fields through their workflow scopes", () => {
    const { model, controller } = setup();
    for (const [target, id, value] of [
      ["$trigger", "inputSchema", { type: "string" }],
      ["$end", "result", { accepted: true }],
      ["$workflow", "name", "orders"],
    ] as const) {
      const spec = controller
        .parameters(target)
        .fields.find((field) => field.id === id)!;
      expect(
        controller.commit(
          target,
          writeParam(controller.subject(target), spec, fixed(value)),
          spec.id,
        ).ok,
      ).toBe(true);
    }
    expect(model.definition.spec.inputSchema).toEqual({ type: "string" });
    expect(model.definition.spec.output).toEqual({
      literal: { accepted: true },
    });
    expect(model.definition.metadata.name).toBe("orders");
    model.undo();
    expect(model.definition.metadata.name).toBe("untitled-workflow");
    expect(model.definition.spec.output).toEqual({
      literal: { accepted: true },
    });
  });
  it("filters field diagnostics on pointer segment boundaries", () => {
    const { host, controller, model } = setup();
    host.diagnosticsDefinition = model.definition;
    host.diagnostics = {
      validationOk: false,
      errorCount: 3,
      diagnostics: [
        {
          path: "/spec/steps/0/durationSeconds",
          message: "Wait needs a duration.",
        },
        {
          path: "/spec/steps/0/durationSecondsExtra",
          message: "Another field.",
        },
        {
          path: "/spec/steps/0/durationSeconds/value",
          message: "Nested field.",
        },
      ],
    };
    expect(
      controller
        .ndvContext("pause")
        .diagnostics(["durationSeconds"])
        .map((diagnostic) => diagnostic.message),
    ).toEqual(["Wait needs a duration.", "Nested field."]);
  });
  it("routes workflow diagnostics to the trigger, End or workflow settings", () => {
    const { host, controller, model } = setup();
    host.diagnosticsDefinition = model.definition;
    host.diagnostics = {
      validationOk: false,
      errorCount: 3,
      diagnostics: [
        { path: "/spec/inputSchema", message: "Input problem." },
        { path: "/spec/output", message: "Result problem." },
        { path: "/metadata/version", message: "Version problem." },
      ],
    };
    expect(
      controller
        .diagnostics("$trigger")
        .map((diagnostic) => diagnostic.message),
    ).toEqual(["Input problem."]);
    expect(
      controller.diagnostics("$end").map((diagnostic) => diagnostic.message),
    ).toEqual(["Result problem."]);
    expect(controller.diagnostics("$workflow")).toHaveLength(3);
  });
  it("delegates resolved values to the supplied preview provider", () => {
    const { host } = setup();
    let requested: unknown;
    const controller = new StepDetailsController(host, undefined, {
      preview: (expression, path, context) => {
        requested = { expression, path, step: context.step.id };
        return { ok: true, value: 7 };
      },
    });
    expect(
      controller
        .ndvContext("pause")
        .evaluate({ literal: 7 }, ["durationSeconds"]),
    ).toEqual({ ok: true, value: 7 });
    expect(requested).toEqual({
      expression: { literal: 7 },
      path: ["durationSeconds"],
      step: "pause",
    });
  });

  it("exposes no-data samples and local connection slots without choosing a binding", async () => {
    const { controller, model } = setup();
    model.definition.spec["connections"] = {
      api: { connector: "weave-http@1.0.0", required: false },
    };
    const context = controller.ndvContext("get");
    expect(context.connections.slot("api")).toEqual({
      connector: "weave-http@1.0.0",
      required: false,
    });
    expect(context.connections.slot("missing")).toBeNull();
    expect(context.connections.devBinding("api")).toBeNull();
    expect(context.sample.context([])).toBeNull();
    expect(context.sample.resolvedInput()).toEqual({ state: "none" });
    expect(context.sample.output()).toEqual({ state: "none" });
    expect(await context.sample.setPin(null)).toEqual({
      ok: false,
      message: "Pinning output isn't available yet.",
    });
    expect(await context.sample.setScript(null)).toEqual({
      ok: false,
      message: "Test scripts aren't available yet.",
    });
  });
});

describe("language feature loading", () => {
  it("shares a load and permits retry after a transient failure", async () => {
    const { host, controller } = setup();
    let calls = 0;
    host.api = {
      request: async () => {
        if (++calls === 1) throw new Error("offline");
        return { features: ["text.concat"] };
      },
    } as never;
    await Promise.all([controller.loadFeatures(), controller.loadFeatures()]);
    expect(controller.features()).toEqual([]);
    await controller.loadFeatures();
    expect(controller.features()).toEqual(["text.concat"]);
    expect(calls).toBe(2);
  });
  it("does not apply a manifest returned after the workspace changed", async () => {
    const { host, controller } = setup();
    let finish!: (value: { features: string[] }) => void;
    const requests: string[] = [];
    const pending = new Promise<{ features: string[] }>((resolve) => {
      finish = resolve;
    });
    host.api = {
      project: "/project",
      request: async (path: string) => {
        requests.push(path);
        return path === "/studio/contracts/language"
          ? pending
          : { features: ["text.join"] };
      },
    } as never;
    const old = controller.loadFeatures();
    host.profile = {
      name: "other",
      baseUrl: "https://example.invalid",
      tenantId: "tenant",
      projectId: "project",
      environmentId: null,
    };
    const current = controller.loadFeatures();
    finish({ features: ["text.concat"] });
    await Promise.all([old, current]);
    expect(controller.features()).toEqual(["text.join"]);
    expect(requests).toEqual([
      "/studio/contracts/language",
      "/project/language",
    ]);
  });
});

describe("step details data preservation", () => {
  it("edits the registered nested step even when data contains a matching ID", () => {
    const { model, controller } = setup();
    const definition = structuredClone(model.definition);
    definition.spec.steps[0]["extra"] = {
      steps: [{ id: "nested", kind: "wait", durationSeconds: 999 }],
    };
    definition.spec.steps.push({
      id: "choose",
      kind: "switch",
      cases: [
        {
          when: { literal: true },
          steps: [{ id: "nested", kind: "wait", durationSeconds: 60 }],
          output: { literal: null },
        },
      ],
      default: { steps: [], output: { literal: null } },
    });
    model.replace(definition);
    expect(
      controller.commit(
        "nested",
        [{ scope: "step", path: ["durationSeconds"], value: 75 }],
        "duration",
      ).ok,
    ).toBe(true);
    expect(controller.step("nested")!["durationSeconds"]).toBe(75);
    expect(model.definition.spec.steps[0]["extra"]).toEqual(
      definition.spec.steps[0]["extra"],
    );
    model.undo();
    expect(controller.step("nested")!["durationSeconds"]).toBe(60);
  });
  it("preserves opaque formulas when another field changes", () => {
    const { model, controller } = setup();
    const formula = {
      op: { name: "future.operation", args: [{ ref: "/input" }] },
    };
    model.update(
      "get",
      JSON.stringify({ ...controller.step("get"), with: formula }),
    );
    model.clearHistory();
    const url = controller
      .parameters("get")
      .fields.find((field) => field.id === "url")!;
    expect(
      controller.commit(
        "get",
        writeParam(controller.subject("get"), url, fixed("/orders")),
        url.id,
      ).ok,
    ).toBe(true);
    expect(controller.step("get")!["with"]).toEqual(formula);
    model.undo();
    expect(controller.step("get")!["with"]).toEqual(formula);
  });
  it("keeps secret input fields out of published action forms", () => {
    const { model, host, controller } = setup();
    model.update(
      "get",
      JSON.stringify({
        ...controller.step("get"),
        uses: "published@2.0.0",
        with: { literal: "dummy" },
      }),
    );
    host.catalogContracts = new Map([
      [
        "published@2.0.0",
        { spec: { inputSchema: { type: "string", "x-secret": true } } },
      ],
    ]);
    const source = model.source;
    expect(
      controller.parameters("get").fields.map((field) => field.id),
    ).toEqual(["action"]);
    expect(controller.parameters("get").options ?? []).toEqual([]);
    expect(model.source).toBe(source);
  });
});

describe("connected step catalog", () => {
  it("does not return decision tables from a workspace that was left while loading", async () => {
    const { host, controller } = setup();
    let finish!: (value: { items: Record<string, unknown>[] }) => void;
    const pending = new Promise<{ items: Record<string, unknown>[] }>(
      (resolve) => {
        finish = resolve;
      },
    );
    host.profile = {
      name: "old",
      baseUrl: "https://example.invalid",
      tenantId: "tenant",
      projectId: "old",
      environmentId: null,
    };
    host.api = { page: async () => pending } as never;
    const result = controller.ndvContext("get").catalog.tables();
    host.profile = { ...host.profile, name: "new", projectId: "new" };
    finish({ items: [{ name: "old-table", version: "1.0.0" }] });
    expect(await result).toEqual([]);
  });
  it("offers published table versions only with catalog access", async () => {
    const { host, controller } = setup();
    host.profile = {
      name: "connected",
      baseUrl: "https://example.invalid",
      tenantId: "tenant",
      projectId: "project",
      environmentId: null,
    };
    host.api = {
      page: async () => ({ items: [{ name: "pricing", version: "2.0.0" }] }),
    } as never;
    const context = controller.ndvContext("get");
    expect(await context.catalog.tables()).toEqual([
      { uses: "pricing@2.0.0", title: "pricing@2.0.0" },
    ]);
    host.can = () => false;
    expect(await context.catalog.tables()).toEqual([]);
  });
});

describe("manual test event ownership", () => {
  it("retains a null sample across dialog openings without persisting it", () => {
    const { model, controller } = setup();
    const before = JSON.stringify([model.definition, model.canvas]);
    expect(controller.testEvent()).toBeUndefined();
    controller.setTestEvent(null);
    const details = new StepDetailsService();
    details.open(openRequest("$trigger"));
    details.close();
    details.open(openRequest("$trigger"));
    expect(controller.testEvent()).toBeNull();
    expect(JSON.stringify([model.definition, model.canvas])).toBe(before);
  });
  it.each(["workflow", "profile", "account"])(
    "clears samples and rejects a stale writer after %s changes",
    (scope) => {
      const { model, host, controller } = setup();
      controller.setTestEvent({ label: "old" });
      const current = controller.testEventOwner();
      if (scope === "workflow") model.clearHistory();
      if (scope === "profile") host.profile = { id: "different" } as never;
      if (scope === "account") host.api = {} as never;
      expect(controller.testEvent()).toBeUndefined();
      expect(current()).toBe(false);
    },
  );
  it("strips nested secret values at the sample boundary", () => {
    const { model, controller } = setup();
    model.updateWorkflow({
      ...model.definition,
      spec: {
        ...model.definition.spec,
        inputSchema: {
          type: "object",
          properties: {
            rows: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  token: { type: "string", writeOnly: true },
                  name: { type: "string" },
                },
              },
            },
          },
        },
      },
    });
    controller.setTestEvent({ rows: [{ token: "private", name: "public" }] });
    expect(controller.testEvent()).toEqual({ rows: [{ name: "public" }] });
  });
});

it("redacts a stored sample when an input field becomes secret", () => {
  const { model, controller } = setup();
  controller.setTestEvent({ token: "private", name: "public" });
  model.updateWorkflow({
    ...model.definition,
    spec: {
      ...model.definition.spec,
      inputSchema: {
        type: "object",
        properties: {
          token: { type: "string", writeOnly: true },
          name: { type: "string" },
        },
      },
    },
  });
  expect(controller.testEvent()).toEqual({ name: "public" });
});

it("keeps composed secrets out of the sample store and serialized workflow", () => {
  const { model, controller } = setup();
  model.updateWorkflow({
    ...model.definition,
    spec: {
      ...model.definition.spec,
      inputSchema: {
        type: "object",
        properties: {
          token: {
            allOf: [
              { type: "string", writeOnly: false },
              { type: "string", writeOnly: true },
            ],
          },
          name: { type: "string" },
        },
      },
    },
  });
  const before = JSON.stringify([model.definition, model.canvas]);
  controller.setTestEvent({ token: "private", name: "public" });
  expect(controller.testEvent()).toEqual({ name: "public" });
  expect(JSON.stringify([model.definition, model.canvas])).toBe(before);
});
