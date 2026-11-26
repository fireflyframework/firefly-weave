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
// Simulate a failure on a step of a transaction and on its undo, its release
// or its confirm: what may fail, what the failure may have changed, the key
// each is written under, and what Execute asks when an undo step needs test
// output. Studio's default of what changed follows the platform's rule, which
// a fixture the platform's tests also read keeps equal.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  effectDefault,
  failureKey,
  simulateBlock,
} from "../src/app/editor/ndv/panes/simulate-failure";
import {
  failureWritten,
  simulateOpening,
} from "../src/app/editor/ndv/panes/simulate-opening";
import { runStatus } from "../src/app/editor/debugger/run-log-panel";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import { copySteps, planPaste } from "../src/app/editor/state/clipboard";
import { phaseBlock } from "../src/app/editor/state/execute-gate";
import type { Blocked } from "../src/app/editor/state/execution";
import type { ErrorEntry, PinEntry } from "../src/app/editor/state/test-data";
import { checkInputsOf } from "../src/app/editor/state/test-data-inputs";
import {
  emptyTestData,
  failureOf,
  pinOf,
  withFailure,
  withPin,
} from "../src/app/editor/state/test-data-ops";
import { readTestData } from "../src/app/editor/state/test-data-read";
import { TestDataService } from "../src/app/editor/state/test-data-service";
import {
  phaseKey,
  splitPhase,
} from "../src/app/editor/transactions/phase-keys";
import {
  phaseOperation,
  phasesOf,
  transactionHolding,
} from "../src/app/editor/transactions/phases";
import {
  bannerLine,
  bannerSentence,
  effectChoices,
  effectReason,
  failureLead,
  phaseNotes,
  shownEffect,
  transactionFailure,
} from "../src/app/editor/transactions/simulate-undo";
import { afterPointOfNoReturn } from "../src/app/editor/transactions/points";
import {
  StructuredCanvasAdapter,
  type Step,
  type Workflow,
} from "../src/app/model";
import type { ToastAction } from "../src/app/toast";

beforeAll(() => loadKindRegistrations());

const AT = "2026-11-25T10:00:00Z";
const op = (uses: string, kind = "action") => ({
  kind,
  uses,
  with: { literal: {} },
});
const step = (id: string, uses: string, fields: object = {}) =>
  ({
    id,
    kind: "action",
    uses,
    with: { literal: {} },
    ...fields,
  }) as unknown as Step;
const LOOKUP = step("lookup", "reports.lookup@1.0.0");
const HOLD = step("hold", "source-system.hold-quantity@1.0.0", {
  confirm: op("source-system.confirm-hold@1.0.0"),
  cancel: op("source-system.release-hold@1.0.0"),
});
const ADD = step("add", "target-system.add-quantity@1.0.0", {
  compensate: op("target-system.remove-quantity@1.0.0"),
});
const CALL = {
  ...step("call", "add-elsewhere@1.0.0", {
    mode: "wait",
    compensate: op("reverse-target-addition@1.0.0", "callWorkflow"),
  }),
  kind: "callWorkflow",
} as unknown as Step;
const SUBMIT = step("submit", "orders.submit-order@1.0.0", {
  irreversible: true,
});
const NOTIFY = step("notify", "notify.send-message@1.0.0");
const SHAPE = {
  id: "shape",
  kind: "transform",
  value: { literal: 1 },
} as unknown as Step;
const workflowOf = (inside: unknown[], fields: object = {}) =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "move-between-systems", version: "1.0.0" },
    spec: {
      inputSchema: {},
      outputSchema: {},
      steps: [
        {
          id: "move",
          kind: "transaction",
          steps: inside,
          output: { literal: null },
          ...fields,
        },
        step("after", "target-system.add-quantity@1.0.0"),
      ],
      output: { literal: null },
    },
  }) as unknown as Workflow;
const MOVE = workflowOf([LOOKUP, HOLD, ADD, CALL, SHAPE, SUBMIT, NOTIFY]);
/** What the actions say they return and which codes change nothing. */
const CONTRACTS: Record<string, object> = {
  "target-system.add-quantity@1.0.0": {
    spec: { definiteFailures: ["VALIDATION"], outputSchema: {} },
  },
  "source-system.confirm-hold@1.0.0": {
    spec: { outputSchema: { type: "object", required: ["confirmedAt"] } },
  },
  "source-system.release-hold@1.0.0": { spec: { definiteFailures: ["GONE"] } },
};
const reads = (errors: Record<string, ErrorEntry> = {}) => ({
  contract: (uses: string) => (CONTRACTS[uses] ?? null) as never,
  errors,
});
const failure = (
  id: string,
  workflow = MOVE,
  errors: Record<string, ErrorEntry> = {},
) => transactionFailure(workflow, id, reads(errors));

describe("the undo steps of a step", () => {
  it("are its undo, or its release and its confirm, each at its own key", () => {
    expect(phasesOf(ADD)).toEqual(["undo"]);
    expect(phasesOf(HOLD)).toEqual(["cancel", "confirm"]);
    expect(phasesOf(SUBMIT)).toEqual([]);
    expect(phasesOf({ ...HOLD, confirm: undefined } as Step)).toEqual([
      "cancel",
    ]);
    expect(phaseKey("add[2]", "undo")).toBe("add[2]#undo");
    expect(splitPhase("add[2]#undo")).toEqual({ key: "add[2]", phase: "undo" });
    // A repeated activation of an AI agent isn't an undo step.
    for (const key of ["add", "support#1.2", "add#undo~2", "add#undone"])
      expect(splitPhase(key), key).toBeNull();
    expect(phaseOperation(HOLD, "cancel")).toEqual({
      kind: "action",
      uses: "source-system.release-hold@1.0.0",
      label: "Release with",
    });
    expect(phaseOperation(CALL, "undo")).toMatchObject({
      kind: "callWorkflow",
      uses: "reverse-target-addition@1.0.0",
    });
    expect(phaseOperation(ADD, "confirm")).toBeNull();
  });
});

describe("what Simulate a failure offers inside a transaction", () => {
  it("offers a transform or a decision table without an On error setting", () => {
    for (const kind of ["transform", "decisionTable"]) {
      const pure = { id: "shape", kind } as unknown as Step;
      expect(simulateBlock(pure, false, true), kind).toBeNull();
      expect(simulateBlock(pure, false, false), kind).toMatch(
        /^This step's failure would stop the run\./,
      );
    }
  });

  it("asks what fails on a step that can be undone, and nothing more on one that can't", () => {
    const offered = (id: string) =>
      failure(id)!.phases.map(({ phase, label }) => [phase, label]);
    expect(offered("add")).toEqual([["undo", "Its undo"]]);
    expect(offered("hold")).toEqual([
      ["cancel", "Its release"],
      ["confirm", "Its confirm"],
    ]);
    for (const id of ["submit", "lookup", "shape", "notify"])
      expect(offered(id), id).toEqual([]);
    // Outside a transaction there is nothing to undo.
    expect(failure("after")).toBeNull();
    expect(failure("move")).toBeNull();
  });

  it("suggests what runs the undo may fail with", () => {
    const codes = (id: string) =>
      failure(id)!.phases[0].suggestions.flatMap(({ items }) =>
        items.map(({ code }) => code),
      );
    expect(codes("add")).toContain("UNAVAILABLE");
    expect(codes("add")).not.toContain("WV-CALL-START");
    // Its release isn't the action that holds: each has its own codes.
    expect(codes("hold")).toContain("GONE");
    // Reversed by a called workflow.
    expect(codes("call")).toContain("WV-CALL-START");
    // It starts on one a person waits out.
    const starts = (id: string) => failure(id)!.phases[0].code;
    expect(starts("add")).toBe("UNAVAILABLE");
    expect(starts("call")).toBe("WV-RUN-TIMED_OUT");
  });

  it("takes the tries of an undo from the transaction's Max tries", () => {
    expect(failure("add")!.tries).toBe(10);
    const three = workflowOf([ADD], {
      policy: { undoRetry: { maxAttempts: 3 } },
    });
    expect(failure("add", three)!.tries).toBe(3);
  });

  it("asks what changed only of a step the transaction would undo or can't", () => {
    const asked = (id: string) => failure(id)!.participant?.mode ?? null;
    expect(asked("add")).toBe("compensate");
    expect(asked("hold")).toBe("reserve");
    expect(asked("call")).toBe("compensate");
    expect(asked("submit")).toBe("irreversible");
    for (const id of ["lookup", "shape", "notify"])
      expect(asked(id), id).toBeNull();
  });
});

describe("what a simulated failure may have changed", () => {
  it("the effect defaults by the platform's rule", () => {
    const { cases } = JSON.parse(
      readFileSync(
        resolve(import.meta.dirname, "fixtures/transaction-effect-cases.json"),
        "utf8",
      ),
    ) as {
      cases: {
        name: string;
        kind: string;
        definiteFailures: string[];
        code: string;
        outcome: "failed" | "unknown" | "not_started";
        effect: string;
      }[];
    };
    expect(cases.length).toBeGreaterThan(5);
    for (const { name, kind, definiteFailures, code, outcome, effect } of cases)
      expect(
        effectDefault(
          code,
          outcome,
          // A called workflow declares no codes.
          kind === "action" ? { spec: { definiteFailures } } : null,
        ),
        name,
      ).toBe(effect);
  });

  it("follows Code and Outcome until the builder picks one, then stays", () => {
    const add = failure("add")!.participant!;
    const shown = (
      code: string,
      outcome: "failed" | "unknown",
      picked: "none" | "unknown" | null = null,
    ) => shownEffect(add, code, outcome, picked);
    expect(shown("VALIDATION", "failed")).toBe("none");
    expect(shown("UNAVAILABLE", "failed")).toBe("unknown");
    expect(shown("VALIDATION", "failed", "unknown")).toBe("unknown");
    expect(shown("UNAVAILABLE", "failed", "none")).toBe("none");
    // Nobody knows what changed: the pick waits for another outcome.
    expect(shown("VALIDATION", "unknown")).toBe("unknown");
    expect(shown("VALIDATION", "unknown", "none")).toBe("unknown");
  });

  it("says why it was chosen, until the builder picks", () => {
    const add = failure("add")!.participant!;
    expect(effectReason(add, "VALIDATION", "failed", null)).toBe(
      "Chosen from the code: target-system.add-quantity says VALIDATION changes nothing.",
    );
    expect(effectReason(add, "REQUEST_REFUSED", "failed", null)).toBe(
      "Chosen from the code: target-system.add-quantity doesn't say REQUEST_REFUSED changes nothing.",
    );
    const call = failure("call")!.participant!;
    expect(effectReason(call, "REQUEST_REFUSED", "failed", null)).toBe(
      "Chosen because a called workflow can't say a failure changed nothing.",
    );
    const hold = failure("hold")!.participant!;
    expect(effectReason(hold, "REQUEST_REFUSED", "failed", null)).toBe(
      "Chosen because Studio can't read which codes of source-system.hold-quantity change nothing.",
    );
    expect(effectReason(add, "VALIDATION", "failed", "none")).toBeNull();
    expect(effectReason(add, "VALIDATION", "unknown", null)).toBeNull();
    expect(effectReason(add, "not a code", "failed", null)).toBeNull();
  });

  it("says what each answer makes of the step, in its own words", () => {
    const words = (id: string, outcome: "failed" | "unknown" = "failed") => {
      const { mode } = failure(id)!.participant!;
      const { may, nothing } = effectChoices(id, mode, outcome);
      return [may, nothing];
    };
    expect(words("add")).toEqual([
      "Weave undoes add with the rest.",
      "Weave skips undoing add.",
    ]);
    expect(words("hold")).toEqual([
      "Weave releases hold with the rest.",
      "Weave skips releasing hold.",
    ]);
    expect(words("submit")).toEqual([
      "submit can't be undone, so the transaction is in doubt.",
      "Weave undoes what's done before it.",
    ]);
    expect(words("add", "unknown")[1]).toBe(
      "Not with an unknown outcome: nobody knows what changed.",
    );
  });
});

describe("what the dialog says first", () => {
  const lead = (
    id: string,
    phase: "undo" | "cancel" | "confirm" | null = null,
    workflow = MOVE,
  ) => failureLead(id, failure(id, workflow), phase);

  it("says what the failure does to the transaction", () => {
    const undoes =
      "Execute workflow fails add with this code, and transaction move undoes what's done. Real runs ignore it.";
    expect(lead("add")).toBe(undoes);
    expect(lead("lookup")).toBe(undoes.replace("add", "lookup"));
    // What it may have changed decides, and a step past the line is never undone.
    for (const id of ["submit", "notify"])
      expect(lead(id)).toBe(
        `Execute workflow fails ${id} with this code. Real runs ignore it.`,
      );
    const setting =
      "Execute step and Execute workflow fail this step with this code; its On error setting decides what happens next. Real runs ignore it.";
    expect(lead("after")).toBe(setting);
    expect(
      lead(
        "lookup",
        null,
        workflowOf([{ ...LOOKUP, onError: "continue" }, ADD]),
      ),
    ).toBe(setting);
  });

  it("says when an undo step fails, how often, and what must happen first", () => {
    expect(lead("hold", "cancel")).toBe(
      "When transaction move is undone, Execute workflow fails hold's release every time Weave tries it. Real runs ignore it.",
    );
    expect(lead("add", "undo")).toBe(
      "When transaction move is undone, Execute workflow fails add's undo every time Weave tries it. Real runs ignore it.",
    );
    expect(lead("hold", "confirm")).toBe(
      "When transaction move succeeds, Execute workflow fails hold's confirm every time Weave tries it. Real runs ignore it.",
    );
    expect(phaseNotes("hold", failure("hold")!, "cancel")).toEqual([
      "Its release fails all 10 tries, the transaction's Max tries, so the transaction ends in doubt.",
      "Something must fail after hold for the transaction to be undone: simulate a failure of a later step too.",
    ]);
    // A confirm runs when the transaction succeeds: nothing has to fail.
    expect(phaseNotes("hold", failure("hold")!, "confirm")).toEqual([
      "Its confirm fails all 10 tries, the transaction's Max tries, so the transaction ends in doubt.",
    ]);
    const once = workflowOf([ADD], {
      policy: { undoRetry: { maxAttempts: 1 } },
    });
    expect(phaseNotes("add", failure("add", once)!, "undo")[0]).toBe(
      "Its undo fails, and Max tries is 1, so the transaction ends in doubt.",
    );
  });

  it("asks for another failure only while none is simulated in the transaction", () => {
    const notes = (errors: Record<string, ErrorEntry>) =>
      phaseNotes("hold", failure("hold", MOVE, errors)!, "cancel").length;
    expect(notes({ submit: { code: "REFUSED" } })).toBe(1);
    expect(notes({ "add[1]": { code: "REFUSED" } })).toBe(1);
    // Its own, an earlier step's (hold never runs), a failure of an undo step, or one outside the transaction.
    expect(
      notes({
        hold: { code: "REFUSED" },
        lookup: { code: "REFUSED" },
        "add#undo": { code: "REFUSED" },
        after: { code: "REFUSED" },
      }),
    ).toBe(2);
  });

  it("knows the steps that run only past a step that can't be undone", () => {
    const move = MOVE.spec.steps[0];
    expect(afterPointOfNoReturn(move, "notify")).toBe(true);
    for (const id of ["lookup", "add", "submit"])
      expect(afterPointOfNoReturn(move, id), id).toBe(false);
    // Reached past it on one path and not on the other; never reached at all.
    const branch = (steps: unknown[]) => ({ steps, output: { literal: null } });
    const [either] = workflowOf([
      {
        id: "choose",
        kind: "switch",
        cases: [{ when: { literal: true }, ...branch([SUBMIT]) }],
        default: branch([SHAPE]),
      },
      LOOKUP,
      { id: "stop", kind: "fail", code: "X", message: { literal: "x" } },
      NOTIFY,
    ]).spec.steps;
    expect(afterPointOfNoReturn(either, "lookup")).toBe(false);
    expect(afterPointOfNoReturn(either, "notify")).toBe(false);
  });

  it("a transaction's error path runs after it, so it isn't inside", () => {
    const guarded = workflowOf([ADD], {
      onError: "errorOutput",
      errorPath: { steps: [LOOKUP], output: { literal: null } },
    });
    expect(transactionHolding(guarded, "add")?.id).toBe("move");
    for (const id of ["lookup", "after", "move", "gone"])
      expect(transactionHolding(guarded, id), id).toBeNull();
  });
});

describe("the banner of a failing undo step", () => {
  it("says whose undo fails, with its code and its tries", () => {
    expect(bannerSentence("undo", "step")).toBe(
      "This step's undo fails when you execute it in Studio. Real runs ignore it.",
    );
    expect(bannerSentence("cancel", "item")).toBe(
      "This item's release fails when you execute it in Studio. Real runs ignore it.",
    );
    expect(bannerSentence("confirm", "every")).toBe(
      "Every item's confirm fails when you execute it in Studio. Real runs ignore it.",
    );
    // The transaction's Max tries now, whatever it was when the failure was saved.
    expect(bannerLine("add", "undo", 10)).toBe(
      "Undo of add · fails all 10 tries",
    );
    expect(bannerLine("hold", "cancel", 1)).toBe(
      "Release of hold · fails once",
    );
  });
});

describe("where a failure of an undo step is written and read", () => {
  it("writes one item's under its own key, and every item's under the step's", () => {
    expect(failureKey("add[2]", false, "undo")).toBe("add[2]#undo");
    expect(failureKey("add[2]", true, "undo")).toBe("add#undo");
    expect(failureKey("hold", false, "cancel")).toBe("hold#cancel");
  });

  it("reads an item's undo from its own key, then every item's, never the step's own failure", () => {
    let data = withFailure(emptyTestData(AT), "add", { code: "OWN" }, AT);
    expect(failureOf(data, "add[2]#undo")).toBeNull();
    data = withFailure(data, "add#undo", { code: "EVERY" }, AT);
    expect(failureOf(data, "add[2]#undo")).toEqual({
      key: "add#undo",
      entry: { code: "EVERY" },
    });
    expect(failureOf(data, "add[2]")?.entry.code).toBe("OWN");
    const pin = { output: {}, source: "manual" } as PinEntry;
    // Its test output at its own key comes first there.
    expect(
      failureOf(withPin(data, "add[2]#undo", pin, AT), "add[2]#undo"),
    ).toBe(null);
    // Its test output is read the same way.
    const pinned = withPin(emptyTestData(AT), "add#undo", pin, AT);
    expect(pinOf(pinned, "add[2]#undo")?.key).toBe("add#undo");
    expect(pinOf(pinned, "add[2]")).toBeNull();
  });

  it("is read as the platform accepts it, and refused otherwise", () => {
    const ok = (errors: unknown) =>
      readTestData({ ...emptyTestData(AT), errors }).ok;
    expect(
      ok({
        add: { code: "VALIDATION", effect: "none" },
        "add[2]#undo": { code: "UNAVAILABLE", attempts: 10 },
        "hold#cancel": { code: "A" },
        "hold#confirm": { code: "A" },
        "support#1.2": { code: "A" },
        submit: { code: "A", outcome: "unknown", effect: "unknown" },
        never: { code: "A", outcome: "not_started", effect: "none" },
      }),
    ).toBe(true);
    for (const entry of [
      { code: "A", effect: "maybe" },
      { code: "A", effect: null },
      { code: "A", outcome: "unknown", effect: "none" },
    ])
      expect(ok({ add: entry }), JSON.stringify(entry)).toBe(false);
    for (const key of [
      "add#compensate",
      "add#undo.1",
      "add#undone",
      "a b#undo",
    ])
      expect(ok({ [key]: { code: "A" } }), key).toBe(false);
  });
});

/** A controller over the transaction, with what the host checked and the toasts. */
function controllerOf(workflow: Workflow = MOVE) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow), "json");
  model.clearHistory();
  const testData = new TestDataService({
    api: {
      request: async (_url: string, _method: string, body?: unknown) => {
        const { pins } = (body as { testData: { pins?: object } }).testData;
        const paths = Object.keys(pins ?? {}).map((key) => `/pins/${key}`);
        return { entries: paths.map((path) => ({ path, ok: true })) };
      },
    } as never,
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
  const controller = new StepDetailsController(
    {
      model,
      draftId: "draft-1",
      perform: (edit: () => void) => edit(),
      notify: (text: string, action?: ToastAction) =>
        void toasts.push({ text, action }),
      undo: () => model.undo(),
      catalogContracts: new Map(Object.entries(CONTRACTS)),
      decisionContracts: new Map(),
      actionVersions: [],
      label: (kind: string) => kind,
      editingLocked: false,
      profile: null,
      testData,
      diagnostics: null,
      diagnosticsDefinition: null,
      cacheDecisionContract: () => undefined,
      can: () => true,
    } as unknown as ControllerHost,
    () => 1000,
  );
  return { controller, testData, toasts };
}

describe("an undo step in step details", () => {
  it("keeps its simulated failure at its own key, and names it when it is removed", async () => {
    const { controller, testData, toasts } = controllerOf();
    await controller.setFailure("add", { code: "VALIDATION", effect: "none" });
    await controller.setFailure("add#undo", {
      code: "UNAVAILABLE",
      attempts: 10,
    });
    expect(testData.data().errors).toEqual({
      add: { code: "VALIDATION", effect: "none", source: "manual" },
      "add#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
    });
    await controller.setFailure("add#undo", null);
    await controller.setFailure("hold[2]#cancel", { code: "TIMEOUT" });
    await controller.setFailure("hold[2]#cancel", null);
    expect(toasts.map(({ text }) => text)).toEqual([
      "Removed the simulated failure of add's undo.",
      "Removed the simulated failure of hold's release for item 3.",
    ]);
    toasts[0].action!.run();
    expect(Object.keys(testData.data().errors!)).toEqual(["add"]);
  });

  it("keeps its test output at its own key, read back for it alone", async () => {
    const { controller, testData, toasts } = controllerOf();
    expect(
      await controller.pin("hold#confirm", { confirmedAt: AT }, "manual"),
    ).toBeNull();
    expect(testData.data().pins).toMatchObject({
      "hold#confirm": { output: { confirmedAt: AT }, source: "manual" },
    });
    expect(controller.phasePin("hold", "confirm")).toMatchObject({
      key: "hold#confirm",
      output: { confirmedAt: AT },
    });
    expect(controller.phasePin("hold", "cancel")).toBeNull();
    // The step itself isn't pinned by it.
    expect(controller.pinOfTarget("hold")).toBeNull();
    await controller.unpin("hold#confirm");
    expect(testData.data().pins).toBeUndefined();
    expect(toasts.map(({ text }) => text)).toEqual([
      "Unpinned hold's confirm.",
    ]);
  });
});

describe("what an answer of the dialog writes", () => {
  it("starts on each part's own failure, and writes the chosen one alone, an undo's without its tries", async () => {
    const { controller } = controllerOf();
    await controller.setFailure("add", { code: "VALIDATION", effect: "none" });
    await controller.setFailure("add#undo", { code: "TIMEOUT", attempts: 3 });
    const open = simulateOpening(controller, "add", "undo")!;
    expect(open).toMatchObject({ step: "add", key: "add", phase: "undo" });
    expect(
      open.current.map(({ phase, code, effect }) => [phase, code, effect]),
    ).toEqual([
      [null, "VALIDATION", "none"],
      ["undo", "TIMEOUT", undefined],
    ]);
    const base = { outcome: "failed", every: false } as const;
    expect(
      failureWritten(
        open,
        { ...base, phase: "undo", code: "UNAVAILABLE" },
        "add",
      ),
    ).toEqual({
      // Execute sends the transaction's Max tries at the time it runs.
      key: "add#undo",
      entry: { code: "UNAVAILABLE" },
      replacing: "add#undo",
    });
    expect(
      failureWritten(
        open,
        {
          ...base,
          phase: null,
          code: "VALIDATION",
          outcome: "unknown",
          effect: "unknown",
        },
        "add",
      ),
    ).toEqual({
      key: "add",
      entry: { code: "VALIDATION", outcome: "unknown", effect: "unknown" },
      replacing: "add",
    });
    // hold has none yet, so nothing is replaced.
    expect(
      failureWritten(
        simulateOpening(controller, "hold")!,
        { ...base, phase: "cancel", code: "TIMEOUT" },
        "hold",
      ),
    ).toMatchObject({ key: "hold#cancel", replacing: null });
  });

  it("reopens an undo's failure saved for every item of a loop as every item's", async () => {
    const looped = workflowOf([
      HOLD,
      {
        id: "each",
        kind: "forEach",
        items: { ref: "/input/items" },
        body: { steps: [ADD], output: { literal: null } },
      },
      SUBMIT,
    ]);
    const { controller } = controllerOf(looped);
    await controller.setFailure("add#undo", { code: "UNAVAILABLE" });
    await controller.setFailure("add[1]#undo", { code: "TIMEOUT" });
    const reopened = (key: string) => {
      controller.pickOpenedItem({ draftId: "draft-1", key }, "add");
      const open = simulateOpening(controller, "add", "undo")!;
      return [open.key, open.current];
    };
    // The third item has no failure of its own: every item's stands for it.
    expect(reopened("add[2]")).toEqual([
      "add[2]",
      [
        {
          phase: "undo",
          key: "add#undo",
          code: "UNAVAILABLE",
          outcome: "failed",
          every: true,
        },
      ],
    ]);
    expect(reopened("add[1]")).toEqual([
      "add[1]",
      [
        {
          phase: "undo",
          key: "add[1]#undo",
          code: "TIMEOUT",
          outcome: "failed",
          every: false,
        },
      ],
    ]);
  });

  it("is dropped when the item picked isn't the one the dialog opened on", () => {
    const open = {
      ...simulateOpening(controllerOf().controller, "add")!,
      key: "add[2]",
    };
    const choice = {
      phase: "undo",
      code: "TIMEOUT",
      outcome: "failed",
      every: false,
    } as const;
    expect(failureWritten(open, choice, "add[3]")).toBeNull();
    expect(failureWritten(open, choice, "add[2]")?.key).toBe("add[2]#undo");
    expect(
      failureWritten(open, { ...choice, every: true }, "add[2]")?.key,
    ).toBe("add#undo");
  });
});

describe("Execute when an undo step needs test output", () => {
  const blocked = (over: Partial<Blocked>): Blocked => ({
    reason: "missing_mock",
    nodeId: "hold",
    instanceKey: "hold#confirm",
    message: "The confirm of hold needs test data.",
    ...over,
  });

  it("names the step, the undo step and what runs it, for every item of a loop", () => {
    expect(phaseBlock(MOVE, blocked({}))).toEqual({
      stepId: "hold",
      phase: "confirm",
      key: "hold#confirm",
      label: "Confirm with",
      kind: "action",
      uses: "source-system.confirm-hold@1.0.0",
      message:
        "hold's Confirm with needs test output: source-system.confirm-hold returns fields Studio can't make up.",
    });
    expect(
      phaseBlock(MOVE, blocked({ instanceKey: "hold[2]#confirm" }))?.key,
    ).toBe("hold#confirm");
  });

  it("keeps the host's own sentence when test output isn't what is missing", () => {
    const misfit =
      "The pinned output of the undo of add doesn't fit its output fields. Edit it, or unpin it.";
    expect(
      phaseBlock(
        MOVE,
        blocked({ nodeId: "add", instanceKey: "add#undo", message: misfit }),
      ),
    ).toMatchObject({ label: "Reverse with", message: misfit });
  });

  it("the run log's header says which undo step", () => {
    const answer = (over: Partial<Blocked>) =>
      ({ status: "blocked", trace: [], blocked: blocked(over) }) as never;
    expect(runStatus(answer({}), null)).toBe(
      "hold's confirm needs test output",
    );
    expect(runStatus(answer({ instanceKey: "" }), null)).toBe(
      "hold needs test data",
    );
  });

  it("is for an undo step of this workflow only", () => {
    expect(phaseBlock(MOVE, blocked({ instanceKey: "" }))).toBeNull();
    expect(phaseBlock(MOVE, blocked({ reason: "path" }))).toBeNull();
    expect(phaseBlock(MOVE, blocked({ frame: "call" }))).toBeNull();
    expect(
      phaseBlock(MOVE, blocked({ nodeId: "add", instanceKey: "add#confirm" })),
    ).toBeNull();
    expect(
      phaseBlock(MOVE, blocked({ nodeId: "gone", instanceKey: "gone#undo" })),
    ).toBeNull();
  });
});

describe("what Studio checks an undo step's test output against", () => {
  it("is what runs the undo step, never the step's own output", () => {
    const { schemaOf } = checkInputsOf(
      MOVE,
      new Map(Object.entries(CONTRACTS)) as never,
    );
    expect(schemaOf("/pins/hold#confirm")).toEqual({
      type: "object",
      required: ["confirmedAt"],
    });
    expect(schemaOf("/pins/hold[2]#confirm")).toEqual(
      schemaOf("/pins/hold#confirm"),
    );
    // What Studio can't read isn't kept (its step's output is no stand-in).
    expect(schemaOf("/pins/add#undo")).toBeUndefined();
    expect(schemaOf("/pins/add")).toEqual({});
  });
});

describe("copying a step that can be undone", () => {
  it("brings its undo's failure set by hand, with its effect, and no test output a run recorded", () => {
    const FROM_RUN = {
      output: { undone: true },
      source: "run",
      runId: "8f0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c11",
      runEnvironmentId: "1b0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c12",
    } as PinEntry;
    const copied = copySteps(
      MOVE,
      ["add"],
      { "add#undo": FROM_RUN },
      {},
      {
        add: { code: "VALIDATION", effect: "none", source: "manual" },
        "add#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
      },
    );
    const plan = planPaste(
      MOVE,
      { ...copied.document, actions: {} },
      { owner: "move/steps", index: 3 },
      new Set(["move", "hold", "add", "submit"]),
    );
    if ("reason" in plan) throw Error(plan.reason);
    expect(plan.pins).toEqual({});
    expect(plan.errors).toEqual({
      "add-1": { code: "VALIDATION", effect: "none", source: "manual" },
      "add-1#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
    });
    // Test data takes them as they came.
    expect(readTestData({ ...emptyTestData(AT), errors: plan.errors }).ok).toBe(
      true,
    );
  });
});
