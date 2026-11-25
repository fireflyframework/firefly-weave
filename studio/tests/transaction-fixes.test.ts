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
// The fix of each transaction finding: what it is called, what it names for a
// screen reader, and what it makes of the workflow in one undo step; and the
// summary before publishing a workflow whose transaction isn't safe. The
// findings are the platform's own, as the repository's compiler reports them.
import "@angular/compiler";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { diagnosticRows } from "../src/app/designer/diagnostics-list";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  publishOwnedFirst,
  publishRefused,
} from "../src/app/editor/ndv/owned/owned-commands";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import {
  applyTransactionFix,
  transactionFix,
} from "../src/app/editor/transactions/fixes";
import {
  problemsText,
  publishSummary,
  transactionProblems,
} from "../src/app/editor/transactions/publish-summary";
import { runProblemFix } from "../src/app/editor/transactions/run-fix";
import { StructuredCanvasAdapter, type Step } from "../src/app/model";
import { python, pythonAvailable, repository } from "./python-path";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

interface Finding {
  code: string;
  severity: string;
  path: string;
  message: string;
  suggestedEdit?: unknown;
}
const CATALOG = join(repository, "tests/fixtures/transactions/catalog.yaml");
const CHECK = `
import json, sys, yaml
from pathlib import Path
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
fixture = yaml.safe_load(Path(sys.argv[1]).read_text())
catalog = CatalogSnapshot.from_definitions([load_definition(d) for d in fixture["definitions"]], tasks=fixture["tasks"])
out = {}
for name, document in json.loads(sys.stdin.read()).items():
    result = json.loads(compile_source(json.dumps(document), format="json", catalog=catalog).to_bytes())
    out[name] = [d for d in result["diagnostics"] if d["code"].startswith("WV-COMP-TXN_")]
print(json.dumps(out))
`;

const QUANTITY = {
  object: {
    quantity: { ref: "/input/quantity" },
    reference: { ref: "/input/moveId" },
  },
};
const op = (uses: string, value: unknown, kind = "action") => ({
  kind,
  uses,
  with: value,
});
const REFERENCE = { object: { reference: { ref: "/input/moveId" } } };
const hold = (fields: object = {}) => ({
  id: "hold",
  kind: "action",
  uses: "source-system.hold-quantity@1.0.0",
  with: QUANTITY,
  confirm: op("source-system.confirm-hold@1.0.0", {
    object: { holdId: { ref: "/steps/hold/output/holdId" } },
  }),
  cancel: op("source-system.release-hold@1.0.0", {
    object: {
      reference: { ref: "/input/moveId" },
      holdOperation: { ref: "/participant/operationKey" },
    },
  }),
  ...fields,
});
const REVERSE = op("target-system.remove-quantity@1.0.0", {
  object: {
    reference: { ref: "/input/moveId" },
    addOperation: { ref: "/participant/operationKey" },
  },
});
const add = (fields: object = { compensate: REVERSE }) => ({
  id: "add",
  kind: "action",
  uses: "target-system.add-quantity@1.0.0",
  with: QUANTITY,
  ...fields,
});
const submit = (fields: object = { irreversible: true }) => ({
  id: "submit",
  kind: "action",
  uses: "orders.submit-order@1.0.0",
  with: REFERENCE,
  ...fields,
});
const move = (steps: object[], extra: object = {}) => ({
  id: "move",
  kind: "transaction",
  steps,
  output: { literal: null },
  ...extra,
});
const document = (...steps: object[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "move-between-systems", version: "1.4.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: {
        moveId: { type: "string", minLength: 1, maxLength: 200 },
        quantity: { type: "integer", minimum: 1 },
      },
      required: ["moveId", "quantity"],
    },
    outputSchema: {},
    steps,
    output: { literal: null },
  },
});
const loop = (...steps: object[]) => ({
  id: "each",
  kind: "forEach",
  items: { literal: [1, 2] },
  maxItems: 3,
  concurrency: 1,
  body: { steps, output: { literal: null } },
});
const parallel = (...branches: object[]) => ({
  id: "both",
  kind: "parallel",
  concurrency: 2,
  branches: Object.fromEntries(
    branches.map((step, index) => [
      `b${index}`,
      { steps: [step], output: { literal: null } },
    ]),
  ),
});
const pause = { id: "pause", kind: "wait", durationSeconds: 7200 };
const notify = { id: "notify", kind: "transform", value: { literal: {} } };

/** A workflow for each finding, named for it. */
const WORKFLOWS = {
  order: document(move([hold(), submit(), add()]), notify),
  orderInPath: document(
    move([
      hold(),
      {
        id: "check",
        kind: "switch",
        cases: [
          {
            when: {
              op: { name: "exists", args: [{ ref: "/input/moveId" }] },
            },
            steps: [submit(), add()],
            output: { literal: null },
          },
        ],
        default: { steps: [], output: { literal: null } },
      },
    ]),
  ),
  inLoop: document(move([hold(), loop(submit())]), notify),
  inBothLoops: document(
    move([hold(), { ...loop(parallel(submit(), add())), id: "outer" }]),
  ),
  beside: document(move([hold(), parallel(submit(), add())])),
  unsafe: document(
    move([hold(), submit(), { ...add({ irreversible: true }) }]),
    notify,
  ),
  undeclared: document(move([hold(), add({})])),
  wait: document(move([hold(), pause, submit()])),
  ownHoldWait: document(move([hold({ holdSeconds: 60 }), pause, submit()])),
  shortHold: document(move([hold({ holdSeconds: 30 }), submit()])),
  output: document(
    move([
      hold({
        cancel: op("source-system.release-hold@1.0.0", {
          object: {
            reference: { ref: "/steps/hold/output/holdId" },
            holdOperation: { ref: "/participant/operationKey" },
          },
        }),
      }),
      submit(),
    ]),
  ),
  unsafeUndo: document(
    move([
      hold(),
      add({ compensate: op("orders.submit-order@1.0.0", REFERENCE) }),
    ]),
  ),
  noLimit: document(
    move([
      hold(),
      add({
        compensate: op(
          "reverse-without-limit@1.0.0",
          { literal: {} },
          "callWorkflow",
        ),
      }),
    ]),
  ),
  calledNoLimit: document(
    move([
      hold(),
      {
        id: "call",
        kind: "callWorkflow",
        uses: "reverse-without-limit@1.0.0",
        mode: "wait",
        with: { literal: {} },
        irreversible: true,
      },
    ]),
  ),
  outside: document(add(), notify),
  detached: document(
    move([
      hold(),
      {
        id: "call",
        kind: "callWorkflow",
        uses: "reverse-target-addition@1.0.0",
        mode: "detach",
        with: REFERENCE,
      },
    ]),
  ),
};
type Name = keyof typeof WORKFLOWS;
const available = pythonAvailable();
const found: Record<Name, Finding[]> = available
  ? (JSON.parse(
      execFileSync(python, ["-c", CHECK, CATALOG], {
        cwd: repository,
        input: JSON.stringify(WORKFLOWS),
        encoding: "utf8",
        timeout: 180_000,
      }),
    ) as Record<Name, Finding[]>)
  : ({} as Record<Name, Finding[]>);

/** The Diagnostics row of a finding in its workflow. */
function row(name: Name, code: string) {
  const { problems, notes } = diagnosticRows(
    found[name] as never,
    WORKFLOWS[name],
  );
  const listed = [...problems, ...notes].find(
    ({ view }) => view.code === `WV-COMP-${code}`,
  );
  if (!listed)
    throw new Error(
      `${name} has no ${code}: ${found[name].map((f) => f.code).join()}`,
    );
  return listed;
}
/** The workflow open in an editor, and what its host does for fixes. */
function editor(name: Name) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(WORKFLOWS[name]), "json");
  model.clearHistory();
  const opened: unknown[] = [];
  const workflows: string[] = [];
  const toasts: string[] = [];
  const undos: (() => void)[] = [];
  const host = {
    model,
    error: "",
    diagnosticsDefinition: model.definition,
    ensureApplied: async () => true,
    perform: (edit: () => void) => edit(),
    notify: (text: string, action?: { run(): void }) => {
      toasts.push(text);
      if (action) undos.push(() => action.run());
    },
    undoIfUnchanged: async (still: () => boolean) => {
      if (!still()) return "changed" as const;
      model.undo();
      return "undone" as const;
    },
    applySuggestion: async (change: { stepId: string; value: unknown }) =>
      model.update(change.stepId, JSON.stringify(change.value)),
    openStepDetails: async (target: string, request: unknown) => {
      opened.push([target, request]);
      return true;
    },
    openDiagnostic: async (at: unknown) => void opened.push(["open", at]),
    openWorkflow: (uses: string) => void workflows.push(uses),
    editorNext: true,
  };
  return { model, host, opened, workflows, toasts, undos };
}
const ids = (steps: unknown): string[] =>
  (steps as Step[]).map((step) => step.id);
const runs = async (name: Name, code: string) => {
  const made = editor(name);
  const { fix, location } = row(name, code);
  await runProblemFix(made.host as never, fix!, location!);
  return made;
};
/** What runs after a fix is one undo step: Undo brings the workflow back. */
const oneStep = ({ model }: ReturnType<typeof editor>, name: Name) => {
  model.undo();
  expect(model.definition).toEqual(WORKFLOWS[name]);
  expect(model.canUndo).toBe(false);
};

describe.skipIf(!available)("the fix of each transaction finding", () => {
  it("moves the step that can't be undone to the end", async () => {
    const { fix, where, view } = row("order", "TXN_ORDER");
    expect(view.text).toBe(
      "This step can be undone, but it runs after a step that can't. Steps that can't be undone must come last in a transaction.",
    );
    expect(where).toBe("Step add · How it's undone");
    expect(fix).toMatchObject({
      kind: "suggestion",
      label: "Move to end",
      name: "Move to end: Move submit to the end of transaction move",
    });
    const made = await runs("order", "TXN_ORDER");
    expect(ids(made.model.definition.spec.steps[0]["steps"])).toEqual([
      "hold",
      "add",
      "submit",
    ]);
    oneStep(made, "order");
    expect(row("orderInPath", "TXN_ORDER").fix).toMatchObject({
      label: "Move to end",
      name: expect.stringMatching(
        /^Move to end: Move submit to the end of Case 1 of check$/,
      ),
    });
  });

  it("moves a step that decides the transaction out of its loop or parallel step", async () => {
    expect(row("inLoop", "TXN_PIVOT_PLACEMENT").fix).toMatchObject({
      kind: "move",
      label: "Move out",
      name: "Move out: Move submit out of each",
    });
    const looped = await runs("inLoop", "TXN_PIVOT_PLACEMENT");
    expect(ids(looped.model.definition.spec.steps[0]["steps"])).toEqual([
      "hold",
      "each",
      "submit",
    ]);
    expect(looped.toasts).toEqual(["Moved submit out of each."]);
    oneStep(looped, "inLoop");
    const beside = await runs("beside", "TXN_PIVOT_PLACEMENT");
    expect(ids(beside.model.definition.spec.steps[0]["steps"])).toEqual([
      "hold",
      "both",
      "submit",
    ]);
    // Out of every loop and parallel step around it, once.
    const both = await runs("inBothLoops", "TXN_PIVOT_PLACEMENT");
    expect(ids(both.model.definition.spec.steps[0]["steps"])).toEqual([
      "hold",
      "outer",
      "submit",
    ]);
  });

  it("moves a step that isn't safe to retry after the transaction", async () => {
    expect(row("unsafe", "TXN_AFTER_PIVOT_UNSAFE").fix).toMatchObject({
      kind: "move",
      label: "Move after transaction",
      name: "Move after transaction: Move add after transaction move",
    });
    const made = await runs("unsafe", "TXN_AFTER_PIVOT_UNSAFE");
    expect(ids(made.model.definition.spec.steps)).toEqual([
      "move",
      "add",
      "notify",
    ]);
    expect(made.toasts).toEqual(["Moved add after transaction move."]);
    oneStep(made, "unsafe");
  });

  it("opens How it's undone for a step without a choice, and never chooses", async () => {
    const { fix, where, change } = row("undeclared", "TXN_UNDECLARED");
    expect(where).toBe("Step add · How it's undone");
    expect(change).toBeNull();
    expect(fix).toEqual({
      kind: "undo",
      label: "Choose how it's undone",
      name: "Choose how it's undone: add",
    });
    const made = await runs("undeclared", "TXN_UNDECLARED");
    expect(made.opened).toEqual([
      [
        "add",
        {
          revealAll: true,
          focus: { kind: "path", path: ["irreversible"] },
        },
      ],
    ]);
    expect(made.model.canUndo).toBe(false);
  });

  it("opens what sets the shortest hold before a wait", async () => {
    const { fix, where } = row("wait", "TXN_HOLD_WAIT");
    expect(where).toBe("Step pause · Duration");
    expect(fix).toMatchObject({ kind: "hold", label: "Set Hold for" });
    // Every reservation holds for what the transaction says.
    expect((await runs("wait", "TXN_HOLD_WAIT")).opened).toEqual([
      [
        "move",
        {
          revealAll: true,
          focus: { kind: "path", path: ["policy", "holdSeconds"] },
        },
      ],
    ]);
    expect((await runs("ownHoldWait", "TXN_HOLD_WAIT")).opened).toEqual([
      [
        "hold",
        { revealAll: true, focus: { kind: "path", path: ["holdSeconds"] } },
      ],
    ]);
  });

  it("writes the hold the step that can't be undone needs, and opens it", async () => {
    expect(row("shortHold", "TXN_HOLD_PIVOT").fix).toMatchObject({
      kind: "suggestion",
      label: "Set Hold for",
      name: "Set Hold for: hold",
    });
    const made = await runs("shortHold", "TXN_HOLD_PIVOT");
    const held = (made.model.definition.spec.steps[0]["steps"] as Step[])[0];
    expect(held["holdSeconds"]).toBeGreaterThan(30);
    expect(made.opened).toHaveLength(1);
    oneStep(made, "shortHold");
  });

  it("opens the data picker of a release that reads its step's output", async () => {
    expect(row("output", "TXN_CANCEL_OUTPUT").fix).toMatchObject({
      kind: "picker",
      label: "Open data picker",
    });
    expect((await runs("output", "TXN_CANCEL_OUTPUT")).opened).toEqual([
      [
        "hold",
        {
          revealAll: true,
          focus: {
            kind: "path",
            path: ["cancel", "with", "reference"],
            open: "choices",
          },
        },
      ],
    ]);
  });

  it("opens the action or the workflow an undo step can't use", async () => {
    expect(row("unsafeUndo", "TXN_UNDO_SIDE_EFFECT").fix).toMatchObject({
      kind: "openAction",
      label: "Open action",
    });
    expect((await runs("unsafeUndo", "TXN_UNDO_SIDE_EFFECT")).opened).toEqual([
      [
        "add",
        {
          revealAll: true,
          focus: {
            kind: "path",
            path: ["compensate", "uses"],
            open: "document",
          },
        },
      ],
    ]);
    for (const name of ["noLimit", "calledNoLimit"] as const) {
      expect(row(name, "TXN_CALL_TIMEOUT").fix).toMatchObject({
        kind: "openWorkflow",
        label: "Open workflow",
        uses: "reverse-without-limit@1.0.0",
      });
      expect((await runs(name, "TXN_CALL_TIMEOUT")).workflows).toEqual([
        "reverse-without-limit@1.0.0",
      ]);
    }
  });

  it("clears a choice outside a transaction, and makes a call wait", async () => {
    const outside = await runs("outside", "TXN_OUTSIDE");
    expect(outside.model.definition.spec.steps[0]).toEqual(add({}));
    expect(outside.toasts).toEqual(["Cleared how add is undone."]);
    expect(row("outside", "TXN_OUTSIDE").fix).toMatchObject({
      label: "Clear How it's undone",
      name: "Clear How it's undone: add",
    });
    oneStep(outside, "outside");
    const detached = await runs("detached", "TXN_DETACH");
    expect(
      (detached.model.definition.spec.steps[0]["steps"] as Step[])[1]["mode"],
    ).toBe("wait");
    expect(detached.toasts).toEqual(["call waits for its result."]);
    expect(row("detached", "TXN_DETACH").fix).toMatchObject({
      label: "Wait for its result",
      name: "Wait for its result: call",
    });
    oneStep(detached, "detached");
  });

  it("changes nothing for a step that isn't there any more", async () => {
    const { model } = editor("unsafe");
    const before = model.source;
    for (const fix of [
      row("unsafe", "TXN_AFTER_PIVOT_UNSAFE").fix!,
      row("outside", "TXN_OUTSIDE").fix!,
    ])
      expect(applyTransactionFix(model, fix, "gone")).toBe(false);
    expect(model.source).toBe(before);
    const made = editor("outside");
    const { fix, location } = row("outside", "TXN_OUTSIDE");
    await runProblemFix(made.host as never, fix!, {
      ...location!,
      stepId: "gone",
    });
    expect([made.toasts, made.model.canUndo]).toEqual([[], false]);
  });

  it("offers Undo until the workflow changes again", async () => {
    const undone = await runs("unsafe", "TXN_AFTER_PIVOT_UNSAFE");
    undone.undos[0]();
    expect(undone.model.definition).toEqual(WORKFLOWS.unsafe);
    const changed = await runs("unsafe", "TXN_AFTER_PIVOT_UNSAFE");
    changed.model.update("notify", JSON.stringify({ ...notify, notes: "x" }));
    const after = changed.model.source;
    changed.undos[0]();
    expect(changed.model.source).toBe(after);
  });

  it("makes no change while an edit in step details can't be applied", async () => {
    const made = editor("unsafe");
    const { fix, location } = row("unsafe", "TXN_AFTER_PIVOT_UNSAFE");
    made.host.ensureApplied = async () => false;
    await runProblemFix(made.host as never, fix!, location!);
    expect([made.toasts, made.model.canUndo]).toEqual([[], false]);
  });

  it("opens no field when the hold wasn't written", async () => {
    const made = editor("shortHold");
    const { fix, location } = row("shortHold", "TXN_HOLD_PIVOT");
    made.host.applySuggestion = async () => {
      made.host.error = "The workflow changed since this check.";
      return undefined as never;
    };
    await runProblemFix(made.host as never, fix!, location!);
    expect(made.opened).toEqual([]);
  });

  it("shows the step in the classic editor, which has no How it's undone", async () => {
    const made = editor("undeclared");
    const { fix, location } = row("undeclared", "TXN_UNDECLARED");
    made.host.editorNext = false;
    await runProblemFix(made.host as never, fix!, location!);
    expect(made.opened).toEqual([["open", location]]);
  });

  it("is the same in step details", async () => {
    const made = editor("order");
    const host = {
      ...made.host,
      refreshView: () => undefined,
      catalogContracts: new Map(),
      decisionContracts: new Map(),
      actionVersions: [],
      label: (kind: string) => kind,
      editingLocked: false,
      profile: null,
      api: { request: async () => ({ features: ["flow.transaction"] }) },
      diagnostics: { diagnostics: found.order },
      cacheDecisionContract: () => undefined,
      can: () => true,
    } as unknown as StepDetailsHost;
    const controller = new StepDetailsController(host, () => 0);
    const form = new FormSession(host, controller, openRequest("add"), {
      confirm: async () => true,
      announce: () => undefined,
    });
    const issue = form
      .issues()
      .find(({ message }) => /must come last/.test(message))!;
    expect(issue.fix).toMatchObject({ label: "Move to end" });
    form.runFix(issue);
    await Promise.resolve();
    expect(ids(made.model.definition.spec.steps[0]["steps"])).toEqual([
      "hold",
      "add",
      "submit",
    ]);
  });
});

describe("a finding's fix", () => {
  const at = (stepId: string, ...steps: object[]) => ({
    stepId,
    at: [],
    workflow: document(move(steps)) as never,
  });
  const held = (id: string, fields: object) => ({ ...hold(fields), id });

  it("is Studio's own only for a transaction's findings", () => {
    expect(
      transactionFix("WV-COMP-UNKNOWN_ACTION", null, at("add", add())),
    ).toBeUndefined();
    expect(
      transactionFix("WV-COMP-TXN_POLICY", null, at("add", add())),
    ).toBeUndefined();
  });

  it("is none without what it would open", () => {
    expect(
      transactionFix("WV-COMP-TXN_CALL_TIMEOUT", null, at("notify", notify)),
    ).toBeNull();
    expect(transactionFix("WV-COMP-TXN_HOLD_PIVOT", null, null)).toBeNull();
    expect(transactionFix("WV-COMP-TXN_HOLD_WAIT", null, null)).toBeNull();
  });

  it("finds the shortest hold of the steps that wait, wherever they are", () => {
    const opens = (...steps: object[]) =>
      transactionFix("WV-COMP-TXN_HOLD_WAIT", null, at("pause", ...steps));
    expect(
      opens(
        held("slow", { holdSeconds: 600 }),
        loop(held("short", { holdSeconds: 60 })),
        held("long", { holdSeconds: 900 }),
        pause,
      ),
    ).toMatchObject({ stepId: "short", path: ["holdSeconds"] });
    // The transaction's own is the shortest, or as short.
    for (const own of [
      held("same", { holdSeconds: 3600 }),
      { ...add({ holdSeconds: 60, compensate: REVERSE }), id: "reversed" },
    ])
      expect(
        transactionFix("WV-COMP-TXN_HOLD_WAIT", null, {
          ...at("pause"),
          workflow: document(
            move([own, pause], { policy: { holdSeconds: 3600 } }),
          ) as never,
        }),
      ).toMatchObject({ stepId: "move", path: ["policy", "holdSeconds"] });
  });

  it("moves no step that left its transaction since the check", () => {
    const { model } = editor("order");
    model.setSource(
      JSON.stringify(document({ ...loop(loop(notify)), id: "outer" })),
      "json",
    );
    const before = model.source;
    for (const to of ["out", "after"] as const)
      expect(applyTransactionFix(model, { kind: "move", to }, "notify")).toBe(
        false,
      );
    expect(model.source).toBe(before);
  });

  it("clears nothing from a step without a choice", () => {
    const { model } = editor("undeclared");
    expect(applyTransactionFix(model, { kind: "clearUndo" }, "add")).toBe(
      false,
    );
    expect(model.canUndo).toBe(false);
  });
});

describe.skipIf(!available)("publishing a transaction that isn't safe", () => {
  const result = (name: Name, more: object[] = []) => ({
    validationOk: false,
    errorCount: 1,
    diagnostics: [...found[name], ...more] as never[],
  });
  const other = {
    code: "WV-COMP-UNKNOWN_ACTION",
    severity: "error",
    path: "/spec/steps/1/uses",
    message: "x",
  };

  it("lists its errors with their fixes, and only those", () => {
    const problems = transactionProblems(
      result("order", [other]),
      WORKFLOWS.order,
    );
    expect(
      problems.map(({ step, text, fix }) => [step, text, fix?.label]),
    ).toEqual([["add", row("order", "TXN_ORDER").view.text, "Move to end"]]);
    expect(problemsText(problems)).toBe(
      "This transaction isn't safe to publish: 1 problem",
    );
    expect(problemsText([...problems, ...problems])).toBe(
      "This transaction isn't safe to publish: 2 problems",
    );
    // What doesn't stop publishing isn't a problem here.
    expect(
      transactionProblems(result("wait"), WORKFLOWS.wait).map(
        ({ text }) => text,
      ),
    ).toEqual([]);
    expect(transactionProblems(result("outside", []), {})).toHaveLength(0);
  });

  it("counts steps without a choice and transactions without steps too", () => {
    const studio = (code: string, path: string, message: string) => ({
      code,
      severity: "error",
      path,
      message,
    });
    expect(
      transactionProblems(
        {
          ...result("order"),
          diagnostics: [
            studio(
              "WV-STUDIO-TRANSACTION",
              "/spec/steps/0",
              "Add a step to the transaction.",
            ),
            studio(
              "WV-STUDIO-UNDO",
              "/spec/steps/0/steps/2/compensate/uses",
              "Choose what reverses this step.",
            ),
          ] as never[],
        },
        WORKFLOWS.order,
      ).map(({ step, text }) => [step, text]),
    ).toEqual([
      ["move", "Add a step to the transaction."],
      ["add", "Choose what reverses this step."],
    ]);
  });

  it("shows when the platform refuses the workflow, with how many problems it found", () => {
    const { host } = editor("order");
    expect(publishRefused(host as never, result("order", [other]))).toBe(
      "2 errors",
    );
    expect(publishSummary(host)()?.problems).toHaveLength(1);
    const { host: clean } = editor("wait");
    expect(
      publishRefused(clean as never, { diagnostics: [], errorCount: 0 }),
    ).toBe("1 error");
    expect(publishSummary(clean)()).toBeNull();
  });

  it("shows before Publish… goes on, and only when a transaction stopped it", async () => {
    const publish = async (
      name: Name,
      passes: boolean,
      more: object[] = [],
    ) => {
      const made = editor(name);
      const host = Object.assign(made.host, {
        diagnostics: result(name, more),
        busy: "",
        errorCode: "",
        can: () => true,
        refreshView: () => undefined,
        actionVersions: [],
      });
      const outcome = await publishOwnedFirst(
        host as never,
        "publish",
        async () => passes,
      );
      return { outcome, summary: publishSummary(host)() };
    };
    const stopped = await publish("order", false);
    expect(stopped.outcome).toEqual({ result: "stopped", published: 0 });
    expect(stopped.summary).toMatchObject({
      name: "move-between-systems",
      version: "1.4.0",
    });
    expect(stopped.summary?.problems).toHaveLength(1);
    expect((await publish("order", true)).summary).toBeNull();
    expect((await publish("wait", false, [other])).summary).toBeNull();
  });
});

it("has no finding of the compiler's list without a fix or a reason", () => {
  // The codes whose fix the platform, not Studio, knows or where none applies.
  const without = [
    "TXN_AFTER_PIVOT_PURE",
    "TXN_AFTER_PIVOT_WAIT",
    "TXN_AGENT",
    "TXN_NESTED",
    "TXN_NOTHING_TO_UNDO",
    "TXN_RESERVE_PAIR",
    "TXN_SCOPE",
    "TXN_SIZE",
    "TXN_UNDO_FORMS",
  ];
  const copy = readFileSync(
    join(repository, "studio/src/app/designer/diagnostic-copy.ts"),
    "utf8",
  );
  const fixes = readFileSync(
    join(repository, "studio/src/app/editor/transactions/fixes.ts"),
    "utf8",
  );
  const codes = [...copy.matchAll(/^ {2}(TXN_[A-Z_]+): \{/gm)].map(
    (match) => match[1],
  );
  expect(codes.length).toBeGreaterThan(15);
  expect(codes.filter((code) => !fixes.includes(code)).sort()).toEqual(without);
});
