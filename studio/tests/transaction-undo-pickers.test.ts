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
// What may undo a step: the project's published actions that are safe to
// retry and its workflows with a time limit, each with the reason it can't be
// chosen; what choosing one writes, and the input rows it shows. The catalog
// is the one Python compiles the transaction tests against.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { parse } from "yaml";
import { withEmptyTransactions } from "../src/app/designer/diagnostic-copy";
import { describeDiagnostic } from "../src/app/designer/diagnostic-copy";
import { locateDiagnostic } from "../src/app/designer/diagnostic-location";
import { notCallable } from "../src/app/editor/calls/callable-catalog";
import { callSources } from "../src/app/editor/calls/call-sources";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import {
  fixed,
  mapped,
  readParam,
  writeParam,
  type ParamValue,
} from "../src/app/editor/ndv/params/value-io";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { undoPickerReason } from "../src/app/editor/transactions/undo";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  type Step,
} from "../src/app/model";
import { repository } from "./python-path";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

interface Document {
  kind: string;
  metadata: { name: string; version: string };
  spec: Record<string, unknown>;
}
const { definitions } = parse(
  readFileSync(
    join(repository, "tests/fixtures/transactions/catalog.yaml"),
    "utf8",
  ),
) as { definitions: Document[] };
/** A workflow the steps of move-between-systems may call. */
const reverses = (
  name: string,
  spec: Record<string, unknown> = {},
  version = "1.0.0",
): Document => ({
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    callable: {},
    inputSchema: {
      type: "object",
      properties: { reference: { type: "string" } },
      required: ["reference"],
    },
    outputSchema: {},
    ...spec,
  },
});
const CATALOG = {
  definitions: [
    ...definitions.filter(({ kind }) => kind === "Action"),
    reverses("reverse-target-addition", { timeoutSeconds: 300 }),
    reverses("reverse-without-limit"),
    reverses("reverse-for-others", {
      callable: { allowedCallers: ["order-intake"] },
    }),
  ].map((document) => ({ document, digest: "0".repeat(64) })),
  schemas: {},
};
const RETRY =
  "Weave retries undo steps until they succeed, so this action must be safe to retry.";
const READS =
  "This action only reads data, so it can't confirm, release or reverse anything.";
const LIMIT = "Called workflows inside a transaction need a time limit.";
const MANIFEST = {
  features: ["flow.transaction", "flow.onError", "flow.callWorkflow"],
  step_fields: [
    {
      name: "undo",
      feature: "flow.transaction",
      kinds: ["action", "callWorkflow"],
      values: ["reserve", "compensate", "irreversible"],
      paths: {},
      only_inside: "transaction",
      studio: "ready",
    },
  ],
};
const EMPTY = { kind: "action", uses: "", with: { literal: {} } };
const REMOVE = "target-system.remove-quantity@1.0.0";

interface Held {
  resolve(value: unknown): void;
}
/** Step details on add, which move holds with these fields, on a connected platform. */
function setup(
  fields: Record<string, unknown> = {},
  options: {
    hold?: boolean;
    fails?: boolean;
    connected?: boolean;
    canRead?: boolean;
  } = {},
) {
  const workflow = freshWorkflow();
  workflow.metadata.name = "move-between-systems";
  workflow.spec["inputSchema"] = {
    type: "object",
    properties: { moveId: { type: "string" } },
  };
  workflow.spec.steps = [
    {
      id: "move",
      kind: "transaction",
      steps: [
        {
          id: "add",
          kind: "action",
          uses: "target-system.add-quantity@1.0.0",
          with: { literal: {} },
          ...fields,
        },
      ],
      output: { literal: null },
    } as unknown as Step,
  ];
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow), "json");
  model.clearHistory();
  const held: Held[] = [];
  const requests: string[] = [];
  const toasts: string[] = [];
  const host = {
    model,
    perform: (edit: () => void) => edit(),
    notify: (text: string) => void toasts.push(text),
    undo: () => model.undo(),
    refreshView: () => undefined,
    catalogContracts: new Map(
      definitions
        .filter(
          ({ metadata }) => metadata.name === "target-system.add-quantity",
        )
        .map((document) => [
          "target-system.add-quantity@1.0.0",
          document as never,
        ]),
    ),
    decisionContracts: new Map(),
    actionVersions: [
      { name: "source-system.confirm-hold", version: "1.0.0" },
      { name: "target-system.add-quantity", version: "1.0.0" },
    ],
    label: (kind: string) => kind,
    editingLocked: false,
    profile:
      options.connected === false
        ? null
        : ({ name: "dev", baseUrl: "x", projectId: "p" } as never),
    stepDataScope: "ann",
    api: {
      project: "/p",
      request: (path: string) => {
        requests.push(path);
        if (path.endsWith("/catalog") && options.fails)
          return Promise.reject(new Error("503"));
        if (path.endsWith("/catalog"))
          return options.hold
            ? new Promise((resolve) => held.push({ resolve }))
            : Promise.resolve(CATALOG);
        return Promise.resolve(MANIFEST);
      },
    } as never,
    diagnostics: null,
    diagnosticsDefinition: null,
    cacheDecisionContract: () => undefined,
    can: () => options.canRead !== false,
  } as unknown as StepDetailsHost;
  let now = 0;
  const controller = new StepDetailsController(host, () => (now += 60_000));
  const session = new FormSession(host, controller, openRequest("add"), {
    confirm: async () => true,
    announce: () => undefined,
  });
  const placed = (id: string): ParamSpec => {
    const found = session.placedSpecs().find(({ spec }) => spec.id === id);
    if (!found) throw new Error(`No field ${id}`);
    return found.spec;
  };
  return {
    model,
    host,
    held,
    requests,
    toasts,
    controller,
    session,
    placed,
    ids: () => session.placedSpecs().map(({ spec }) => spec.id),
    write: (id: string, value: ParamValue) =>
      controller.commit(
        "add",
        writeParam(controller.subject("add"), placed(id), value),
        id,
      ),
    read: (id: string) => readParam(controller.subject("add"), placed(id)),
    targets: () => controller.ndvContext("add").catalog.undoTargets(),
    add: () => controller.step("add") as unknown as Record<string, unknown>,
    ready: async () => {
      await controller.loadFeatures();
      await callSources(host).lock.read();
    },
  };
}

describe("what may undo a step", () => {
  it("is an action that is safe to retry, in the platform's words", () => {
    const action = (sideEffect?: string) =>
      undoPickerReason({ kind: "action", sideEffect });
    expect(action("idempotent")).toBeNull();
    expect(action("idempotency_key")).toBeNull();
    expect(action("non_idempotent")).toBe(RETRY);
    // Reading is safe to retry, but it undoes nothing.
    expect(action("read_only")).toBe(READS);
    // What Studio can't read, the platform decides.
    expect(action(undefined)).toBeNull();
  });

  it("is a workflow with a time limit", () => {
    expect(
      undoPickerReason({ kind: "callWorkflow", timeoutSeconds: 300 }),
    ).toBeNull();
    expect(undoPickerReason({ kind: "callWorkflow" })).toBe(LIMIT);
  });

  it("is listed as Actions then Workflows, each with its fact and why it can't be chosen", async () => {
    const { targets } = setup();
    const rows = await targets();
    expect(
      rows
        .filter(
          ({ uses }) => !/^(reports|notify|entitlements|intake)\./.test(uses),
        )
        .map(({ uses, kind, group, aside, disabled }) => [
          uses,
          kind,
          group,
          aside,
          disabled ?? "",
        ]),
    ).toEqual([
      ["orders.submit-order@1.0.0", "action", "Actions", "Changes data", RETRY],
      [
        "source-system.confirm-hold@1.0.0",
        "action",
        "Actions",
        "Safe to retry",
        "",
      ],
      [
        "source-system.hold-quantity@1.0.0",
        "action",
        "Actions",
        "Safe to retry",
        "",
      ],
      [
        "source-system.release-hold@1.0.0",
        "action",
        "Actions",
        "Safe to retry",
        "",
      ],
      [
        "target-system.add-quantity@1.0.0",
        "action",
        "Actions",
        "Changes data",
        RETRY,
      ],
      [REMOVE, "action", "Actions", "Safe to retry", ""],
      [
        "reverse-for-others@1.0.0",
        "callWorkflow",
        "Workflows",
        "No time limit",
        notCallable("move-between-systems"),
      ],
      [
        "reverse-target-addition@1.0.0",
        "callWorkflow",
        "Workflows",
        "Up to 5 minutes",
        "",
      ],
      [
        "reverse-without-limit@1.0.0",
        "callWorkflow",
        "Workflows",
        "No time limit",
        LIMIT,
      ],
    ]);
    expect(rows.find(({ uses }) => uses === REMOVE)?.title).toBe(
      "target-system.remove-quantity 1.0.0",
    );
    const lookup = rows.find(({ uses }) => uses.startsWith("reports.lookup"));
    expect([lookup?.aside, lookup?.disabled]).toEqual(["Reads data", READS]);
  });

  it("reads the project's catalog once, for every picker", async () => {
    const { targets, requests } = setup({
      confirm: EMPTY,
      cancel: EMPTY,
    });
    await targets();
    await targets();
    expect(requests.filter((path) => path.endsWith("/catalog"))).toEqual([
      "/p/catalog",
    ]);
  });

  it("lists the actions by name, with what Studio knows of each, while the catalog can't be read", async () => {
    const { targets } = setup({}, { fails: true });
    expect(
      (await targets()).map(({ uses, aside, disabled }) => [
        uses,
        aside ?? "",
        disabled ?? "",
      ]),
    ).toEqual([
      ["source-system.confirm-hold@1.0.0", "", ""],
      ["target-system.add-quantity@1.0.0", "Changes data", RETRY],
    ]);
  });

  it("offers nothing without a platform, and says what to do", async () => {
    const offline = setup({ compensate: EMPTY }, { connected: false });
    await offline.controller.loadFeatures();
    expect(await offline.targets()).toEqual([]);
    expect(offline.placed("compensate").listNote).toBe(
      "Connect to a platform to choose published actions and workflows.",
    );
    const refused = setup({ compensate: EMPTY }, { canRead: false });
    await refused.controller.loadFeatures();
    expect(await refused.targets()).toEqual([]);
    expect(refused.placed("compensate").listNote).toBe(
      "You can't read this project's published actions and workflows here.",
    );
  });

  it("drops an answer that arrives after the step, its workflow or the account changed", async () => {
    const changes: ((made: ReturnType<typeof setup>) => void)[] = [
      ({ host }) => {
        (host as { profile: unknown }).profile = {
          name: "prod",
          baseUrl: "y",
          projectId: "q",
        };
      },
      ({ host }) => {
        (host as { stepDataScope: string }).stepDataScope = "bo";
      },
      ({ host, model }) => {
        const other = new StructuredCanvasAdapter();
        other.setSource(model.source, "json");
        (host as { model: unknown }).model = other;
      },
      ({ model }) => model.replace(structuredClone(model.definition)),
      ({ model }) => model.renameStep("add", "extra"),
    ];
    for (const [index, change] of changes.entries()) {
      const made = setup({}, { hold: true });
      const answer = made.targets();
      await Promise.resolve();
      change(made);
      made.held.forEach(({ resolve }) => resolve(CATALOG));
      expect(await answer, String(index)).toEqual([]);
    }
    const kept = setup({}, { hold: true });
    const answer = kept.targets();
    await Promise.resolve();
    kept.held.forEach(({ resolve }) => resolve(CATALOG));
    expect((await answer).length).toBeGreaterThan(0);
  });
});

describe("choosing what undoes a step", () => {
  it("writes an action with an empty input, as one undo step, and shows its input rows", async () => {
    const { ready, write, add, model, ids, placed } = setup({
      compensate: EMPTY,
    });
    await ready();
    const before = model.source;
    write("compensate", fixed(REMOVE));
    expect(add()["compensate"]).toEqual({ ...EMPTY, uses: REMOVE });
    expect(Object.keys(add()["compensate"] as object)).toEqual([
      "kind",
      "uses",
      "with",
    ]);
    const rows = ids().filter((id) => id.startsWith("compensate.input."));
    expect(rows).toEqual(
      Object.keys(
        (
          definitions.find(
            ({ metadata }) => metadata.name === "target-system.remove-quantity",
          )!.spec["inputSchema"] as { properties: object }
        ).properties,
      ).map((name) => `compensate.input.${name}`),
    );
    expect(placed(rows[0]).path.slice(0, 2)).toEqual(["compensate", "with"]);
    model.undo();
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });

  it("writes a workflow as a call", async () => {
    const { ready, write, add, ids } = setup({ compensate: EMPTY });
    await ready();
    write("compensate", fixed("reverse-target-addition@1.0.0"));
    expect(add()["compensate"]).toEqual({
      kind: "callWorkflow",
      uses: "reverse-target-addition@1.0.0",
      with: { literal: {} },
    });
    expect(ids()).toContain("compensate.input.reference");
  });

  it("writes an action in place of a workflow", async () => {
    const { ready, write, add } = setup({
      compensate: {
        ...EMPTY,
        kind: "callWorkflow",
        uses: "reverse-target-addition@1.0.0",
      },
    });
    await ready();
    write("compensate", fixed(REMOVE));
    expect(add()["compensate"]).toEqual({ ...EMPTY, uses: REMOVE });
  });

  it("resets the input for another target and keeps it for the same one", async () => {
    const configured = {
      kind: "action",
      uses: REMOVE,
      with: { object: { reference: { ref: "/input/moveId" } } },
    };
    const same = setup({ compensate: configured });
    await same.ready();
    const before = same.model.source;
    same.write("compensate", fixed(REMOVE));
    expect(same.model.source).toBe(before);
    const other = setup({ compensate: configured });
    await other.ready();
    other.write("compensate", fixed("source-system.release-hold@1.0.0"));
    expect(other.add()["compensate"]).toEqual({
      kind: "action",
      uses: "source-system.release-hold@1.0.0",
      with: { literal: {} },
    });
  });

  it("keeps an undo step's own connection only for another version of its action", async () => {
    const configured = { ...EMPTY, uses: REMOVE, connection: "target" };
    const chosen = async (uses: string) => {
      const made = setup({ compensate: configured });
      await made.ready();
      made.write("compensate", fixed(uses));
      return made.add()["compensate"];
    };
    expect(await chosen("target-system.remove-quantity@1.1.0")).toMatchObject({
      connection: "target",
    });
    expect(await chosen("source-system.release-hold@1.0.0")).not.toHaveProperty(
      "connection",
    );
    expect(await chosen("reverse-target-addition@1.0.0")).not.toHaveProperty(
      "connection",
    );
  });

  it("keeps the kind it has for a name no catalog lists, and asks again when cleared", async () => {
    const { ready, write, add, session } = setup({
      compensate: {
        kind: "callWorkflow",
        uses: "reverse-target-addition@1.0.0",
        with: { object: { reference: { literal: "a" } } },
      },
    });
    await ready();
    write("compensate", fixed("unknown@1.0.0"));
    expect(add()["compensate"]).toEqual({
      kind: "callWorkflow",
      uses: "unknown@1.0.0",
      with: { literal: {} },
    });
    write("compensate", { mode: "absent" });
    expect(add()["compensate"]).toMatchObject({ uses: "" });
    expect(
      session
        .issues()
        .filter((issue) => issue.field?.spec.id === "compensate")
        .map((issue) => issue.message),
    ).toEqual(["Choose what reverses this step."]);
  });

  it("maps an input row like any field, typed by the target's input", async () => {
    const { ready, write, read, add, session, placed } = setup({
      compensate: { ...EMPTY, uses: REMOVE },
    });
    await ready();
    write("compensate.input.reference", mapped({ ref: "/input/moveId" }));
    expect(add()["compensate"]).toEqual({
      ...EMPTY,
      uses: REMOVE,
      with: { object: { reference: { ref: "/input/moveId" } } },
    });
    expect(read("compensate.input.reference")).toEqual(
      mapped({ ref: "/input/moveId" }),
    );
    expect(
      session.expected(placed("compensate.input.reference")),
    ).toMatchObject({ type: "string" });
    expect(session.node(placed("compensate.input.reference"))).toMatchObject({
      kind: "ref",
      pointer: "/input/moveId",
    });
  });

  it("shows both operations' inputs apart, and what Release must do without", async () => {
    const { ready, ids, placed } = setup({
      confirm: { ...EMPTY, uses: "source-system.confirm-hold@1.0.0" },
      cancel: { ...EMPTY, uses: "source-system.release-hold@1.0.0" },
    });
    await ready();
    expect(ids().filter((id) => /^(confirm|cancel|undoHold)/.test(id))).toEqual(
      [
        "confirm",
        "confirm.input",
        "confirm.input.holdId",
        "cancel",
        "cancel.input",
        "cancel.input.reference",
        "cancel.input.holdOperation",
        "undoHold",
      ],
    );
    expect(placed("cancel.input").hint).toBe(
      "Release must work even if the hold didn't go through. Use the step's operation key or your own reference.",
    );
    expect(placed("confirm.input").hint).toBeUndefined();
  });

  it("takes any input while Studio can't read the target's", async () => {
    const { ready, placed, ids } = setup({
      compensate: { ...EMPTY, uses: "unknown@1.0.0" },
    });
    await ready();
    expect(placed("compensate.input")).toMatchObject({
      path: ["compensate", "with"],
      type: "json",
      mapping: "both",
      hint: "Studio can't read unknown's input.",
    });
    expect(ids().filter((id) => id.startsWith("compensate.input."))).toEqual(
      [],
    );
  });
});

describe("what the compiler finds inside an undo step", () => {
  const at = (fields: Record<string, unknown>, path: string) => {
    const workflow = freshWorkflow();
    workflow.spec.steps = [
      {
        id: "move",
        kind: "transaction",
        steps: [{ id: "add", kind: "action", uses: "x@1.0.0", ...fields }],
        output: { literal: null },
      } as unknown as Step,
    ];
    return locateDiagnostic(`/spec/steps/0/steps/0${path}`, workflow);
  };

  it("is at its action or at the input field it names", () => {
    const fields = {
      compensate: {
        ...EMPTY,
        uses: REMOVE,
        with: { object: { reference: { ref: "/input/nope" } } },
      },
    };
    expect(at(fields, "/compensate/uses")).toMatchObject({
      stepId: "add",
      field: ["compensate", "uses"],
      exact: true,
    });
    expect(at(fields, "/compensate/with/object/reference/ref")).toMatchObject({
      field: ["compensate", "with"],
      dataPath: ["reference"],
    });
    expect(at(fields, "/compensate")).toMatchObject({ field: ["compensate"] });
  });

  it("shows at that field in step details", async () => {
    const made = setup({ compensate: { ...EMPTY, uses: REMOVE } });
    await made.ready();
    const host = made.host as unknown as Record<string, unknown>;
    host["diagnosticsDefinition"] = made.model.definition;
    host["diagnostics"] = {
      diagnostics: [
        {
          code: "WV-COMP-TXN_UNDO_SIDE_EFFECT",
          severity: "error",
          message: "x",
          path: "/spec/steps/0/steps/0/compensate/uses",
        },
        {
          code: "WV-EXPR-MISSING_REFERENCE",
          severity: "error",
          message: "y",
          path: "/spec/steps/0/steps/0/compensate/with/object/reference/ref",
        },
      ],
      errorCount: 2,
      validationOk: false,
    };
    const placed = made.session
      .issues()
      .filter((issue) => issue.key.startsWith("compiler:"))
      .map((issue) => [issue.field?.spec.id, issue.message]);
    expect(placed).toEqual([
      ["compensate", RETRY],
      ["compensate.input.reference", expect.any(String)],
    ]);
  });
});

describe("a check of a way whose operations aren't chosen yet", () => {
  const generic = {
    code: "WV-SCHEMA-INVALID_INSTANCE",
    severity: "error",
    path: "/spec/steps/0",
    message: "Value does not satisfy the declared contract.",
  };
  const checked = (steps: unknown[], diagnostics: unknown[] = [generic]) => {
    const result = withEmptyTransactions(
      {
        validationOk: false,
        errorCount: diagnostics.length,
        diagnostics: diagnostics as object[],
      },
      { spec: { steps } },
    );
    return result.diagnostics.map((found) => {
      const { code, path } = found as { code: string; path: string };
      return [path, code, describeDiagnostic(found as never).text];
    });
  };
  const step = (fields: Record<string, unknown>) => ({
    id: "add",
    kind: "action",
    uses: "x@1.0.0",
    with: { literal: {} },
    ...fields,
  });
  const move = (...steps: unknown[]) => ({
    id: "move",
    kind: "transaction",
    steps,
    output: { literal: null },
  });
  const PAIR = "A step that reserves needs both Confirm with and Release with.";
  const REVERSE = "Choose what reverses this step.";

  it("says which step lacks what, where it is to be chosen", () => {
    expect(checked([move(step({ confirm: EMPTY, cancel: EMPTY }))])).toEqual([
      ["/spec/steps/0/steps/0/confirm/uses", "WV-COMP-TXN_RESERVE_PAIR", PAIR],
    ]);
    expect(
      checked([
        move(step({ confirm: { ...EMPTY, uses: REMOVE }, cancel: EMPTY })),
      ]),
    ).toEqual([
      ["/spec/steps/0/steps/0/cancel/uses", "WV-COMP-TXN_RESERVE_PAIR", PAIR],
    ]);
    expect(checked([move(step({ compensate: EMPTY }))])).toEqual([
      ["/spec/steps/0/steps/0/compensate/uses", "WV-STUDIO-UNDO", REVERSE],
    ]);
    const { uses: _, ...unnamed } = EMPTY;
    expect(checked([move(step({ compensate: unnamed }))])).toEqual([
      ["/spec/steps/0/steps/0/compensate/uses", "WV-STUDIO-UNDO", REVERSE],
    ]);
  });

  it("leaves the platform's answer once every operation is chosen", () => {
    expect(
      checked([move(step({ compensate: { ...EMPTY, uses: REMOVE } }))]),
    ).toEqual([[generic.path, generic.code, expect.any(String)]]);
  });

  it("is found in loops and error paths, and keeps answers about other steps", () => {
    const loop = {
      id: "each",
      kind: "forEach",
      items: { literal: [] },
      body: {
        steps: [move(step({ compensate: EMPTY }))],
        output: { literal: null },
      },
    };
    const other = { ...generic, path: "/spec/steps/1" };
    expect(checked([loop, step({})], [generic, other])).toEqual([
      [
        "/spec/steps/0/body/steps/0/steps/0/compensate/uses",
        "WV-STUDIO-UNDO",
        REVERSE,
      ],
      [other.path, other.code, expect.any(String)],
    ]);
  });

  it("says a step outside a transaction has nothing to undo it with", () => {
    expect(checked([step({ compensate: EMPTY })])).toEqual([
      [
        "/spec/steps/0/compensate",
        "WV-COMP-TXN_OUTSIDE",
        "How a step is undone applies only to steps inside a transaction.",
      ],
    ]);
    // A loop isn't a transaction, and neither is what runs after one failed.
    const loop = {
      id: "each",
      kind: "forEach",
      items: { literal: [] },
      body: { steps: [step({ compensate: EMPTY })], output: { literal: null } },
    };
    const places = (steps: unknown[]) =>
      checked(steps).map(([path, code]) => [path, code]);
    expect(places([loop])).toEqual([
      ["/spec/steps/0/body/steps/0/compensate", "WV-COMP-TXN_OUTSIDE"],
    ]);
    expect(
      places([
        {
          ...move(step({})),
          errorPath: {
            steps: [step({ compensate: EMPTY })],
            output: { literal: null },
          },
        },
      ]),
    ).toEqual([
      ["/spec/steps/0/errorPath/steps/0/compensate", "WV-COMP-TXN_OUTSIDE"],
    ]);
  });
});
