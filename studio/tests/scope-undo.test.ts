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
// What a field can read inside a transaction and in a step's undo, offline,
// as the compiler's scope_at says it in the shared scope cases: the step's
// operation key and outcome and its transaction in an undo step, with the
// step's own output left out of a release and maybe empty in a reverse;
// after a transaction only its result.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { locateDiagnostic } from "../src/app/designer/diagnostic-location";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { enclosingLoops } from "../src/app/editor/formula/loops";
import {
  referenceScope,
  stepOutputSchema,
  visibleRefs,
} from "../src/app/forms/core/scope";
import {
  referenceHint,
  referenceView,
} from "../src/app/forms/ui/reference-combobox";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

interface Entry {
  ref: string;
  source: string;
  stepId?: string;
  label: string;
  breadcrumb: string;
  schema: unknown;
  types: string[];
  typeLabel: string;
  optional: boolean;
}
const { cases } = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/language/scope.json"),
    "utf8",
  ),
) as {
  cases: {
    name: string;
    document: unknown;
    path: string;
    result: { found: boolean; evaluated: boolean; entries: Entry[] };
  }[];
};
/** The cases about transactions and the undo of their steps. */
const transactions = cases.filter(({ document }) =>
  /"kind": ?"transaction"/.test(JSON.stringify(document)),
);
if (transactions.length < 5)
  throw new Error("The shared scope cases hold no transactions to compare.");
const shown = ({
  ref,
  source,
  stepId,
  label,
  breadcrumb,
  types,
  typeLabel,
  optional,
}: Entry) => ({
  ref,
  source,
  stepId,
  label,
  breadcrumb,
  types,
  typeLabel,
  optional,
});

const MOVE = {
  spec: {
    inputSchema: {
      type: "object",
      properties: { moveId: { type: "string" } },
      required: ["moveId"],
    },
    steps: [
      {
        id: "lookup",
        kind: "action",
        uses: "lookup@1.0.0",
        with: { literal: {} },
      },
      {
        id: "move",
        kind: "transaction",
        businessKey: { ref: "/input/moveId" },
        steps: [
          {
            id: "hold",
            kind: "action",
            uses: "hold@1.0.0",
            with: { literal: {} },
            confirm: { kind: "action", uses: "c@1.0.0", with: {} },
            cancel: { kind: "action", uses: "r@1.0.0", with: {} },
          },
          {
            id: "add",
            kind: "action",
            uses: "add@1.0.0",
            with: { literal: {} },
            compensate: { kind: "action", uses: "x@1.0.0", with: {} },
            onError: "continue",
          },
        ],
        output: { ref: "/steps/add/output" },
      },
      { id: "notify", kind: "transform", value: { literal: {} } },
    ],
    output: { literal: null },
  },
};
const options = {
  actionOutput: (uses: string) => ({
    type: "object",
    properties: { [`${uses.split("@")[0]}Id`]: { type: "string" } },
    required: [`${uses.split("@")[0]}Id`],
  }),
};
const refs = (step: string, field: string) =>
  visibleRefs(MOVE, step, field, options).map((entry) => entry.ref);

describe("the data a transaction's fields read, as the compiler offers it", () => {
  for (const { name, document, path, result } of transactions)
    it(name, () => {
      const at = locateDiagnostic(path, document);
      expect(at.exact).toBe(true);
      const scope = referenceScope(document, at.stepId, at.fieldPath);
      expect([scope.found, scope.evaluated]).toEqual([
        result.found,
        result.evaluated,
      ]);
      expect(scope.entries.map((entry) => shown(entry as Entry))).toEqual(
        result.entries
          .filter(
            (entry) => entry.source !== "item" && entry.source !== "index",
          )
          .map(shown),
      );
      // What only an undo step reads has the language's own schemas.
      const undo = (entries: readonly Entry[]) =>
        entries
          .filter(({ source }) => source === "undo")
          .map(({ ref, schema }) => [ref, schema]);
      expect(undo(scope.entries as readonly Entry[])).toEqual(
        undo(result.entries),
      );
    });

  it("inside an undo step is the operation key and the transaction first, then what the step could read", () => {
    const undo = [
      "/participant/operationKey",
      "/participant/outcome",
      "/transaction/id",
      "/transaction/businessKey",
    ];
    const before = ["/input", "/input/moveId"];
    const lookup = ["/steps/lookup/output", "/steps/lookup/output/lookupId"];
    const hold = ["/steps/hold/output", "/steps/hold/output/holdId"];
    expect(refs("hold", "/confirm/with")).toEqual([
      ...before,
      ...undo,
      ...lookup,
      ...hold,
    ]);
    expect(refs("hold", "/cancel/with")).toEqual([
      ...before,
      ...undo,
      ...lookup,
    ]);
    // A step whose outcome is unknown may have no output: its fields aren't listed.
    expect(refs("add", "/compensate/with/object/addId")).toEqual([
      ...before,
      ...undo,
      ...lookup,
      ...hold,
      "/steps/add/output",
    ]);
    const own = visibleRefs(MOVE, "add", "/compensate/with", options).at(-1)!;
    expect([own.ref, own.optional, own.types]).toEqual([
      "/steps/add/output",
      true,
      ["object", "null"],
    ]);
    // The step's own error isn't there yet, though it handles its failures.
    expect(refs("add", "/compensate/with")).not.toContain("/steps/add/error");
    // What isn't a step's undo reads none of it.
    expect(refs("add", "/with")).toEqual([...before, ...lookup, ...hold]);
  });

  it("is the body's steps in its result, the steps before it in its business key", () => {
    expect(refs("move", "/output")).toEqual([
      "/input",
      "/input/moveId",
      "/steps/lookup/output",
      "/steps/lookup/output/lookupId",
      "/steps/hold/output",
      "/steps/hold/output/holdId",
      "/steps/add/output",
      "/steps/add/error",
      ...[
        "code",
        "message",
        "outcome",
        "attempts",
        "instanceKey",
        "childRunId",
      ].map((field) => `/steps/add/error/${field}`),
    ]);
    expect(refs("move", "/businessKey")).toEqual([
      "/input",
      "/input/moveId",
      "/steps/lookup/output",
      "/steps/lookup/output/lookupId",
    ]);
    // Only a transaction has a result of its own.
    expect(referenceScope(MOVE, "hold", "/output").found).toBe(false);
  });

  it("is only the transaction's result after it, with the shape it returns", () => {
    expect(refs("notify", "/value").slice(-1)).toEqual(["/steps/move/output"]);
    expect(stepOutputSchema(MOVE, "move", options)).toMatchObject({
      anyOf: [{ properties: { addId: { type: "string" } } }, { type: "null" }],
    });
  });

  it("ends where a transaction's body can't finish", () => {
    const ends = structuredClone(MOVE);
    ends.spec.steps[1].steps!.push({ id: "stop", kind: "fail" } as never);
    expect(
      visibleRefs(ends, "notify", "/value", options).map(({ ref }) => ref),
    ).toEqual([
      "/input",
      "/input/moveId",
      "/steps/lookup/output",
      "/steps/lookup/output/lookupId",
    ]);
    expect(referenceScope(ends, "move", "/output").evaluated).toBe(false);
  });
});

describe("the data picker inside a step's undo", () => {
  const groups = (
    step: string,
    phase: "confirm" | "cancel" | "compensate",
    query = "",
  ) =>
    referenceView(
      visibleRefs(MOVE, step, `/${phase}/with`, options),
      query,
      null,
      { step, phase },
    ).groups.map(({ label, kind, note, options: listed }) => ({
      label,
      kind,
      note: note ?? "",
      fields: listed.map(({ option }) => option.label),
    }));

  it("starts with This step's undo, and says why a release reads nothing of its step", () => {
    const [undo, ...others] = groups("hold", "cancel");
    expect(undo).toEqual({
      label: "This step's undo",
      kind: "undo",
      note: "hold's own output isn't offered here: Release must work even if the hold didn't go through.",
      fields: [
        "Operation key",
        "Outcome of hold",
        "Transaction ID",
        "Business key",
      ],
    });
    expect(others.map(({ label }) => label).sort()).toEqual([
      "Workflow input",
      "lookup",
    ]);
  });

  it("puts the step's own output next, and says when a reverse finds it empty", () => {
    const [undo, own, ...others] = groups("add", "compensate");
    expect([undo.label, undo.note]).toEqual(["This step's undo", ""]);
    expect([own.label, own.note]).toEqual([
      "add · this step's output",
      "Empty when add failed with an unknown outcome.",
    ]);
    expect(others.map(({ label }) => label).sort()).toEqual([
      "Workflow input",
      "hold",
      "lookup",
    ]);
    const [, confirm] = groups("hold", "confirm");
    expect([confirm.label, confirm.note]).toEqual([
      "hold · this step's output",
      "",
    ]);
  });

  it("keeps a group's note only while the group is listed", () => {
    expect(groups("add", "compensate", "moveId")).toHaveLength(1);
    expect(groups("hold", "cancel", "operation")).toMatchObject([
      { label: "This step's undo", fields: ["Operation key"] },
    ]);
  });

  it("reads an undo reference as one of the suggestions", () => {
    const entries = visibleRefs(MOVE, "add", "/compensate/with", options);
    expect(referenceHint("/participant/operationKey", entries)).toEqual({
      tone: "info",
      message: "Text",
    });
    expect(referenceHint("/transaction/nope", entries)).toEqual({
      tone: "info",
      message: "Not in the suggestions. Validate to check this reference.",
    });
    // Outside an undo step neither root is there.
    expect(
      referenceHint("/transaction/id", visibleRefs(MOVE, "add", "/with")),
    ).toMatchObject({ tone: "warning" });
  });
});

describe("a field of a step's undo in step details", () => {
  function session(stepId: string, definition: unknown = workflow()) {
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(definition), "json");
    const host = {
      model,
      perform: (edit: () => void) => edit(),
      notify: () => undefined,
      refreshView: () => undefined,
      catalogContracts: new Map(),
      decisionContracts: new Map(),
      actionVersions: [],
      label: (kind: string) => kind,
      editingLocked: false,
      profile: null,
      api: {
        request: async () => ({
          found: true,
          evaluated: true,
          truncated: false,
          entries: loopCase.result.entries,
        }),
      } as never,
      diagnostics: null,
      diagnosticsDefinition: null,
      cacheDecisionContract: () => undefined,
      can: () => true,
    } as unknown as StepDetailsHost;
    const controller = new StepDetailsController(host, () => 0);
    return {
      session: new FormSession(host, controller, openRequest(stepId), {
        confirm: async () => true,
        announce: () => undefined,
      }),
      controller,
      model,
    };
  }
  const workflow = (): Workflow =>
    ({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "move-between-systems", version: "1.0.0" },
      ...structuredClone(MOVE),
    }) as unknown as Workflow;
  const loopCase = {
    result: {
      entries: transactions.find(({ name }) => name.startsWith("a reverse"))!
        .result.entries,
    },
  };
  const spec = (phase: string): ParamSpec => ({
    id: `${phase}.input.reference`,
    path: [phase, "with", "reference"],
    type: "text",
    label: "Reference",
    mapping: "both",
  });

  it("reads the undo's data, and warns of nothing the compiler allows", () => {
    const { session: form, controller } = session("hold");
    expect(form.scope(spec("confirm")).map(({ ref }) => ref)).toContain(
      "/steps/hold/output",
    );
    controller.commit(
      "hold",
      [
        {
          scope: "step",
          path: ["confirm", "with"],
          value: {
            object: { reference: { ref: "/steps/hold/output" } },
          },
        },
      ],
      "confirm",
    );
    expect(form.formulaWarning(spec("confirm"))).toBeNull();
    controller.commit(
      "hold",
      [
        {
          scope: "step",
          path: ["cancel", "with"],
          value: {
            object: { reference: { ref: "/steps/hold/output" } },
          },
        },
      ],
      "cancel",
    );
    expect(form.formulaWarning(spec("cancel"))).toContain("hold");
  });

  it("names an undo reference in words, with the undo icon", () => {
    const { session: form } = session("add");
    const field = Object.create(FormulaField.prototype) as FormulaField;
    Object.assign(field, {
      session: () => form,
      spec: () => spec("compensate"),
    });
    expect(field.breadcrumbOf("/participant/operationKey")).toBe(
      "This step's undo › Operation key",
    );
    expect(field.iconOf("/participant/operationKey")).toBe("undo");
    expect(field.iconOf("/steps/add/output")).toBe("action");
  });

  it("keeps the undo's data in what the Studio host says a loop's step reads", async () => {
    const looped = workflow();
    const body = looped.spec.steps[1]["steps"];
    looped.spec.steps[1]["steps"] = [
      {
        id: "each",
        kind: "forEach",
        items: { literal: [] },
        maxItems: 3,
        body: { steps: body, output: { literal: null } },
      },
    ];
    expect(enclosingLoops(looped, "add")).toEqual(["each"]);
    const { session: form } = session("add", looped);
    form.formulaScope(spec("compensate"));
    await new Promise((done) => setTimeout(done, 0));
    expect(
      form
        .formulaScope(spec("compensate"))
        .entries.filter(({ source }) => source === "undo")
        .map(({ ref }) => ref),
    ).toHaveLength(4);
  });
});
