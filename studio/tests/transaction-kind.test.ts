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
// The Transaction step: where Add a step offers it and why it may not be
// added there, what it starts as, how its own steps are named, what an empty
// one still needs, and that it survives renames and the clipboard.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { buildCatalog } from "../src/app/editor/add-step/entries";
import {
  insertEntry,
  placeMark,
  type InsertHost,
} from "../src/app/editor/add-step/insert";
import { shown, startState } from "../src/app/editor/add-step/panel-model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { breadcrumb } from "../src/app/editor/ndv/navigation";
import { ndvRegistry } from "../src/app/editor/ndv/registry";
import { readClipboard } from "../src/app/editor/state/clipboard";
import { blocksOf } from "../src/app/editor/step-blocks";
import {
  describeDiagnostic,
  withEmptyTransactions,
} from "../src/app/designer/diagnostic-copy";
import {
  StructuredCanvasAdapter,
  branchTitle,
  freshWorkflow,
  ownerLabel,
  type Step,
  type Workflow,
} from "../src/app/model";
import { catalogInput, entryById, viewLabels } from "./add-step-fixtures";

beforeAll(() => loadKindRegistrations());

const step = (value: Record<string, unknown>) => value as unknown as Step;
const lookup = (id: string) =>
  step({ id, kind: "transform", value: { literal: {} } });
/** A transaction as Add a step makes it. */
const newTransaction = (id: string) =>
  step({ id, kind: "transaction", steps: [], output: { literal: null } });
/**
 * move holds hold, add (with an error path) and each (a loop) in a
 * transaction with an error path of its own; notify comes after it.
 */
function nested(): Workflow {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    step({
      id: "move",
      kind: "transaction",
      businessKey: { ref: "/steps/lookup/output/reference" },
      steps: [
        lookup("hold"),
        step({
          ...lookup("add"),
          onError: "errorOutput",
          errorPath: { steps: [lookup("flag")], output: { literal: null } },
        }),
        step({
          id: "each",
          kind: "forEach",
          items: { literal: [] },
          body: { steps: [lookup("send")], output: { ref: "/item" } },
        }),
      ],
      output: { ref: "/steps/add/output" },
      onError: "errorOutput",
      errorPath: { steps: [lookup("tell")], output: { literal: null } },
    }),
    lookup("notify"),
  ];
  return workflow;
}
const runs = {
  language: { features: ["flow.transaction"], pending: new Set<string>() },
};

describe("the Transaction step", () => {
  it("loads with the editor's kinds, gated by the platform's transactions", () => {
    expect(ndvRegistry.kind("transaction")).toMatchObject({
      kind: "transaction",
      feature: "flow.transaction",
      label: "Transaction",
      description: "Run steps together; if one fails, undo what's done",
      icon: "transaction",
      role: "group",
      category: "flow",
      idPrefix: "transaction",
      pinnable: false,
    });
  });

  it("starts empty, with no result and no undo chosen for anyone", () => {
    expect(ndvRegistry.kind("transaction")!.create("transaction-1")).toEqual({
      id: "transaction-1",
      kind: "transaction",
      steps: [],
      output: { literal: null },
    });
  });

  it("holds its own steps in one frame, read by its result and its business key", () => {
    const kind = ndvRegistry.kind("transaction")!;
    const empty = newTransaction("transaction-1");
    expect(
      kind.containers!(empty).map(({ path, layout, label }) => ({
        path,
        layout,
        label: label(empty, {} as never),
      })),
    ).toEqual([{ path: ["steps"], layout: "frame", label: "Transaction" }]);
    expect(kind.fields(empty).map((field) => field.path)).toEqual([
      ["output"],
      ["businessKey"],
    ]);
  });

  it("counts its steps on its tile", () => {
    const summary = (steps: Step[]) =>
      ndvRegistry
        .kind("transaction")!
        .summary(step({ ...newTransaction("move"), steps }), {} as never);
    expect(summary([])).toBe("");
    expect(summary([lookup("hold")])).toBe("1 step");
    expect(summary([lookup("hold"), lookup("add")])).toBe("2 steps");
  });

  it("names its own steps Transaction move, and its error path On error of move", () => {
    const move = nested().spec.steps[0];
    const steps = new Map([["move", move]]);
    expect(
      blocksOf(move).map((block) => branchTitle(move, block.name)),
    ).toEqual(["Transaction", "On error"]);
    expect(ownerLabel("move/steps", steps)).toBe("Transaction move");
    expect(ownerLabel("move/steps")).toBe("Transaction move");
    expect(ownerLabel("move/errorPath", steps)).toBe("On error of move");
    // Where a step runs, in step details: its transaction, its error path.
    expect(breadcrumb(nested(), "hold")).toBe("Transaction move · step 1 of 3");
    expect(breadcrumb(nested(), "tell")).toBe(
      "Error path of move · step 1 of 1",
    );
    expect(breadcrumb(nested(), "notify")).toBe("Main sequence · step 2 of 2");
    // A parallel branch may be called steps too.
    const fan = step({
      id: "fan",
      kind: "parallel",
      branches: { steps: { steps: [], output: {} } },
    });
    expect(ownerLabel("fan/steps", new Map([["fan", fan]]))).toBe(
      "steps of fan",
    );
  });
});

describe("Transaction in Add a step", () => {
  const entry = (extra: Parameters<typeof catalogInput>[0] = {}) =>
    entryById(
      buildCatalog(catalogInput({ ...runs, ...extra })),
      "kind:transaction",
    );

  it("comes after Loop over items in Flow", () => {
    expect(
      viewLabels(buildCatalog(catalogInput(runs)), "category:flow"),
    ).toEqual([
      "Decision",
      "Parallel",
      "Loop over items",
      "Transaction",
      "Call a workflow",
      "Stop with error",
    ]);
    expect(entry()).toMatchObject({
      label: "Transaction",
      description: "Run steps together; if one fails, undo what's done",
      icon: "transaction",
      disabled: null,
    });
  });

  it("stays listed with its reason where the platform has no transactions", () => {
    expect(
      entry({ language: { features: [], pending: new Set() } }).disabled,
    ).toBe("Needs a platform with transactions (flow.transaction)");
    // The platform comes first: it is what must change.
    expect(
      entry({
        language: { features: [], pending: new Set() },
        insideTransaction: true,
      }).disabled,
    ).toBe("Needs a platform with transactions (flow.transaction)");
  });

  it("can't be added inside another transaction", () => {
    const inside = buildCatalog(
      catalogInput({ ...runs, insideTransaction: true }),
    );
    expect(entryById(inside, "kind:transaction").disabled).toBe(
      "A transaction can't contain another transaction.",
    );
    expect(entryById(inside, "kind:switch").disabled).toBeNull();
  });

  it("is found by what people call it", () => {
    const catalog = buildCatalog(catalogInput(runs));
    for (const query of ["rollback", "undo", "all or nothing", "reserve"])
      expect(
        shown({ ...startState("next"), query }, catalog, false).options.map(
          (found) => found.id,
        ),
        query,
      ).toContain("kind:transaction");
  });

  it("inserts an empty transaction as one undo step and opens it", async () => {
    const model = new StructuredCanvasAdapter();
    model.insert("transform");
    const opened: string[] = [];
    const host = {
      model,
      editingLocked: false,
      ensureApplied: async () => true,
      edit: (change: () => void) => (change(), ""),
      notify: () => {},
      openInserted: (id: string) => void opened.push(id),
    } as unknown as InsertHost;
    const at = { owner: "root", index: 1 };
    expect(
      await insertEntry(host, entry(), {
        at,
        opened: model.opened,
        mark: placeMark(model, at),
      }),
    ).toBe("done");
    expect(model.definition.spec.steps[1]).toEqual(
      newTransaction("transaction-1"),
    );
    expect(opened).toEqual(["transaction-1"]);
    model.undo();
    expect(model.definition.spec.steps.map((item) => item.id)).toEqual([
      "transform-1",
    ]);
  });
});

describe("a transaction in the workflow model", () => {
  const opened = () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(nested()), "json");
    return model;
  };

  it("opens for editing, its steps under it", () => {
    const model = opened();
    expect(model.error).toBe("");
    expect(model.readonly).toBe(false);
    expect(
      model.nodes().map((node) => `${node.owner} ${node.step.id}`),
    ).toEqual(
      expect.arrayContaining([
        "root move",
        "move/steps hold",
        "move/steps add",
        "root notify",
      ]),
    );
  });

  it("keeps its result and its business key on the steps they read", () => {
    const model = opened();
    expect(
      model.referenceSites().filter((site) => site.holder === "move"),
    ).toEqual([
      {
        path: ["spec", "steps", 0, "businessKey", "ref"],
        ref: "/steps/lookup/output/reference",
        holder: "move",
      },
      {
        path: ["spec", "steps", 0, "output", "ref"],
        ref: "/steps/add/output",
        holder: "move",
      },
    ]);
    model.renameStep("add", "add-quantity");
    expect(model.definition.spec.steps[0]["output"]).toEqual({
      ref: "/steps/add-quantity/output",
    });
  });

  it("is read from the clipboard with every field it has, and refused with one it hasn't", () => {
    const move = {
      ...nested().spec.steps[0],
      policy: { holdSeconds: 600 },
    };
    const paste = (value: unknown) =>
      readClipboard(JSON.stringify({ weaveClipboard: 1, steps: [value] }));
    expect(paste(move)).toMatchObject({ ok: true });
    expect(paste({ ...move, irreversible: true })).toEqual({
      ok: false,
      reason:
        "The pasted step move has a field Studio doesn't know: irreversible.",
    });
    expect(paste({ ...move, steps: undefined })).toEqual({
      ok: false,
      reason: "The pasted step move has a path without a list of steps.",
    });
  });
});

describe("a transaction without steps", () => {
  const generic = (path: string) => ({
    code: "WV-SCHEMA-INVALID_INSTANCE",
    severity: "error",
    path,
    message: "Value does not satisfy the declared contract.",
  });
  const checked = (
    steps: Step[],
    diagnostics: { path?: string; severity?: string }[],
  ) => {
    const workflow = freshWorkflow();
    workflow.spec.steps = steps;
    return withEmptyTransactions(
      {
        validationOk: !diagnostics.length,
        errorCount: diagnostics.filter((item) => item.severity === "error")
          .length,
        diagnostics,
      },
      workflow,
    );
  };
  const problem = {
    code: "WV-STUDIO-TRANSACTION",
    severity: "error",
    message: "Add a step to the transaction.",
  };

  it("says what to do in place of the platform's general answer", () => {
    const other = {
      code: "WV-COMP-UNUSED",
      severity: "warning",
      path: "/spec/steps/1",
    };
    const result = checked(
      [newTransaction("transaction-1"), lookup("notify")],
      [generic("/spec/steps/0"), other],
    );
    expect(result).toEqual({
      validationOk: false,
      errorCount: 1,
      diagnostics: [{ ...problem, path: "/spec/steps/0" }, other],
    });
    expect(describeDiagnostic(result.diagnostics[0]).text).toBe(
      "Add a step to the transaction.",
    );
  });

  it("is named inside a loop too, where the platform reports only the loop", () => {
    const result = checked(
      [
        step({
          id: "each",
          kind: "forEach",
          body: { steps: [newTransaction("move")], output: {} },
        }),
      ],
      [generic("/spec/steps/0")],
    );
    expect(result.diagnostics).toEqual([
      { ...problem, path: "/spec/steps/0/body/steps/0" },
    ]);
    expect(result.errorCount).toBe(1);
  });

  it("leaves every other answer as it was", () => {
    // Step 1 isn't step 10, though its pointer starts the same.
    const other = [generic("/spec/steps/1")];
    const result = checked(
      [
        ...Array.from({ length: 10 }, (_, index) => lookup(`step-${index}`)),
        newTransaction("transaction-1"),
      ],
      other,
    );
    expect(result.diagnostics).toEqual([
      { ...problem, path: "/spec/steps/10" },
      other[0],
    ]);
    expect(result.errorCount).toBe(2);
    const full = {
      validationOk: false,
      errorCount: 1,
      diagnostics: [generic("/spec/steps/0")],
    };
    expect(withEmptyTransactions(full, nested())).toBe(full);
    expect(withEmptyTransactions(full, null)).toBe(full);
  });
});
