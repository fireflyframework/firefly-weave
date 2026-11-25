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
// Group in a transaction: what it makes of a run of steps, which later reads
// it would break, when it can't run, and how it changes the workflow. Which
// transaction a place is in.
import { describe, expect, it } from "vitest";
import { KEYMAP } from "../src/app/editor/state/keymap";
import {
  CANVAS_COMMANDS,
  canvasSheet,
} from "../src/app/editor/state/canvas-commands";
import {
  blockedReason,
  selectionOf,
  type StepPlace,
} from "../src/app/editor/state/selection";
import {
  applyGroup,
  enclosingTransaction,
  groupInTransaction,
  nestedReason,
  readsText,
} from "../src/app/editor/transactions/group";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  sequencesOf,
  type ReferenceSite,
  type Step,
  type Workflow,
} from "../src/app/model";

const step = (value: Record<string, unknown>) => value as unknown as Step;
const transform = (id: string) =>
  step({ id, kind: "transform", value: { literal: {} } });
/**
 * move holds hold, add (with an error path) and each (a loop) in a
 * transaction with an error path of its own; notify comes after it.
 */
function nested() {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    step({
      id: "move",
      kind: "transaction",
      steps: [
        transform("hold"),
        step({
          ...transform("add"),
          onError: "errorOutput",
          errorPath: { steps: [transform("flag")], output: { literal: null } },
        }),
        step({
          id: "each",
          kind: "forEach",
          items: { literal: [] },
          body: { steps: [transform("send")], output: { ref: "/item" } },
        }),
      ],
      output: { ref: "/steps/add/output" },
      onError: "errorOutput",
      errorPath: { steps: [transform("tell")], output: { literal: null } },
    }),
    transform("notify"),
  ];
  return workflow;
}

describe("the transaction a place is in", () => {
  it("is the one whose own steps hold it, at any depth, and never its error path", () => {
    const workflow = nested();
    const inside = (owner: string) =>
      enclosingTransaction(workflow, owner)?.id ?? null;
    expect(
      [...sequencesOf(workflow).keys()].map((owner) => [owner, inside(owner)]),
    ).toEqual([
      ["root", null],
      ["move/steps", "move"],
      ["add/errorPath", "move"],
      ["each/body", "move"],
      ["move/errorPath", null],
    ]);
    expect(inside("gone/steps")).toBeNull();
  });
});

const reads = (id: string, ...refs: string[]) =>
  step({
    id,
    kind: "transform",
    value: {
      object: Object.fromEntries(refs.map((ref, i) => [`f${i}`, { ref }])),
    },
  });
/** The steps in the main sequence, whose result reads what `output` says. */
function holding(steps: Step[], output: unknown = { literal: null }): Workflow {
  const workflow = freshWorkflow();
  workflow.spec.steps = steps;
  workflow.spec["output"] = output;
  return workflow;
}
const run = (from: number, to: number, owner = "root") => ({ owner, from, to });
const group = (
  workflow: Workflow,
  from: number,
  to: number,
  owner = "root",
) => {
  const grouped = groupInTransaction(
    workflow,
    run(from, to, owner),
    "transaction-1",
  );
  if ("refused" in grouped) throw new Error(grouped.refused);
  return grouped;
};
const listed = (sites: readonly ReferenceSite[]) =>
  sites.map((site) => `${site.holder || "result"} ${site.ref}`);

describe("grouping steps in a transaction", () => {
  const workflow = () =>
    holding([
      transform("hold"),
      transform("add"),
      reads("notify", "/steps/hold/output"),
    ]);

  it("puts the run in one block whose result is the last step's, and the rest after it", () => {
    const before = workflow();
    const { definition } = group(before, 0, 1);
    expect(definition.spec.steps).toEqual([
      {
        id: "transaction-1",
        kind: "transaction",
        steps: [transform("hold"), transform("add")],
        output: { ref: "/steps/add/output" },
      },
      reads("notify", "/steps/hold/output"),
    ]);
    // Nothing but the steps it was given: no step says how it is undone.
    expect(before).toEqual(workflow());
  });

  it("groups steps of a path where they are", () => {
    const definition = group(
      holding([
        step({
          id: "each",
          kind: "forEach",
          items: { literal: [] },
          body: {
            steps: [transform("hold"), transform("add"), transform("tell")],
            output: { ref: "/item" },
          },
        }),
      ]),
      1,
      2,
      "each/body",
    ).definition;
    expect(
      sequencesOf(definition)
        .get("each/body")!
        .map((item) => item.id),
    ).toEqual(["hold", "transaction-1"]);
    expect(
      sequencesOf(definition)
        .get("transaction-1/steps")!
        .map((item) => item.id),
    ).toEqual(["add", "tell"]);
  });

  it("gives no result where the last step has none", () => {
    const outputOf = (last: Step) =>
      group(holding([transform("hold"), last]), 0, 1).definition.spec.steps[0][
        "output"
      ];
    expect(
      outputOf(
        step({ id: "stop", kind: "fail", code: "REFUSED", message: "" }),
      ),
    ).toEqual({ literal: null });
    expect(
      outputOf(step({ id: "notify", kind: "callWorkflow", mode: "detach" })),
    ).toEqual({ literal: null });
    expect(
      outputOf(step({ id: "notify", kind: "callWorkflow", mode: "wait" })),
    ).toEqual({ ref: "/steps/notify/output" });
  });

  it("refuses to nest a transaction, at any depth", () => {
    const refused = (
      definition: Workflow,
      from: number,
      to: number,
      owner?: string,
    ) => groupInTransaction(definition, run(from, to, owner), "transaction-1");
    const reason = {
      refused: "A transaction can't contain another transaction.",
    };
    const inside = nested();
    expect(refused(inside, 0, 1, "move/steps")).toEqual(reason);
    expect(refused(inside, 0, 0, "each/body")).toEqual(reason);
    expect(refused(inside, 0, 1)).toEqual(reason);
    const deep = holding([
      step({
        ...transform("hold"),
        onError: "errorOutput",
        errorPath: { steps: nested().spec.steps, output: { literal: null } },
      }),
      transform("add"),
    ]);
    expect(refused(deep, 0, 1)).toEqual(reason);
    expect(nestedReason(deep, run(1, 1))).toBeNull();
    // A transaction's error path isn't inside it.
    expect("refused" in refused(nested(), 0, 0, "move/errorPath")).toBe(false);
  });
});

describe("the later reads grouping would break", () => {
  it("are those of steps after the run, never the block's own result", () => {
    expect(
      listed(
        group(
          holding([
            transform("hold"),
            reads("add", "/steps/hold/output/holdId"),
            transform("hold-2"),
            reads(
              "notify",
              "/steps/hold/output/holdId",
              "/input/moveId",
              "/steps/hold-2/output",
            ),
          ]),
          0,
          1,
        ).outsideReads,
      ),
    ).toEqual(["notify /steps/hold/output/holdId"]);
  });

  it("include the last step's output and a grouped step's error", () => {
    expect(
      listed(
        group(
          holding([
            transform("hold"),
            transform("add"),
            reads("notify", "/steps/add/output", "/steps/hold/error/code"),
          ]),
          0,
          1,
        ).outsideReads,
      ),
    ).toEqual(["notify /steps/add/output", "notify /steps/hold/error/code"]);
  });

  it("are found in paths, undo steps, results and the workflow's own", () => {
    const workflow = holding(
      [
        step({
          id: "each",
          kind: "forEach",
          items: { literal: [] },
          body: {
            steps: [
              transform("hold"),
              transform("add"),
              step({
                id: "submit",
                kind: "action",
                uses: "orders.submit-order@1.0.0",
                with: { literal: { ref: "/steps/hold/output" } },
                compensate: {
                  kind: "action",
                  uses: "target-system.remove-quantity@1.0.0",
                  with: {
                    object: { holdId: { ref: "/steps/hold/output/holdId" } },
                  },
                },
                onError: "errorOutput",
                errorPath: {
                  steps: [reads("flag", "/steps/add/output/id")],
                  output: { literal: null },
                },
              }),
            ],
            output: { ref: "/steps/hold/output" },
          },
        }),
      ],
      { ref: "/steps/hold/output" },
    );
    expect(listed(group(workflow, 0, 1, "each/body").outsideReads)).toEqual([
      "each /steps/hold/output",
      "submit /steps/hold/output/holdId",
      "flag /steps/add/output/id",
      "result /steps/hold/output",
    ]);
  });

  it("say where each one is in the workflow before grouping", () => {
    const [site] = group(
      holding([transform("hold"), reads("notify", "/steps/hold/output")]),
      0,
      0,
    ).outsideReads;
    expect(site).toEqual({
      path: ["spec", "steps", 1, "value", "object", "f0", "ref"],
      ref: "/steps/hold/output",
      holder: "notify",
    });
  });

  it("are counted by the steps that read", () => {
    const site = (holder: string): ReferenceSite => ({
      path: [],
      ref: "/steps/hold/output",
      holder,
    });
    expect(readsText([site("notify"), site("record"), site("notify")])).toBe(
      "2 later steps read data from these steps. After grouping they can read only the transaction's result.",
    );
    expect(readsText([site("notify")])).toBe(
      "1 later step reads data from these steps. After grouping it can read only the transaction's result.",
    );
    expect(readsText([site("notify"), site("")])).toBe(
      "1 later step and the workflow's result read data from these steps. After grouping they can read only the transaction's result.",
    );
    expect(readsText([site("")])).toBe(
      "The workflow's result reads data from these steps. After grouping it can read only the transaction's result.",
    );
  });
});

describe("the change grouping makes", () => {
  const SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    # The reservation.
    - id: hold
      kind: transform
      value: {literal: {}}
    - id: add
      kind: transform
      value: {literal: {}}
    - {id: notify, kind: transform, value: {ref: /steps/hold/output}}
  output: {literal: null}
`;
  const opened = () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(SOURCE, "yaml");
    model.clearHistory();
    return model;
  };

  it("is one undo step that Undo takes back byte for byte", () => {
    const model = opened();
    const grouped = group(model.definition, 0, 1);
    applyGroup(model, run(0, 1), grouped);
    expect(model.definition).toEqual(grouped.definition);
    expect(model.selected).toBe("transaction-1");
    expect(model.error).toBe("");
    model.undo();
    expect(model.source).toBe(SOURCE);
    expect(model.canUndo).toBe(false);
  });

  it("is written to the source as the workflow reads", () => {
    const model = opened();
    applyGroup(model, run(0, 1), group(model.definition, 0, 1));
    const again = new StructuredCanvasAdapter();
    again.setSource(model.source, "yaml");
    expect(again.error).toBe("");
    expect(again.definition).toEqual(model.definition);
  });
});

describe("the command", () => {
  it("says why it can't run on steps in two paths, in its own words", () => {
    const places = new Map<string, StepPlace>(
      ["hold", "add", "submit"].map((id, index) => [
        id,
        { id, owner: "root", index, parent: null, order: index },
      ]),
    );
    expect(
      blockedReason("group", selectionOf(["hold", "submit"]), places),
    ).toBe(
      "Group in a transaction works on steps next to each other in one path. Select a single run of steps.",
    );
    expect(
      blockedReason("group", selectionOf(["hold", "add"]), places),
    ).toBeNull();
  });

  it("has no key of its own, so no shortcut sheet lists it", () => {
    const row = KEYMAP.find((item) =>
      Object.values(item.commands).includes("groupInTransaction"),
    );
    expect(row?.keys).toEqual([]);
    expect(CANVAS_COMMANDS.has("groupInTransaction")).toBe(true);
    expect(
      canvasSheet("other").flatMap((section) =>
        section.entries.map((entry) => entry.label),
      ),
    ).not.toContain("Group the selection in a transaction");
  });
});
