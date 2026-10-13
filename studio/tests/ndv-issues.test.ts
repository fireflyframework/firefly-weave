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
import {
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import {
  fieldFor,
  firstIssueField,
  fixFor,
  issueCount,
  issueTone,
  leavingText,
  leavingToast,
  targetName,
  type PlacedSpec,
  type StepIssue,
} from "../src/app/editor/ndv/issues";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { newHttpRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { FormulaField } from "../src/app/editor/ndv/params/formula-field";
import { ParamField } from "../src/app/editor/ndv/params/param-field";
import { fixed, mapped } from "../src/app/editor/ndv/params/value-io";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import { outlineOrder } from "../src/app/editor/ndv/navigation";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import {
  openRequest,
  type StepDetailsRequest,
} from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter } from "../src/app/model";
import { lookupAction } from "./browser/support";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

const placed = (
  id: string,
  path: (string | number)[],
  tab: "parameters" | "settings" = "parameters",
  type: ParamSpec["type"] = "text",
): PlacedSpec => ({
  spec: { id, path, type, label: id },
  tab,
});
const specs = [
  placed("connection", ["connection"], "parameters", "connection"),
  placed("input.parameters", ["with", "parameters"], "parameters", "fields"),
  placed("input.parameters.customerId", ["with", "parameters", "customerId"]),
  placed("timeout", ["timeoutSeconds"], "settings", "duration"),
];
const issue = (
  severity: "error" | "warning",
  field: PlacedSpec | null = null,
): StepIssue => ({ key: severity, severity, message: "m", field, fix: null });

describe("issues in step details", () => {
  it("places a problem at the deepest field that holds it", () => {
    expect(fieldFor(["with", "parameters", "customerId"], specs)?.spec.id).toBe(
      "input.parameters.customerId",
    );
    expect(fieldFor(["with", "parameters", "region"], specs)?.spec.id).toBe(
      "input.parameters",
    );
    expect(fieldFor(["timeoutSeconds"], specs)?.tab).toBe("settings");
    expect(fieldFor(["retry"], specs)).toBeNull();
  });
  it("offers the fix Studio knows", () => {
    expect(fixFor("WV-COMP-UNAVAILABLE_REFERENCE", null, specs[2])).toEqual({
      kind: "picker",
      label: "Use data…",
    });
    expect(fixFor("WV-COMP-CONNECTION", null, specs[0])).toEqual({
      kind: "connection",
      label: "Choose a connection",
    });
    expect(fixFor("WV-COMP-UNSUPPORTED_FEATURE", null, null)?.label).toBe(
      "Update the platform",
    );
    expect(fixFor("required", null, specs[2])).toEqual({
      kind: "focus",
      label: "Fix",
    });
    // A field that only takes data opens the data picker.
    expect(
      fixFor(
        "required",
        null,
        placed("file", ["with", "file"], "parameters", "fileRef"),
      ),
    ).toEqual({ kind: "picker", label: "Use data…" });
    expect(
      fixFor(
        "WV-COMP-TYPE_MISMATCH",
        { stepId: "lookup", value: {} },
        specs[2],
      ),
    ).toEqual({
      kind: "suggestion",
      label: "Apply suggested edit",
      change: { stepId: "lookup", value: {} },
    });
    expect(fixFor("WV-COMP-TYPE_MISMATCH", null, specs[2])).toBeNull();
  });
  it("counts issues in words and picks the tone", () => {
    expect(issueCount([issue("warning")])).toBe("1 issue");
    expect(issueCount([issue("warning"), issue("error")])).toBe("2 issues");
    expect(issueTone([issue("warning")])).toBe("warning");
    expect(issueTone([issue("warning"), issue("error")])).toBe("danger");
  });
  it("goes to the first error that has a field, else the first issue with one", () => {
    expect(
      firstIssueField([
        issue("warning", specs[0]),
        issue("error"),
        issue("error", specs[3]),
      ])?.spec.id,
    ).toBe("timeout");
    expect(
      firstIssueField([issue("error"), issue("warning", specs[0])])?.spec.id,
    ).toBe("connection");
    expect(firstIssueField([issue("error")])).toBeNull();
  });
  it("says what is kept when step details close, naming the other steps", () => {
    expect(leavingText([])).toBe("");
    expect(leavingText([{ label: "Max tries" }])).toBe(
      "Not applied yet: Max tries. What you typed is kept until you finish it.",
    );
    expect(
      leavingText([
        { label: "Max tries" },
        { label: "Wait for", step: "wait-a-minute" },
      ]),
    ).toBe(
      "Not applied yet: Max tries, Wait for in wait-a-minute. What you typed is kept until you finish them.",
    );
  });
  it("names the trigger, End and workflow settings in words", () => {
    expect(targetName("$trigger")).toBe("Manual form trigger");
    expect(targetName("$end")).toBe("End");
    expect(targetName("$workflow")).toBe("workflow settings");
    expect(targetName("check-customer")).toBe("check-customer");
  });
  it("names where the entries are when leaving for another step", () => {
    // Leaving workflow settings for a step's details: its own entries are
    // named with it, so they never read as fields of the step that opens.
    expect(
      leavingToast(
        [
          { label: "Stop runs after" },
          { label: "Wait for", step: "wait-a-minute" },
        ],
        "$workflow",
      ),
    ).toBe(
      "Not applied yet: Stop runs after in workflow settings, Wait for in wait-a-minute. What you typed is kept until you finish them.",
    );
    // Closing names the open target's entries alone, as before.
    expect(leavingToast([{ label: "Stop runs after" }])).toBe(
      "Not applied yet: Stop runs after. What you typed is kept until you finish it.",
    );
  });
  it("names three steps at most, each once, and counts the rest", () => {
    expect(
      leavingText([
        { label: "Wait at most" },
        { label: "Path result", step: "route-by-value" },
        { label: "Due after", step: "notify-sales" },
        { label: "Run at most", step: "fan-out" },
        { label: "Wait for", step: "wait-a-minute" },
      ]),
    ).toBe(
      "Not applied yet: Wait at most, Path result in route-by-value, Due after in notify-sales and 2 more steps. What you typed is kept until you finish them.",
    );
    expect(
      leavingText([
        { label: "Prompt", step: "summarize" },
        { label: "Model", step: "summarize" },
        { label: "Wait for", step: "wait-a-minute" },
        { label: "Run at most", step: "fan-out" },
        { label: "Due after", step: "notify-sales" },
      ]),
    ).toBe(
      "Not applied yet: Prompt and Model in summarize, Wait for in wait-a-minute, Run at most in fan-out and 1 more step. What you typed is kept until you finish them.",
    );
    // A step holding many entries is counted, not listed.
    expect(
      leavingText([
        { label: "Max tries" },
        { label: "Timeout" },
        { label: "Response sample" },
      ]),
    ).toBe(
      "Not applied yet: Max tries and 2 more fields. What you typed is kept until you finish them.",
    );
  });
});

// ----------------------------------------------------------- the session

const USES = "untitled-workflow.get@1.0.0";
type MutableHost = {
  -readonly [K in keyof StepDetailsHost]: StepDetailsHost[K];
};
function harness(
  recipe: Record<string, unknown> = {},
  extra: { lookupContract?: unknown; lookupWith?: unknown } = {},
) {
  const model = new StructuredCanvasAdapter();
  model.insert("action", "root", undefined, {
    id: "get",
    uses: USES,
    connection: "api",
    with: { literal: {} },
  });
  model.insert("action", "root", undefined, {
    id: "lookup",
    uses: "sql.lookup@1.0.0",
    connection: "db",
    with: extra.lookupWith ?? { literal: {} },
  });
  model.canvas = withOwnedAction(model.canvas, USES, {
    ...newHttpRecipe(),
    pathTemplate: "/orders",
    ...recipe,
  } as never);
  model.clearHistory();
  const notes: string[] = [];
  const host = {
    model,
    editingLocked: false,
    profile: null,
    stepDataScope: "account-a",
    catalogContracts: new Map(
      extra.lookupContract
        ? [["sql.lookup@1.0.0", extra.lookupContract as never]]
        : [],
    ),
    decisionContracts: new Map(),
    actionVersions: [],
    diagnostics: null,
    diagnosticsDefinition: null,
    contractIssues: [],
    label: (kind: string) => kind,
    can: () => true,
    notify: (text: string) => notes.push(text),
    refreshView: () => undefined,
    perform: (edit: () => void) => edit(),
    api: { request: async () => ({ features: [] }) },
  } as unknown as MutableHost;
  let clock = 1000;
  const controller = new StepDetailsController(host, () => (clock += 700));
  const open = (
    target = "get",
    request: Partial<StepDetailsRequest> = {},
  ): FormSession =>
    new FormSession(host, controller, openRequest(target, request), {
      confirm: async () => true,
      announce: () => undefined,
    });
  return { model, host, controller, open, notes };
}
const settingsSpec = (session: FormSession, id: string): ParamSpec => {
  const found = session.placedSpecs().find((p) => p.spec.id === id);
  if (!found) throw new Error(`No field ${id}`);
  return found.spec;
};
const issueOf = (session: FormSession, id: string) =>
  session.issues().filter((item) => item.field?.spec.id === id);

describe("issues of a step", () => {
  it("lists saved out-of-range settings as field issues on the Settings tab", () => {
    const h = harness({
      timeoutSeconds: 31,
      retry: { maxAttempts: 3, initialDelaySeconds: 0, maxDelaySeconds: 30 },
    });
    const session = h.open();
    const timeout = issueOf(session, "timeout");
    expect(timeout).toHaveLength(1);
    expect(timeout[0]).toMatchObject({
      severity: "error",
      message: "Use a number from 1 to 30.",
      field: { tab: "settings" },
      fix: { kind: "focus", label: "Fix" },
    });
    expect(issueOf(session, "retry.initialDelaySeconds")[0]).toMatchObject({
      message: "Use 1 or more.",
      field: { tab: "settings" },
    });
    // Zero is no timeout either.
    session.write(settingsSpec(session, "timeout"), fixed(0));
    expect(issueOf(session, "timeout")[0].message).toBe(
      "Enter a whole number greater than 0.",
    );
  });
  it("leaves out the tries of a retry that is off", () => {
    const h = harness({
      retry: { maxAttempts: 1, initialDelaySeconds: 2, maxDelaySeconds: 30 },
    });
    expect(
      h
        .open()
        .issues()
        .filter((item) => item.field?.spec.id.startsWith("retry")),
    ).toEqual([]);
  });
  it("shows a read-only field's reason instead of asking for a value", () => {
    const h = harness();
    const session = h.open("lookup");
    const timeout = settingsSpec(session, "timeout");
    const entry = {
      spec: timeout,
      option: false,
      readOnly: "Set by sql.lookup@1.0.0.",
      disabled: null,
    };
    expect(session.line(timeout, entry)).toEqual({
      kind: "reason",
      text: "Set by sql.lookup@1.0.0.",
    });
    expect(issueOf(session, "timeout")).toEqual([]);
  });
  it("places a compiler message at its field with the diagnostic's words", () => {
    const h = harness();
    h.host.diagnostics = {
      validationOk: false,
      errorCount: 1,
      partial: true,
      diagnostics: [
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "error",
          message: "pathTemplate is a string",
          path: "/spec/steps/0/connection",
        },
      ],
    } as never;
    h.host.diagnosticsDefinition = h.model.definition;
    const session = h.open();
    const found = issueOf(session, "connection");
    expect(found).toHaveLength(1);
    expect(found[0].message).toBe(
      "This value's type doesn't match what the field expects.",
    );
    expect(found[0].fix).toEqual({
      kind: "connection",
      label: "Choose a connection",
    });
  });
  it("leaves the platform check's notes out of a step's issues, as the canvas does", () => {
    const h = harness();
    h.host.diagnostics = {
      validationOk: true,
      errorCount: 0,
      partial: false,
      diagnostics: [
        {
          code: "WV-STUDIO-OWNED_UNPUBLISHED",
          severity: "info",
          message:
            "This step's action is published with the workflow: Publish… and Simulate publish it first.",
          path: "/spec/steps/0/uses",
        },
      ],
    } as never;
    h.host.diagnosticsDefinition = h.model.definition;
    expect(h.open().issues()).toEqual([]);
  });
  it("keeps required messages for later on a new step, until the field is left or Execute step is pressed", () => {
    const h = harness({ pathTemplate: "" });
    const fresh = h.open("get", { fresh: true });
    expect(issueOf(fresh, "url")).toEqual([]);
    fresh.touch(settingsSpec(fresh, "url"));
    expect(issueOf(fresh, "url")[0].message).toBe("Enter a value.");
    const other = harness({ pathTemplate: "" });
    const later = other.open("get", { fresh: true });
    expect(later.issues()).toEqual([]);
    other.controller.reveal("get");
    expect(issueOf(later, "url")[0].message).toBe("Enter a value.");
  });
  it("puts a problem the action builder found on its field, once", () => {
    const h = harness({ timeoutSeconds: 31 });
    h.controller.ownedProblems = () =>
      new Map([
        [
          USES,
          [
            {
              code: "WV-HTTP-ACTION-REQUEST",
              severity: "error",
              message: "The HTTP action request has an out-of-range field.",
              path: "/timeoutSeconds",
            },
            {
              code: "WV-STUDIO-ACTION-BUILD",
              severity: "error",
              message:
                "Couldn't build the API action. Check the URL and try again.",
              path: "/pathTemplate",
            },
          ],
        ],
      ]) as never;
    const session = h.open();
    expect(issueOf(session, "timeout").map((i) => i.message)).toEqual([
      "Use a number from 1 to 30.",
    ]);
    expect(issueOf(session, "url").map((i) => i.message)).toEqual([
      "Couldn't build the API action. Check the URL and try again.",
    ]);
  });
});

describe("issues of a step, as pinned", () => {
  it("lists fields in the order they show: Parameters before Settings", () => {
    const h = harness({ timeoutSeconds: 31 });
    h.host.diagnostics = {
      validationOk: false,
      errorCount: 1,
      partial: true,
      diagnostics: [
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "error",
          message: "connection",
          path: "/spec/steps/0/connection",
        },
      ],
    } as never;
    h.host.diagnosticsDefinition = h.model.definition;
    // The Settings rule is found first; the Parameters message shows first.
    expect(
      h
        .open()
        .issues()
        .map((issue) => `${issue.field?.tab}:${issue.field?.spec.id}`),
    ).toEqual(["parameters:connection", "settings:timeout"]);
  });
  it("keeps a missing required input for later on a new step, like the field's own message", () => {
    const lookupWith = {
      object: {
        parameters: { object: { customerId: { ref: "/input/customerId" } } },
      },
    };
    const gap = {
      stepId: "lookup",
      field: "with" as const,
      dataPath: ["parameters", "customerId"],
      label: "Customer ID",
    };
    const fresh = harness({}, { lookupContract: lookupAction, lookupWith });
    fresh.host.contractIssues = [gap];
    const session = fresh.open("lookup", { fresh: true });
    const id = "input.parameters.customerId";
    expect(issueOf(session, id)).toEqual([]);
    session.touch(settingsSpec(session, id));
    expect(issueOf(session, id).map((issue) => issue.message)).toEqual([
      "Enter a value or map one.",
    ]);
    const later = harness({}, { lookupContract: lookupAction, lookupWith });
    later.host.contractIssues = [gap];
    expect(issueOf(later.open("lookup"), id)).toHaveLength(1);
  });
  it("shows why a read-only field can't change instead of any rule message", () => {
    const h = harness(
      {},
      {
        lookupContract: {
          ...lookupAction,
          spec: { ...lookupAction.spec, timeoutSeconds: 0 },
        },
      },
    );
    const session = h.open("lookup");
    const timeout = settingsSpec(session, "timeout");
    expect(
      session.line(timeout, {
        spec: timeout,
        option: false,
        readOnly: "Set by sql.lookup@1.0.0.",
        disabled: null,
      }),
    ).toEqual({ kind: "reason", text: "Set by sql.lookup@1.0.0." });
    expect(issueOf(session, "timeout")).toEqual([]);
  });
});

// ------------------------------------------------- entries not applied yet

describe("entries not applied yet", () => {
  it("keeps a held entry for the same field when step details open again", () => {
    const h = harness({
      retry: { maxAttempts: 5, initialDelaySeconds: 2, maxDelaySeconds: 30 },
    });
    const first = h.open("get", { tab: "settings" });
    const tries = settingsSpec(first, "retry.maxAttempts");
    first.hold(tries, "number", "1", "Use a number from 2 to 10.");
    const again = h.open("get", { tab: "settings" });
    expect(again.held(settingsSpec(again, "retry.maxAttempts"))).toMatchObject({
      text: "1",
      message: "Use a number from 2 to 10.",
    });
    const listed = issueOf(again, "retry.maxAttempts");
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({
      status: "Not applied",
      message: "Use a number from 2 to 10.",
      field: { tab: "settings" },
    });
    expect(again.heldLabels()).toEqual(["Max tries"]);
    // What the workflow keeps is the last value that applied.
    expect(ownedActionsOf(h.model.canvas)[USES].retry?.maxAttempts).toBe(5);
  });
  it("keeps it through an edit of another field, and drops it when its own value changes", () => {
    const h = harness({
      retry: { maxAttempts: 5, initialDelaySeconds: 2, maxDelaySeconds: 30 },
    });
    const session = h.open();
    const tries = settingsSpec(session, "retry.maxAttempts");
    session.hold(tries, "number", "1", "Use a number from 2 to 10.");
    session.write(settingsSpec(session, "timeout"), fixed(20));
    expect(session.held(tries)?.text).toBe("1");
    session.write(tries, fixed(7));
    expect(session.held(tries)).toBeNull();
    session.hold(tries, "number", "1", "Use a number from 2 to 10.");
    h.model.undo();
    expect(h.open().held(tries)).toBeNull();
  });
  for (const change of [
    "another workflow",
    "the workflow reopened",
    "another account",
    "another platform",
    "another connection to it",
  ] as const)
    it(`forgets held entries for ${change}`, () => {
      const h = harness({ responseSample: { id: 1 } });
      const session = h.open();
      const sample = settingsSpec(session, "responseSample");
      session.hold(sample, "json", "{", "This isn't valid JSON.");
      expect(session.held(sample)?.text).toBe("{");
      if (change === "another workflow") {
        const other = harness({ responseSample: { id: 1 } });
        h.host.model = other.model;
      }
      if (change === "the workflow reopened") h.model.clearHistory();
      if (change === "another account") h.host.stepDataScope = "account-b";
      if (change === "another platform")
        h.host.profile = { id: "remote" } as never;
      if (change === "another connection to it")
        h.host.api = { request: async () => ({}) } as never;
      const next = h.open();
      expect(next.issues().filter((i) => i.status)).toEqual([]);
      expect(next.held(sample)).toBeNull();
    });
  it("never gives a held entry to a step made again with the same name", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    session.hold(sample, "json", "{", "This isn't valid JSON.");
    h.model.remove("get");
    h.controller.prunePending();
    h.model.insert("action", "root", undefined, {
      id: "get",
      uses: USES,
      with: { literal: {} },
    });
    expect(h.open().held(sample)).toBeNull();
  });
  it("keeps the open step's entries while the dialog follows a rename back", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    session.hold(sample, "json", "{", "This isn't valid JSON.");
    h.model.remove("get");
    h.controller.prunePending("get");
    h.model.undo();
    expect(h.open().held(sample)?.text).toBe("{");
  });
  it("moves held entries with a rename made in step details", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    session.hold(sample, "json", "{", "This isn't valid JSON.");
    h.model.renameStep("get", "fetch");
    h.controller.renamePending("get", "fetch");
    h.controller.prunePending();
    expect(h.open("fetch").held(sample)?.text).toBe("{");
    expect(h.open("get").held(sample)).toBeNull();
  });
  it("never gives a row's entry to the row that takes its place", () => {
    const h = harness({ statuses: [200, 201, 204] });
    const session = h.open();
    const list = settingsSpec(session, "statuses");
    const second = session
      .placedSpecs()
      .find((p) => p.spec.id === "status" && p.spec.path[1] === 1)!.spec;
    session.hold(second, "number", "abc", "Enter a number.");
    expect(session.held(second)?.text).toBe("abc");
    session.listRemove(list, 0);
    const after = h.open();
    const moved = after
      .placedSpecs()
      .find((p) => p.spec.id === "status" && p.spec.path[1] === 1)!.spec;
    expect(after.held(moved)).toBeNull();
  });
  it("hides held entries while the workflow is read-only, and shows them again after", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    session.hold(sample, "json", "{", "This isn't valid JSON.");
    h.host.editingLocked = true;
    expect(session.held(sample)).toBeNull();
    expect(
      h
        .open()
        .issues()
        .filter((issue) => issue.status),
    ).toEqual([]);
    h.host.editingLocked = false;
    expect(h.open().held(sample)?.text).toBe("{");
  });
  it("refuses to hold an entry while the workflow is read-only", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    h.host.editingLocked = true;
    session.hold(sample, "json", "{", "This isn't valid JSON.");
    h.host.editingLocked = false;
    expect(h.open().held(sample)).toBeNull();
  });
  it("tells a row's entry from an equal row's by the rows around it", () => {
    const rowOf = (session: FormSession, at: number) =>
      session
        .placedSpecs()
        .find((p) => p.spec.id === "status" && p.spec.path[1] === at)!.spec;
    // Removed: the equal row now at its place doesn't get it.
    const removed = harness({ statuses: [201, 201, 201] });
    const first = removed.open();
    first.hold(rowOf(first, 1), "number", "abc", "Enter a number.");
    first.listRemove(settingsSpec(first, "statuses"), 0);
    const left = removed.open();
    expect(left.held(rowOf(left, 1))).toBeNull();
    // Moved: same values, same count, and still another row.
    const moved = harness({ statuses: [201, 201] });
    const second = moved.open();
    second.hold(rowOf(second, 1), "number", "abc", "Enter a number.");
    second.listMove(settingsSpec(second, "statuses"), 0, 1);
    const after = moved.open();
    expect(ownedActionsOf(moved.model.canvas)[USES].statuses).toEqual([
      201, 201,
    ]);
    expect(after.held(rowOf(after, 1))).toBeNull();
  });
  it("names the entries other steps still hold, once checked against their fields", () => {
    const h = harness({ responseSample: { id: 1 } });
    const get = h.open();
    const sample = settingsSpec(get, "responseSample");
    get.hold(sample, "json", "{", "This isn't valid JSON.");
    expect(h.open("lookup").heldInOtherSteps()).toEqual([
      { label: "Response sample", step: "get" },
    ]);
    expect(h.open("get").heldInOtherSteps()).toEqual([]);
    // Its value changed since: the entry no longer belongs to it.
    get.write(sample, fixed({ id: 2 }));
    expect(h.open("lookup").heldInOtherSteps()).toEqual([]);
  });
  const limitHarness = () =>
    harness(
      {},
      {
        lookupContract: lookupAction,
        lookupWith: {
          object: {
            parameters: {
              object: { customerId: { ref: "/input/customerId" } },
            },
            limit: { literal: 5 },
          },
        },
      },
    );
  it("keeps a held number out of sight while its field is Mapped, and shows it again on Fixed", async () => {
    const h = limitHarness();
    const session = h.open("lookup");
    const limit = settingsSpec(session, "input.limit");
    session.hold(limit, "number", "abc", "Enter a number.");
    expect(session.held(limit)?.text).toBe("abc");
    await session.setMode(limit, "mapped");
    expect(session.mode(limit)).toBe("mapped");
    expect(session.held(limit)).toBeNull();
    expect(session.issues().filter((issue) => issue.status)).toEqual([]);
    await session.setMode(limit, "fixed");
    expect(session.mode(limit)).toBe("fixed");
    expect(session.held(limit)).toMatchObject({
      text: "abc",
      message: "Enter a number.",
    });
    expect(session.heldLabels()).toEqual(["Limit"]);
  });
  it("drops a held number once a mapping is written over it", async () => {
    const h = limitHarness();
    const session = h.open("lookup");
    const limit = settingsSpec(session, "input.limit");
    session.hold(limit, "number", "abc", "Enter a number.");
    await session.setMode(limit, "mapped");
    expect(session.write(limit, mapped({ ref: "/input/amount" }))).toBe(true);
    // Undo puts the old number back; what was typed over it never applied.
    h.model.undo();
    expect(session.read(limit)).toEqual(fixed(5));
    await session.setMode(limit, "fixed");
    expect(session.held(limit)).toBeNull();
    expect(h.open("lookup").held(limit)).toBeNull();
  });
  it("names a number kept out of sight under Mapped when step details close", async () => {
    const h = limitHarness();
    const session = h.open("lookup");
    const limit = settingsSpec(session, "input.limit");
    session.hold(limit, "number", "abc", "Enter a number.");
    await session.setMode(limit, "mapped");
    // Closing forgets the switch: the field opens Fixed with the entry again.
    expect(session.heldOnLeaving()).toEqual([{ label: "Limit" }]);
  });
  it("lists other steps' entries in workflow order when step details close", () => {
    const h = harness({ responseSample: { id: 1 } });
    h.model.insert("action", "root", 0, {
      id: "first",
      uses: "untitled-workflow.first@1.0.0",
      with: { literal: {} },
    });
    const owned = "untitled-workflow.first@1.0.0";
    h.model.canvas = withOwnedAction(h.model.canvas, owned, {
      ...newHttpRecipe(),
      pathTemplate: "/first",
      responseSample: { id: 1 },
    } as never);
    const order = outlineOrder(h.model.definition);
    expect(order.indexOf("first")).toBeLessThan(order.indexOf("get"));
    // Held last-step first: the toast still follows the workflow.
    const get = h.open("get");
    get.hold(settingsSpec(get, "responseSample"), "json", "{", "Bad JSON.");
    const first = h.open("first");
    first.hold(settingsSpec(first, "responseSample"), "json", "[", "Bad JSON.");
    expect(h.open("lookup").heldOnLeaving()).toEqual([
      { label: "Response sample", step: "first" },
      { label: "Response sample", step: "get" },
    ]);
    expect(h.open("get").heldOnLeaving()).toEqual([
      { label: "Response sample" },
      { label: "Response sample", step: "first" },
    ]);
  });
  it("drops a held entry when its field is described differently", () => {
    const h = harness({ responseSample: { id: 1 } });
    const session = h.open();
    const sample = settingsSpec(session, "responseSample");
    // Typed while the field was described another way.
    session.hold(
      { ...sample, label: "Example answer" },
      "json",
      "{",
      "This isn't valid JSON.",
    );
    expect(session.held(sample)).toBeNull();
    expect(h.controller.heldTargets()).toEqual(["get"]);
    session.pruneHeld();
    expect(h.controller.heldTargets()).toEqual([]);
  });
});

describe("unapplied template text", () => {
  async function templateHarness() {
    const model = new StructuredCanvasAdapter();
    model.insert("llm", "root", undefined, {
      id: "summarize",
      profile: "assistant",
      prompt: {
        op: {
          name: "concat",
          args: [{ literal: "Hello " }, { ref: "/input/name" }],
        },
      },
    });
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
      contractIssues: [],
      label: (kind: string) => kind,
      can: () => true,
      notify: () => undefined,
      refreshView: () => undefined,
      perform: (edit: () => void) => edit(),
      api: { request: async () => ({ features: ["text.concat"] }) },
    } as unknown as MutableHost;
    const controller = new StepDetailsController(host);
    await controller.loadFeatures();
    const open = () =>
      new FormSession(host, controller, openRequest("summarize"), {
        confirm: async () => true,
        announce: () => undefined,
      });
    const field = (session: FormSession) => {
      const injector = Injector.create({ providers: [] });
      const component = runInInjectionContext(
        injector,
        () => new FormulaField(),
      );
      const spec = session
        .state("parameters")
        .fields.find((entry) => entry.spec.id === "prompt")!.spec;
      component.session = signal(session) as never;
      component.spec = signal(spec) as never;
      component.controlId = signal("prompt-control") as never;
      return { component, spec, injector };
    };
    return { model, host, controller, open, field };
  }
  const type = (field: FormulaField, value: string) => field.typed(value);
  it("keeps the text across close and reopen, Not applied, with the last valid value saved", async () => {
    const t = await templateHarness();
    const before = JSON.stringify(t.model.definition);
    const first = t.field(t.open());
    first.component.beginEdit();
    type(first.component, "Dear {{ input.name");
    first.component.leave();
    expect(JSON.stringify(t.model.definition)).toBe(before);
    first.injector.destroy();
    const session = t.open();
    const again = t.field(session);
    expect(session.mode(again.spec)).toBe("mapped");
    expect(again.component.view()).toBe("editor");
    expect(again.component.templateMode()).toBe(true);
    expect(again.component.shownText()).toBe("Dear {{ input.name");
    expect(again.component.templateError()).toBe(
      "The formula ends too early. Close the placeholder with `}}`.",
    );
    expect(session.issues()[0]).toMatchObject({
      status: "Not applied",
      field: { spec: { id: "prompt" } },
    });
    // Finishing it applies it and the entry goes.
    type(again.component, "Dear {{ input.name }}");
    expect(again.component.templateError()).toBe("");
    expect(session.held(again.spec)).toBeNull();
    expect(JSON.stringify(t.model.definition)).toContain("Dear ");
  });
  it("Escape in the field restores the last valid value and discards the text", async () => {
    const t = await templateHarness();
    const first = t.field(t.open());
    first.component.beginEdit();
    type(first.component, "Dear {{ input.name");
    const escape = {
      event: { key: "Escape" } as KeyboardEvent,
      handled: false,
    };
    first.component.escape(escape);
    expect(escape.handled).toBe(true);
    expect(first.component.shownText()).toBe("Hello {{ input.name }}");
    first.component.leave();
    expect(t.open().held(first.spec)).toBeNull();
    // Nothing held, Escape is left to step details.
    const again = {
      event: { key: "Escape" } as KeyboardEvent,
      handled: false,
    };
    first.component.escape(again);
    expect(again.handled).toBe(false);
  });
  it("lets go of held template text when a Mapped draft goes back to Fixed", async () => {
    const t = await templateHarness();
    const session = t.open();
    const prompt = session
      .state("parameters")
      .fields.find((entry) => entry.spec.id === "prompt")!.spec;
    // A literal prompt, switched to Mapped, with text that doesn't parse.
    t.model.update(
      "summarize",
      JSON.stringify({
        ...t.model.nodes()[0].step,
        prompt: { literal: "Hello" },
      }),
    );
    await session.setMode(prompt, "mapped");
    session.hold(prompt, "template", "Dear {{ input.name", "Close the braces.");
    expect(session.held(prompt)?.text).toBe("Dear {{ input.name");
    await session.setMode(prompt, "fixed");
    expect(session.mode(prompt)).toBe("fixed");
    expect(session.held(prompt)).toBeNull();
    expect(t.open().held(prompt)).toBeNull();
    expect(session.read(prompt)).toEqual(fixed("Hello"));
  });
  it("never applies held text while the workflow is read-only", async () => {
    const t = await templateHarness();
    const first = t.field(t.open());
    first.component.beginEdit();
    type(first.component, "Dear {{ input.name");
    t.host.editingLocked = true;
    const before = JSON.stringify(t.model.definition);
    first.component.leave();
    expect(JSON.stringify(t.model.definition)).toBe(before);
    t.host.editingLocked = false;
    expect(t.open().held(first.spec)?.text).toBe("Dear {{ input.name");
  });
});

describe("number and JSON entries not applied yet", () => {
  it("shows a held number again in a new field component", () => {
    const h = harness({
      retry: { maxAttempts: 5, initialDelaySeconds: 2, maxDelaySeconds: 30 },
    });
    const make = (session: FormSession) => {
      const injector = Injector.create({
        providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
      });
      const field = runInInjectionContext(injector, () => new ParamField());
      const spec = settingsSpec(session, "retry.maxAttempts");
      field.session = signal(session) as never;
      field.spec = signal(spec) as never;
      field.entry = signal({
        spec,
        option: false,
        readOnly: null,
        disabled: null,
      }) as never;
      return field;
    };
    const first = make(h.open());
    first.number({ target: { value: "1" } } as unknown as Event);
    expect(first.numberProblem()).toBe("Use a number from 2 to 10.");
    const again = make(h.open());
    expect(again.numberText).toBe("1");
    expect(again.numberProblem()).toBe("Use a number from 2 to 10.");
    again.number({ target: { value: "8" } } as unknown as Event);
    expect(again.numberText).toBeNull();
    expect(ownedActionsOf(h.model.canvas)[USES].retry?.maxAttempts).toBe(8);
  });
  it("shows JSON that doesn't parse again in a new field component", () => {
    const h = harness({ responseSample: { id: 1 } });
    const make = (session: FormSession) => {
      const injector = Injector.create({
        providers: [{ provide: ElementRef, useValue: new ElementRef({}) }],
      });
      const field = runInInjectionContext(injector, () => new ParamField());
      const spec = settingsSpec(session, "responseSample");
      field.session = signal(session) as never;
      field.spec = signal(spec) as never;
      field.entry = signal({
        spec,
        option: true,
        readOnly: null,
        disabled: null,
      }) as never;
      return field;
    };
    const first = make(h.open());
    first.jsonText({ target: { value: '{"id": ' } } as unknown as Event);
    expect(first.jsonProblem).toMatch(/^This isn't valid JSON/);
    const again = make(h.open());
    expect(again.jsonShown()).toBe('{"id": ');
    expect(again.jsonProblem).toMatch(/^This isn't valid JSON/);
    again.jsonText({ target: { value: '{"id": 2}' } } as unknown as Event);
    expect(again.jsonProblem).toBe("");
    expect(ownedActionsOf(h.model.canvas)[USES].responseSample).toEqual({
      id: 2,
    });
  });
});

describe("entries not applied yet, on leaving and on the canvas", () => {
  const rowOf = (session: FormSession, at: number) =>
    session
      .placedSpecs()
      .find((p) => p.spec.id === "status" && p.spec.path[1] === at)!.spec;
  it("says a note that isn't saved is kept until it fits, apart from entries not applied", () => {
    expect(leavingText([{ label: "Notes", status: "Not saved" }])).toBe(
      "Not saved yet: Notes. It's kept until it fits.",
    );
    expect(
      leavingText([
        { label: "Max tries" },
        { label: "Notes", status: "Not saved" },
        { label: "Wait for", step: "wait-a-minute" },
        { label: "Notes", step: "wait-a-minute", status: "Not saved" },
      ]),
    ).toBe(
      "Not applied yet: Max tries, Wait for in wait-a-minute. What you typed is kept until you finish them. Not saved yet: Notes, Notes in wait-a-minute. They're kept until they fit.",
    );
  });
  it("names a list's row by the list and the row", () => {
    const h = harness({ statuses: [200, 201, 204] });
    const session = h.open();
    session.hold(rowOf(session, 1), "number", "abc", "Enter a number.");
    expect(session.heldOnLeaving()).toEqual([
      { label: "Accepted status codes (row 2)" },
    ]);
    expect(h.open("lookup").heldInOtherSteps()).toEqual([
      { label: "Accepted status codes (row 2)", step: "get" },
    ]);
  });
  it("reads an entry that no longer belongs to its value without dropping it, and drops it when checked", () => {
    const h = harness({
      retry: { maxAttempts: 5, initialDelaySeconds: 2, maxDelaySeconds: 30 },
    });
    const session = h.open();
    const tries = settingsSpec(session, "retry.maxAttempts");
    session.hold(tries, "number", "1", "Use a number from 2 to 10.");
    session.write(tries, fixed(7));
    // Reading, as a render does, changes nothing.
    expect(session.held(tries)).toBeNull();
    expect(
      h
        .open()
        .issues()
        .filter((i) => i.status),
    ).toEqual([]);
    expect(h.controller.heldTargets()).toEqual(["get"]);
    // The check after a change drops it, and lists what is left per step.
    expect(FormSession.heldByStep(h.host, h.controller)).toEqual(new Map());
    expect(h.controller.heldTargets()).toEqual([]);
  });
  it("lists each step's entries for its tile, checked against their fields", () => {
    const h = harness({ statuses: [200, 201, 204], responseSample: { id: 1 } });
    const session = h.open();
    session.hold(rowOf(session, 1), "number", "abc", "Enter a number.");
    session.hold(
      settingsSpec(session, "responseSample"),
      "json",
      "{",
      "This isn't valid JSON.",
    );
    const held = FormSession.heldByStep(h.host, h.controller);
    expect([...held.keys()]).toEqual(["get"]);
    expect(
      held.get("get")!.map(({ label, status }) => [label, status]),
    ).toEqual([
      ["Response sample", "Not applied"],
      ["Accepted status codes (row 2)", "Not applied"],
    ]);
    // Each names the issue it is, for step details to open on.
    const keys = h
      .open()
      .issues()
      .filter((i) => i.status)
      .map((i) => i.key);
    expect(held.get("get")!.map((fact) => fact.key)).toEqual(keys);
  });
});
