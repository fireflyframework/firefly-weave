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
// How a step inside a transaction is undone: which steps choose, what each
// choice writes and drops, and what Settings shows. Studio never chooses for
// the builder: opening Settings writes nothing, and the compiler's suggested
// edit for a step without a choice is never offered.
import "@angular/compiler";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { ToastAction } from "../src/app/dialog";
import { fixFor } from "../src/app/editor/ndv/issues";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { newHttpRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import { withOwnedAction } from "../src/app/editor/ndv/owned/owned-store";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import {
  fixed,
  readParam,
  writeParam,
  type ParamValue,
} from "../src/app/editor/ndv/params/value-io";
import type { Edit, Json } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import {
  copySteps,
  planPaste,
  readClipboard,
} from "../src/app/editor/state/clipboard";
import {
  clearUndo,
  removedUndoText,
  undoChoiceOf,
  undoOperation,
  undoRow,
  writeUndo,
  type UndoChoice,
} from "../src/app/editor/transactions/undo";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

/** The manifest's entry for how a step is undone, as the platform lists it. */
const UNDO = {
  name: "undo",
  feature: "flow.transaction",
  kinds: ["action", "callWorkflow"],
  values: ["reserve", "compensate", "irreversible"],
  paths: {
    reserve: ["/confirm", "/cancel", "/holdSeconds"],
    compensate: ["/compensate"],
    irreversible: ["/irreversible"],
  },
  only_inside: "transaction",
  studio: "ready" as const,
};
const MANIFEST = {
  features: ["flow.transaction", "flow.onError", "flow.callWorkflow"],
  step_fields: [
    UNDO,
    { name: "onError", kinds: ["action", "callWorkflow", "transaction"] },
  ],
};
const ADD = "intake.add@1.0.0";
const REMOVE = {
  kind: "action",
  uses: "target-system.remove-quantity@1.0.0",
  with: { object: { reference: { ref: "/input/moveId" } } },
};
const CONFIRM = {
  kind: "action",
  uses: "source-system.confirm-hold@1.0.0",
  with: { literal: {} },
};
const RELEASE = { ...CONFIRM, uses: "source-system.release-hold@1.0.0" };
const add = (extra: Record<string, unknown> = {}): Step =>
  ({
    id: "add",
    kind: "action",
    uses: ADD,
    with: { literal: {} },
    ...extra,
  }) as unknown as Step;
const reverse = (extra: Record<string, unknown> = {}): Step =>
  ({
    id: "reverse",
    kind: "callWorkflow",
    uses: "reverse-target-addition@1.0.0",
    mode: "wait",
    with: { literal: {} },
    ...extra,
  }) as unknown as Step;
const move = (
  steps: readonly Step[],
  extra: Record<string, unknown> = {},
): Step =>
  ({
    id: "move",
    kind: "transaction",
    steps,
    output: { literal: null },
    ...extra,
  }) as unknown as Step;
const workflow = (...steps: Step[]): Workflow => {
  const value = freshWorkflow();
  // As a document read from a file: no object is shared.
  value.spec.steps = JSON.parse(JSON.stringify(steps)) as Step[];
  return value;
};

const row = (step: Step, extra: Partial<Parameters<typeof undoRow>[1]> = {}) =>
  undoRow(step, {
    inTransaction: move([step]),
    features: MANIFEST.features,
    manifest: MANIFEST as never,
    sideEffect: "non_idempotent",
    ...extra,
  });

describe("who chooses how a step is undone", () => {
  it("is an action that changes data, and a call that waits, inside a transaction", () => {
    expect(row(add())).toEqual({
      kind: "choices",
      value: null,
      holdDefault: 900,
      problems: ["This step changes data. Choose how it's undone."],
    });
    expect(row(reverse(), { sideEffect: "" })).toMatchObject({
      kind: "choices",
      value: null,
      problems: ["This step changes data. Choose how it's undone."],
    });
    // Any action that isn't read-only, as the platform reads it.
    expect(row(add(), { sideEffect: "idempotent" })).toMatchObject({
      problems: ["This step changes data. Choose how it's undone."],
    });
  });

  it("says nothing is missing while Studio can't tell what an action does", () => {
    expect(row(add(), { sideEffect: "" })).toEqual({
      kind: "choices",
      value: null,
      holdDefault: 900,
      problems: [],
    });
  });

  it("never asks a step that reads, unless the document declares one", () => {
    expect(row(add(), { sideEffect: "read_only" })).toEqual({
      kind: "read-only",
      reason: "Doesn't change anything, nothing to undo",
    });
    expect(
      row(add({ irreversible: true }), { sideEffect: "read_only" }),
    ).toMatchObject({ kind: "choices", value: "irreversible", problems: [] });
  });

  it("leaves out a call that doesn't wait, other kinds, and targets without transactions", () => {
    expect(row(reverse({ mode: "detach" }))).toBeNull();
    expect(
      row({ id: "total", kind: "transform", value: {} } as unknown as Step),
    ).toBeNull();
    expect(row(add(), { features: ["flow.onError"] })).toBeNull();
    expect(row(add(), { manifest: { step_fields: [] } as never })).toBeNull();
    expect(row(add(), { manifest: {} as never })).toBeNull();
  });

  it("takes who may choose from the platform's manifest, never a list of its own", () => {
    const manifest = {
      step_fields: [{ ...UNDO, kinds: ["callWorkflow"] }],
    } as never;
    expect(row(add(), { manifest })).toBeNull();
    expect(row(reverse(), { manifest })).toMatchObject({ kind: "choices" });
  });

  it("names the fields a step keeps outside a transaction, and nothing without them", () => {
    const outside = { inTransaction: null };
    expect(row(add(), outside)).toBeNull();
    expect(
      row(add({ confirm: CONFIRM, cancel: RELEASE, holdSeconds: 60 }), outside),
    ).toEqual({
      kind: "outside",
      fields: ["confirm", "cancel", "holdSeconds"],
    });
    expect(row(add({ irreversible: true }), outside)).toEqual({
      kind: "outside",
      fields: ["irreversible"],
    });
    // Outside, a call that doesn't wait keeps its fields' problem too.
    expect(
      row(reverse({ mode: "detach", compensate: REMOVE }), outside),
    ).toEqual({ kind: "outside", fields: ["compensate"] });
    expect(
      row(add({ irreversible: true }), {
        ...outside,
        features: ["flow.onError"],
      }),
    ).toBeNull();
  });

  it("reads the choice from the fields the step has", () => {
    const cases: [Record<string, unknown>, UndoChoice][] = [
      [{}, null],
      [{ confirm: CONFIRM, cancel: RELEASE }, "reserve"],
      [{ cancel: RELEASE }, "reserve"],
      [{ compensate: REMOVE }, "compensate"],
      [{ irreversible: true }, "irreversible"],
      [{ holdSeconds: 60 }, null],
    ];
    for (const [fields, choice] of cases)
      expect(undoChoiceOf(add(fields)), JSON.stringify(fields)).toBe(choice);
  });

  it("holds for as long as the transaction says, 15 minutes when it says nothing", () => {
    expect(
      row(add(), {
        inTransaction: move([], { policy: { holdSeconds: 600 } }),
      }),
    ).toMatchObject({ holdDefault: 600 });
    expect(
      row(add(), { inTransaction: move([], { policy: {} }) }),
    ).toMatchObject({ holdDefault: 900 });
  });
});

/** The step after a write, and the keys the write deletes from it. */
const after = (step: Step, patch: readonly Edit[]) => {
  const next = structuredClone(step) as Record<string, unknown>;
  for (const { path, value } of patch) {
    expect(path).toHaveLength(1);
    if (value === undefined) delete next[String(path[0])];
    else next[String(path[0])] = value;
  }
  return next;
};
const EMPTY = { kind: "action", uses: "", with: { literal: {} } };

describe("what a choice writes", () => {
  it("starts Reserve, then confirm with both operations still to choose", () => {
    const step = add();
    const { patch, removed } = writeUndo(step, "reserve");
    expect(after(step, patch)).toEqual({
      ...step,
      confirm: EMPTY,
      cancel: EMPTY,
    });
    expect(removed).toEqual([]);
  });

  it("starts An action that reverses it with its operation still to choose", () => {
    const step = add();
    const { patch, removed } = writeUndo(step, "compensate");
    expect(after(step, patch)).toEqual({ ...step, compensate: EMPTY });
    expect(removed).toEqual([]);
  });

  it("writes Can't be undone alone", () => {
    const step = add();
    const { patch, removed } = writeUndo(step, "irreversible");
    expect(after(step, patch)).toEqual({ ...step, irreversible: true });
    expect(removed).toEqual([]);
  });

  it("drops the other choices' fields and names the operations that were chosen", () => {
    const reserved = add({
      confirm: CONFIRM,
      cancel: RELEASE,
      holdSeconds: 60,
    });
    const reversed = writeUndo(reserved, "compensate");
    expect(after(reserved, reversed.patch)).toEqual({
      ...add(),
      compensate: EMPTY,
    });
    expect(reversed.removed).toEqual(["Confirm with", "Release with"]);
    expect(removedUndoText(reversed.removed)).toBe(
      "Removed Confirm with and Release with.",
    );

    const final = writeUndo(reserved, "irreversible");
    expect(after(reserved, final.patch)).toEqual(add({ irreversible: true }));
    expect(final.removed).toEqual(["Confirm with", "Release with"]);

    const compensated = add({ compensate: REMOVE });
    const none = writeUndo(compensated, "irreversible");
    expect(after(compensated, none.patch)).toEqual(add({ irreversible: true }));
    expect(none.removed).toEqual(["Reverse with"]);
    expect(removedUndoText(none.removed)).toBe("Removed Reverse with.");

    const held = writeUndo(compensated, "reserve");
    expect(after(compensated, held.patch)).toEqual(
      add({ confirm: EMPTY, cancel: EMPTY }),
    );
    expect(held.removed).toEqual(["Reverse with"]);

    const undone = add({ irreversible: true });
    expect(after(undone, writeUndo(undone, "compensate").patch)).toEqual(
      add({ compensate: EMPTY }),
    );
  });

  it("says nothing of operations that were never chosen", () => {
    const step = add({ confirm: EMPTY, cancel: EMPTY });
    expect(writeUndo(step, "irreversible").removed).toEqual([]);
    expect(
      writeUndo(add({ confirm: CONFIRM, cancel: EMPTY }), "irreversible")
        .removed,
    ).toEqual(["Confirm with"]);
  });

  it("keeps what the same choice already has, and fills half a pair", () => {
    const reserved = add({
      confirm: CONFIRM,
      cancel: RELEASE,
      holdSeconds: 60,
    });
    expect(after(reserved, writeUndo(reserved, "reserve").patch)).toEqual(
      reserved,
    );
    const half = add({ confirm: CONFIRM });
    expect(after(half, writeUndo(half, "reserve").patch)).toEqual(
      add({ confirm: CONFIRM, cancel: EMPTY }),
    );
    const compensated = add({ compensate: REMOVE });
    expect(
      after(compensated, writeUndo(compensated, "compensate").patch),
    ).toEqual(compensated);
  });

  it("writes an operation as the language has it, with an empty input", () => {
    expect(undoOperation("reverse@1.0.0", "callWorkflow")).toEqual({
      kind: "callWorkflow",
      uses: "reverse@1.0.0",
      with: { literal: {} },
    });
  });

  it("clears every field, and only those", () => {
    const step = add({
      confirm: CONFIRM,
      cancel: RELEASE,
      compensate: REMOVE,
      irreversible: true,
      holdSeconds: 60,
      onError: "continue",
    });
    expect(after(step, clearUndo(step))).toEqual(add({ onError: "continue" }));
    expect(clearUndo(add())).toEqual([]);
  });
});

/** Step details on `target`, in a workflow whose action add is a request that changes data. */
function setup(
  definition: Workflow,
  target = "add",
  extra: { method?: string; diagnostics?: Record<string, unknown>[] } = {},
) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(definition), "json");
  model.canvas = withOwnedAction(model.canvas, ADD, {
    ...newHttpRecipe(),
    method: extra.method ?? "POST",
  } as never);
  model.clearHistory();
  const toasts: { text: string; action?: ToastAction }[] = [];
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: (text: string, action?: ToastAction) =>
      void toasts.push({ text, action }),
    undo: () => model.undo(),
    undoIfUnchanged: async (still: () => boolean) => {
      if (!still()) return "changed" as const;
      model.undo();
      return "undone" as const;
    },
    refreshView: () => undefined,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: { request: async () => MANIFEST } as never,
    diagnostics: extra.diagnostics
      ? { diagnostics: extra.diagnostics, errorCount: 1, validationOk: false }
      : null,
    diagnosticsDefinition: extra.diagnostics ? definition : null,
    cacheDecisionContract: () => undefined,
    can: () => true,
  } as unknown as StepDetailsHost;
  let now = 0;
  const controller = new StepDetailsController(host, () => (now += 60_000));
  const focused: string[] = [];
  const session = new FormSession(host, controller, openRequest(target), {
    confirm: async () => true,
    announce: () => undefined,
    focusField: (field) => void focused.push(field.spec.id),
  });
  const field = (id: string) => {
    const found = session.placedSpecs().find((placed) => placed.spec.id === id);
    if (!found) throw new Error(`No field ${id}`);
    return found.spec;
  };
  return {
    model,
    controller,
    session,
    toasts,
    focused,
    field,
    ids: () => controller.settings(target).fields.map((spec) => spec.id),
    read: (id: string) => readParam(controller.subject(target), field(id)),
    write: (id: string, value: ParamValue) =>
      controller.commit(
        target,
        writeParam(controller.subject(target), field(id), value),
        id,
      ),
    step: () => controller.step(target)!,
  };
}

describe("How it's undone in Settings", () => {
  it("sits after Retry on fail and Timeout and before On error", async () => {
    const { controller, ids } = setup(workflow(move([add()])));
    // Until the platform's manifest is read, nothing is offered.
    expect(ids()).not.toContain("undo");
    await controller.loadFeatures();
    expect(ids()).toEqual([
      "retry",
      "timeout",
      "undo",
      "onError",
      "resultOnError",
    ]);
  });

  it("shows the chosen way's own fields under it, in order", async () => {
    const shown = async (fields: Record<string, unknown>) => {
      const { controller, ids } = setup(workflow(move([add(fields)])));
      await controller.loadFeatures();
      return ids().slice(2, -2);
    };
    expect(await shown({ confirm: CONFIRM, cancel: RELEASE })).toEqual([
      "undo",
      "confirm",
      "cancel",
      "undoHold",
    ]);
    expect(await shown({ compensate: REMOVE })).toEqual(["undo", "compensate"]);
    expect(await shown({ irreversible: true })).toEqual(["undo"]);
  });

  it("is found inside loops and paths of a transaction, not in its error path", async () => {
    const loop = {
      id: "each",
      kind: "forEach",
      items: { literal: [1] },
      maxItems: 3,
      body: { steps: [add()], output: { literal: null } },
    } as unknown as Step;
    const inside = setup(workflow(move([loop])));
    await inside.controller.loadFeatures();
    expect(inside.ids()).toContain("undo");
    const after = setup(
      workflow(
        move([], {
          onError: "errorOutput",
          errorPath: { steps: [add()], output: { literal: null } },
        }),
      ),
    );
    await after.controller.loadFeatures();
    expect(after.ids()).not.toContain("undo");
  });

  it("writes nothing and chooses nothing when it opens", async () => {
    const { model, controller, session, ids, read } = setup(
      workflow(move([add()])),
    );
    await controller.loadFeatures();
    const before = model.source;
    expect(ids()).toContain("undo");
    session.state("settings");
    session.placedSpecs();
    session.issues();
    expect(read("undo")).toEqual({ mode: "absent" });
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("says an undeclared step changes data, once, with the compiler's finding or without", async () => {
    const message = "This step changes data. Choose how it's undone.";
    const alone = setup(workflow(move([add()])));
    await alone.controller.loadFeatures();
    const found = alone.session.issues();
    expect(
      found.map((issue) => [issue.message, issue.field?.spec.id]),
    ).toContainEqual([message, "undo"]);
    const checked = setup(workflow(move([add()])), "add", {
      diagnostics: [
        {
          code: "WV-COMP-TXN_UNDECLARED",
          severity: "error",
          message: "add changes data. Choose how it's undone: …",
          path: "/spec/steps/0/steps/0",
          suggestedEdit: {
            path: "/spec/steps/0/steps/0/irreversible",
            value: true,
          },
        },
      ],
    });
    await checked.controller.loadFeatures();
    const issues = checked.session
      .issues()
      .filter((issue) => issue.message === message);
    expect(issues).toHaveLength(1);
    expect(issues[0].field?.spec.id).toBe("undo");
    expect(issues[0].fix).toEqual({
      kind: "undo",
      label: "Choose how it's undone",
    });
    checked.session.runFix(issues[0]);
    expect(checked.focused).toEqual(["undo"]);
    expect(checked.step()).toEqual(add());
  });

  it("never offers the compiler's own answer for an undeclared step", () => {
    const suggestion = {
      stepId: "add",
      value: add({ irreversible: true }),
    };
    expect(fixFor("WV-COMP-TXN_UNDECLARED", suggestion, null)).toEqual({
      kind: "undo",
      label: "Choose how it's undone",
    });
    // Other suggestions stay one press away.
    expect(fixFor("WV-COMP-TXN_SIZE", suggestion, null)).toMatchObject({
      kind: "suggestion",
      label: "Apply suggested edit",
    });
  });

  it("clears a step's leftover fields outside a transaction, in one undo step", async () => {
    const step = add({ confirm: CONFIRM, cancel: RELEASE, holdSeconds: 60 });
    const at = (field: string) => ({
      code: "WV-COMP-TXN_OUTSIDE",
      severity: "error",
      message: "confirm, cancel, … apply only to steps inside a transaction.",
      path: `/spec/steps/0/${field}`,
    });
    const {
      controller,
      session,
      model,
      step: now,
    } = setup(workflow(step), "add", {
      diagnostics: ["confirm", "cancel", "holdSeconds"].map(at),
    });
    await controller.loadFeatures();
    const issues = session
      .issues()
      .filter((issue) => /inside a transaction/.test(issue.message));
    // Three fields, one thing to do.
    expect(issues).toHaveLength(1);
    expect(issues[0].field?.spec.id).toBe("undo");
    expect(issues[0].fix).toEqual({
      kind: "clearUndo",
      label: "Clear How it's undone",
    });
    const before = model.source;
    session.runFix(issues[0]);
    expect(now()).toEqual(add());
    model.undo();
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("makes a call that doesn't wait wait for its result", async () => {
    const { controller, session, step } = setup(
      workflow(move([reverse({ mode: "detach" })])),
      "reverse",
      {
        diagnostics: [
          {
            code: "WV-COMP-TXN_DETACH",
            severity: "error",
            message: "A call inside a transaction must wait for its result; …",
            path: "/spec/steps/0/steps/0/mode",
          },
        ],
      },
    );
    await controller.loadFeatures();
    const issue = session
      .issues()
      .find((found) => /must be waited for/.test(found.message))!;
    expect(issue.field?.spec.id).toBe("mode");
    expect(issue.fix).toEqual({
      kind: "waitForCall",
      label: "Wait for its result",
    });
    session.runFix(issue);
    expect(step()["mode"]).toBe("wait");
  });
});

describe("a way of undoing a step, chosen in step details", () => {
  it("is one undo step, and a toast names what it drops, with Undo", async () => {
    const step = add({ confirm: CONFIRM, cancel: RELEASE, holdSeconds: 60 });
    const {
      controller,
      session,
      model,
      toasts,
      step: now,
    } = setup(workflow(move([step])));
    await controller.loadFeatures();
    const before = model.source;
    session.chooseUndo("compensate");
    expect(now()).toEqual(add({ compensate: EMPTY }));
    expect(toasts.map(({ text }) => text)).toEqual([
      "Removed Confirm with and Release with.",
    ]);
    expect(toasts[0].action?.label).toBe("Undo");
    toasts[0].action!.run();
    await Promise.resolve();
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("toasts nothing while nothing chosen is dropped", async () => {
    const { controller, session, toasts, step } = setup(
      workflow(move([add()])),
    );
    await controller.loadFeatures();
    session.chooseUndo("reserve");
    session.chooseUndo("irreversible");
    expect(step()).toEqual(add({ irreversible: true }));
    expect(toasts).toEqual([]);
  });

  it("keeps Hold for only while it differs from the transaction's", async () => {
    const reserved = add({ confirm: CONFIRM, cancel: RELEASE });
    const { controller, model, field, write, step, read } = setup(
      workflow(move([reserved], { policy: { holdSeconds: 600 } })),
    );
    await controller.loadFeatures();
    expect(field("undoHold")).toMatchObject({
      path: ["holdSeconds"],
      type: "duration",
      label: "Hold for",
      min: 30,
      max: 86400,
      default: 600,
      hint: "Same as the transaction's Hold reservations for.",
    });
    write("undoHold", fixed(1200));
    expect(step()["holdSeconds"]).toBe(1200);
    expect(field("undoHold").hint).toBeUndefined();
    write("undoHold", fixed(600));
    expect(step()).not.toHaveProperty("holdSeconds");
    expect(read("undoHold")).toEqual({ mode: "absent" });
    // The transaction's own hold in a document stays as it is until Hold for changes.
    const before = model.source;
    write("undoHold", fixed(600));
    expect(model.source).toBe(before);
  });

  it("keeps a document's own Hold for through other edits", async () => {
    const reserved = add({
      confirm: CONFIRM,
      cancel: RELEASE,
      holdSeconds: 900,
    });
    const { controller, step } = setup(workflow(move([reserved])));
    await controller.loadFeatures();
    controller.commit(
      "add",
      [{ scope: "step", path: ["onError"], value: "continue" }],
      "onError",
    );
    expect(step()["holdSeconds"]).toBe(900);
  });

  it("asks for every operation of the choice, in the compiler's words for a pair", async () => {
    const pair = setup(
      workflow(move([add({ confirm: CONFIRM, cancel: EMPTY })])),
    );
    await pair.controller.loadFeatures();
    expect(
      pair.session
        .issues()
        .filter((issue) => issue.field?.spec.id === "cancel")
        .map((issue) => issue.message),
    ).toEqual([
      "A step that reserves needs both Confirm with and Release with.",
    ]);
    expect(pair.field("confirm")).toMatchObject({
      path: ["confirm", "uses"],
      type: "resource",
      label: "Confirm with",
      required: true,
    });
    expect(pair.field("cancel")).toMatchObject({
      path: ["cancel", "uses"],
      label: "Release with",
    });
    const one = setup(workflow(move([add({ compensate: EMPTY })])));
    await one.controller.loadFeatures();
    expect(one.field("compensate")).toMatchObject({
      path: ["compensate", "uses"],
      type: "resource",
      label: "Reverse with",
      required: true,
    });
    expect(
      one.session
        .issues()
        .filter((issue) => issue.field?.spec.id === "compensate"),
    ).toHaveLength(1);
  });

  it("marks Settings changed only once a way is chosen", async () => {
    const changed = async (fields: Record<string, unknown>) => {
      const { controller } = setup(workflow(move([add(fields)])));
      await controller.loadFeatures();
      return controller
        .settings("add")
        .fields.filter(
          (spec) =>
            spec.scope !== "action" &&
            readParam(controller.subject("add"), spec).mode !== "absent",
        )
        .map((spec) => spec.id);
    };
    expect(await changed({})).toEqual([]);
    expect(await changed({ irreversible: true })).toEqual(["undo"]);
    expect(await changed({ compensate: REMOVE })).toEqual(["compensate"]);
  });
});

describe("a step's undo in the rest of the editor", () => {
  const reads = (id: string): Json => ({
    object: { target: { ref: `/steps/${id}/output` } },
  });
  const undone = () =>
    workflow(
      move([
        add({ id: "hold", compensate: REMOVE }),
        add({
          confirm: { ...CONFIRM, with: reads("hold") },
          cancel: { ...RELEASE, with: reads("hold") },
        }),
        reverse({ compensate: { ...REMOVE, with: reads("hold") } }),
      ]),
    );

  it("follows a step it reads when that step is renamed", () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(undone()), "json");
    expect(model.renameStep("hold", "reservation")).toBe(3);
    const [, added, called] = model.definition.spec.steps[0]["steps"] as Record<
      string,
      { with: unknown }
    >[];
    expect(added["confirm"].with).toEqual(reads("reservation"));
    expect(added["cancel"].with).toEqual(reads("reservation"));
    expect(called["compensate"].with).toEqual(reads("reservation"));
  });

  it("keeps a step it reads from being deleted, naming who reads it", () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(undone()), "json");
    expect(() => model.remove("hold")).toThrow(/referenced by add and reverse/);
  });

  it("copies and pastes with its steps, its connection slot renamed with them", () => {
    const source = workflow(
      move([
        add({
          connection: "target",
          confirm: { ...CONFIRM, connection: "target" },
          cancel: { ...RELEASE, connection: "undo-only" },
        }),
      ]),
    );
    source.spec["connections"] = {
      target: { connector: "weave-http@2.0.0" },
      "undo-only": { connector: "weave-http@2.0.0" },
    };
    const copied = copySteps(source, ["move"]);
    expect(Object.keys(copied.document.connections)).toEqual([
      "target",
      "undo-only",
    ]);
    const read = readClipboard(copied.yaml);
    if (!read.ok) throw new Error(read.reason);
    const into = freshWorkflow();
    into.spec["connections"] = { target: { connector: "weave-email@1.0.0" } };
    const plan = planPaste(
      into,
      read.content,
      { owner: "root", index: 0 },
      new Set(),
    );
    if ("reason" in plan) throw new Error(plan.reason);
    const [pasted] = plan.steps[0]["steps"] as Record<
      string,
      { connection?: string }
    >[];
    expect(pasted["connection"]).toBe("target-2");
    expect(pasted["confirm"].connection).toBe("target-2");
    expect(pasted["cancel"].connection).toBe("undo-only");
    expect(Object.keys(plan.addConnections)).toEqual(["target-2", "undo-only"]);
  });

  it("pastes every way of undoing a call as it is", () => {
    for (const fields of [
      { confirm: CONFIRM, cancel: RELEASE, holdSeconds: 60 },
      { compensate: REMOVE },
      { irreversible: true },
    ]) {
      const read = readClipboard(
        JSON.stringify(reverse(fields)),
        new Set(["callWorkflow"]),
      );
      expect(read.ok && read.content.steps).toEqual([reverse(fields)]);
      const action = readClipboard(JSON.stringify(add(fields)));
      expect(action.ok && action.content.steps).toEqual([add(fields)]);
    }
    expect(
      readClipboard(JSON.stringify({ ...move([]), irreversible: true })).ok,
    ).toBe(false);
  });
});
