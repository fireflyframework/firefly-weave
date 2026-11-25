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
// A transaction's Parameters and Settings: its result, its business key, how
// long it holds reservations and retries its undo steps, and what a failure
// does. Its policy is written whole once one value differs from the
// platform's default, and left out while every value is its default.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { languageTarget } from "../src/app/editor/add-step/catalog-sources";
import { buildCatalog } from "../src/app/editor/add-step/entries";
import { formProblems } from "../src/app/editor/ndv/form-contract";
import { headerSubtitle } from "../src/app/editor/ndv/header";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { fieldRules } from "../src/app/editor/ndv/params/field-rules";
import { paramIsDefault } from "../src/app/editor/ndv/params/form-model";
import {
  ABSENT,
  fixed,
  readParam,
  resetChanges,
  writeParam,
  type ParamValue,
} from "../src/app/editor/ndv/params/value-io";
import {
  ndvRegistry,
  type KindContext,
  type ParamSpec,
} from "../src/app/editor/ndv/registry";
import {
  onErrorFields,
  onErrorRow,
  type OnErrorContext,
} from "../src/app/editor/ndv/settings/on-error";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
} from "../src/app/model";
import { catalogInput, entryById } from "./add-step-fixtures";

beforeAll(() => loadKindRegistrations());

const MANIFEST = {
  features: ["flow.transaction", "flow.onError"],
  step_fields: [{ name: "onError", kinds: ["transform", "transaction"] }],
};
/** Step details on move, a transaction that holds hold, with its fields as the document has them. */
function setup(extra: Record<string, unknown> = {}) {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    {
      id: "move",
      kind: "transaction",
      steps: [{ id: "hold", kind: "transform", value: { literal: {} } }],
      output: { literal: null },
      ...extra,
    } as unknown as Step,
  ];
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow), "json");
  model.clearHistory();
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: () => undefined,
    undo: () => model.undo(),
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    label: (kind: string) => kind,
    editingLocked: false,
    profile: null,
    api: { request: async () => MANIFEST } as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => true,
  } as unknown as ControllerHost;
  // Each write is a burst of its own.
  let now = 0;
  const controller = new StepDetailsController(host, () => (now += 60_000));
  const field = (id: string): ParamSpec => {
    const all = [
      ...controller.parameters("move").fields,
      ...controller
        .settings("move")
        .fields.flatMap((spec) => [
          spec,
          ...(spec.children?.(
            controller.step("move")!,
            controller.kindContext(),
          ) ?? []),
        ]),
    ];
    const found = all.find((spec) => spec.id === id);
    if (!found) throw new Error(`No field ${id}`);
    return found;
  };
  return {
    model,
    controller,
    field,
    read: (id: string) => readParam(controller.subject("move"), field(id)),
    write: (id: string, value: ParamValue) =>
      controller.commit(
        "move",
        writeParam(controller.subject("move"), field(id), value),
        id,
      ),
    reset: (id: string) =>
      controller.commit(
        "move",
        resetChanges(controller.subject("move"), field(id)),
        id,
      ),
    move: () => model.definition.spec.steps[0],
  };
}
const DEFAULTS = {
  holdSeconds: 900,
  undoRetry: { maxAttempts: 10, initialDelaySeconds: 2, maxDelaySeconds: 300 },
  undoTimeoutSeconds: 86400,
};

describe("a transaction's fields", () => {
  it("shows its result in Parameters, empty until a step gives one", () => {
    const { controller, read, write, reset, move } = setup();
    const [result, ...others] = controller.parameters("move").fields;
    expect(others).toEqual([]);
    expect(result).toMatchObject({
      id: "output",
      path: ["output"],
      label: "Result",
      mapping: "both",
      default: null,
      hint: "What later steps read as this transaction's output.",
    });
    expect(read("output")).toEqual(fixed(null));
    // Nothing to fix: null is what a transaction returns until it is mapped.
    expect(fieldRules(result, read("output"), { kind: "absent" })).toEqual([]);
    write("output", {
      mode: "mapped",
      expression: { ref: "/steps/hold/output" },
    });
    expect(move()["output"]).toEqual({ ref: "/steps/hold/output" });
    reset("output");
    expect(move()["output"]).toEqual({ literal: null });
  });

  it("shows its four settings in order, then On error, with the platform's defaults", async () => {
    const { controller, field } = setup();
    await controller.loadFeatures();
    expect(controller.settings("move").fields.map((spec) => spec.id)).toEqual([
      "businessKey",
      "holdSeconds",
      "undoRetry",
      "undoTimeout",
      "onError",
      "resultOnError",
    ]);
    expect(field("businessKey")).toMatchObject({
      path: ["businessKey"],
      label: "Business key",
      mapping: "both",
      hint: "Shown in Operations and in platform events. Use a reference, never personal data.",
    });
    expect(field("businessKey").required).toBeUndefined();
    expect(field("holdSeconds")).toMatchObject({
      path: ["policy", "holdSeconds"],
      type: "duration",
      label: "Hold reservations for",
      default: 900,
      min: 30,
      max: 86400,
      hint: "From 30 seconds to 1 day. Steps that reserve hold for this long unless they set their own Hold for.",
    });
    expect(field("undoRetry")).toMatchObject({
      path: ["policy", "undoRetry"],
      label: "Undo retries",
      hint: "Weave retries each undo step until it succeeds, waiting longer each time.",
    });
    expect(
      [
        "undoRetry.maxAttempts",
        "undoRetry.initialDelaySeconds",
        "undoRetry.maxDelaySeconds",
      ].map((id) => {
        const spec = field(id);
        return [spec.label, spec.path, spec.type, spec.default, spec.min];
      }),
    ).toEqual([
      ["Max tries", ["policy", "undoRetry", "maxAttempts"], "number", 10, 1],
      [
        "Wait between tries",
        ["policy", "undoRetry", "initialDelaySeconds"],
        "duration",
        2,
        1,
      ],
      [
        "Longest wait",
        ["policy", "undoRetry", "maxDelaySeconds"],
        "duration",
        300,
        2,
      ],
    ]);
    expect(field("undoTimeout")).toMatchObject({
      path: ["policy", "undoTimeoutSeconds"],
      type: "duration",
      label: "Give up undoing after",
      default: 86400,
      min: 60,
      max: 604800,
      hint: "If an undo, a release or a confirm still hasn't finished after this long, Weave stops trying and the transaction is in doubt.",
    });
  });

  it("keeps the rules every kind's form follows", () => {
    const { controller, move } = setup();
    expect(
      formProblems(
        ndvRegistry.kind("transaction")!,
        move(),
        controller.kindContext(),
      ),
    ).toEqual([]);
  });

  it("says out of range in the units it offers", () => {
    const { field, write } = setup();
    const said = (id: string, seconds: number) =>
      fieldRules(field(id), fixed(seconds), { kind: "absent" }).map(
        (problem) => problem.message,
      );
    expect(said("holdSeconds", 29)).toEqual(["Use 30 seconds to 1 day."]);
    expect(said("holdSeconds", 30)).toEqual([]);
    expect(said("holdSeconds", 86400)).toEqual([]);
    expect(said("holdSeconds", 86401)).toEqual(["Use 30 seconds to 1 day."]);
    expect(said("undoTimeout", 8 * 86400)).toEqual(["Use 1 minute to 7 days."]);
    expect(said("undoTimeout", 59)).toEqual(["Use 1 minute to 7 days."]);
    expect(said("undoRetry.maxAttempts", 0)).toEqual(["Use 1 or more."]);
    // The longest wait is at least the wait between tries.
    write("undoRetry.initialDelaySeconds", fixed(600));
    expect(said("undoRetry.maxDelaySeconds", 300)).toEqual([
      "Use 10 minutes or more.",
    ]);
    expect(said("undoRetry.maxDelaySeconds", 600)).toEqual([]);
  });

  it("maps its business key like any expression", () => {
    const { write, read, move } = setup();
    write("businessKey", {
      mode: "mapped",
      expression: { ref: "/input/moveId" },
    });
    expect(move()["businessKey"]).toEqual({ ref: "/input/moveId" });
    expect(read("businessKey")).toEqual({
      mode: "mapped",
      expression: { ref: "/input/moveId" },
    });
    write("businessKey", ABSENT);
    expect(move()).not.toHaveProperty("businessKey");
  });

  it("counts its steps in step details", () => {
    const { controller, move } = setup();
    expect(
      headerSubtitle(move(), controller.kindContext(), "Transaction"),
    ).toBe("Transaction · 1 step");
    expect(
      headerSubtitle(
        { ...move(), steps: [] },
        controller.kindContext(),
        "Transaction",
      ),
    ).toBe("Transaction");
  });
});

describe("a transaction's policy", () => {
  it("is written whole once one value differs, as one undo step", () => {
    const { model, write, move } = setup();
    const before = model.source;
    write("holdSeconds", fixed(600));
    expect(move()["policy"]).toEqual({ ...DEFAULTS, holdSeconds: 600 });
    expect(Object.keys(move()["policy"] as object)).toEqual([
      "holdSeconds",
      "undoRetry",
      "undoTimeoutSeconds",
    ]);
    model.undo();
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("goes once every value is its default again", () => {
    const { model, write, move } = setup();
    const before = structuredClone(model.definition);
    write("holdSeconds", fixed(600));
    write("undoRetry.maxAttempts", fixed(3));
    write("holdSeconds", fixed(900));
    expect(move()["policy"]).toEqual({
      ...DEFAULTS,
      undoRetry: { ...DEFAULTS.undoRetry, maxAttempts: 3 },
    });
    write("undoRetry.maxAttempts", fixed(10));
    expect(model.definition).toEqual(before);
  });

  it("puts one value back to its default and keeps the others", () => {
    const { reset, write, move } = setup();
    write("undoTimeout", fixed(3600));
    write("undoRetry.maxDelaySeconds", fixed(60));
    reset("undoTimeout");
    expect(move()["policy"]).toEqual({
      ...DEFAULTS,
      undoRetry: { ...DEFAULTS.undoRetry, maxDelaySeconds: 60 },
    });
    reset("undoRetry.maxDelaySeconds");
    expect(move()).not.toHaveProperty("policy");
  });

  it("writes nothing for a value that is already so", () => {
    const { model, write } = setup();
    const before = model.source;
    write("holdSeconds", fixed(900));
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("stays as the document has it until one of its values changes", () => {
    const { model, write, controller, move } = setup({
      policy: { holdSeconds: 900 },
    });
    write("businessKey", fixed("move-1"));
    expect(move()["policy"]).toEqual({ holdSeconds: 900 });
    controller.commit(
      "move",
      [{ scope: "step", path: ["onError"], value: "continue" }],
      "onError",
    );
    expect(move()["policy"]).toEqual({ holdSeconds: 900 });
    model.undo();
    model.undo();
    write("undoTimeout", fixed(7200));
    expect(move()["policy"]).toEqual({ ...DEFAULTS, undoTimeoutSeconds: 7200 });
  });

  it("marks Settings changed only for a value that differs", () => {
    const { controller, write } = setup();
    const changed = () => {
      const env = controller.env("move");
      return controller
        .settings("move")
        .fields.filter((spec) => !paramIsDefault(spec, env))
        .map((spec) => spec.id);
    };
    expect(changed()).toEqual([]);
    write("holdSeconds", fixed(600));
    expect(changed()).toEqual(["holdSeconds"]);
  });
});

describe("On error of a transaction", () => {
  const context = (): OnErrorContext =>
    ({
      workflow: freshWorkflow(),
      features: ["flow.onError"],
      actionContract: () => null,
      tableContract: () => null,
      workflowContract: () => null,
      onErrorKinds: ["transform", "transaction"],
    }) as KindContext & { onErrorKinds: string[] };
  const transaction = {
    id: "move",
    kind: "transaction",
    steps: [],
    output: { literal: null },
  } as unknown as Step;

  it("says what each choice does once the transaction is undone", () => {
    const [onError] = onErrorFields(transaction, context());
    expect(
      (onError.choices as { label: string; description: string }[]).map(
        (choice) => [choice.label, choice.description],
      ),
    ).toEqual([
      [
        "Stop the run",
        "If the transaction is undone, the run fails. Nothing stays applied.",
      ],
      [
        "Continue",
        "If the transaction is undone, the run goes on; later steps read its error.",
      ],
      [
        "Continue with error output",
        "If the transaction is undone, the run takes the error path.",
      ],
    ]);
  });

  it("says that it never handles a transaction in doubt", () => {
    expect(onErrorRow(transaction, context())).toEqual({
      kind: "choices",
      value: "stop",
      hints: [
        "A transaction in doubt always waits for a person; On error never handles it.",
      ],
    });
  });

  it("keeps Stop the run where the platform can't continue after errors", () => {
    expect(
      onErrorRow(transaction, {
        ...context(),
        features: [],
        onErrorKinds: null,
      }),
    ).toEqual({
      kind: "read-only",
      reason: "Update the platform to continue after errors.",
    });
  });
});

describe("the manifest's mark", () => {
  it("offers Transaction wherever the platform runs transactions", () => {
    // The language manifest as the platform serves it.
    const { manifest } = JSON.parse(
      readFileSync(
        resolve(import.meta.dirname, "fixtures/language/manifest.json"),
        "utf8",
      ),
    ) as { manifest: unknown };
    const language = languageTarget(manifest)!;
    expect(language.features).toContain("flow.transaction");
    expect(language.pending.has("transaction")).toBe(false);
    expect(
      entryById(buildCatalog(catalogInput({ language })), "kind:transaction")
        .disabled,
    ).toBeNull();
  });
});
