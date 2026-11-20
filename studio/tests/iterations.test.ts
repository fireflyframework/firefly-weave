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
// The item picked in each loop around a step, in one draft: which item's
// data step details show, pin and execute (its instance key), and how many
// items the loop has, from the last Execute or from the current data.
import { computed, signal } from "@angular/core";
import { describe, expect, it } from "vitest";
import type { LoopProgress } from "../src/app/editor/state/execution";
import { isInstanceKey } from "../src/app/editor/state/instance-keys";
import {
  Iterations,
  type IterationsHost,
  type LoopPick,
} from "../src/app/editor/state/iterations";
import {
  StepDetailsController,
  type ControllerHost,
} from "../src/app/editor/ndv/step-details-controller";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  StructuredCanvasAdapter,
  createStep,
  freshWorkflow,
  type Step,
} from "../src/app/model";

const step = (id: string): Step => ({ id, kind: "transform" }) as Step;
const loop = (id: string, items: string, body: Step[]): Step => ({
  ...createStep("forEach", id),
  items: { ref: items },
  body: { steps: body, output: { ref: "/item" } },
});
/** orders over input.orders holds check and lines; lines over item.lines holds send. */
function draft() {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    loop("orders", "/input/orders", [
      step("check"),
      loop("lines", "/item/lines", [step("send")]),
    ]),
    step("after"),
  ];
  return workflow;
}
const progress = (
  nodeId: string,
  instanceKey: string,
  count: number,
): LoopProgress => ({
  nodeId,
  instanceKey,
  count,
  completed: count,
  running: 0,
  nextIndex: count,
  status: "completed",
});

interface Asked {
  loopId: string;
  picks: readonly LoopPick[];
  answer(value: unknown): void;
}
function iterations() {
  let workflow = draft();
  let revision = 1;
  const asked: Asked[] = [];
  const host: IterationsHost = {
    definition: () => workflow,
    revision: () => revision,
    items: (loopId, picks) =>
      new Promise((answer) => asked.push({ loopId, picks, answer })),
  };
  return {
    made: new Iterations(host),
    asked,
    /** Edits the draft, as the editor does: a new revision. */
    edit(change: (steps: Step[]) => void) {
      workflow = structuredClone(workflow);
      change(workflow.spec.steps);
      revision++;
    },
  };
}
const orders = (steps: Step[]) => steps[0];
const bodyOf = (target: Step) => (target["body"] as { steps: Step[] }).steps;
const settle = () => new Promise((done) => setTimeout(done, 0));

describe("the picked item", () => {
  it("starts at the first item of every loop around a step, outermost first", () => {
    const { made } = iterations();
    expect(made.picks("send")).toEqual([
      { loopId: "orders", index: 0 },
      { loopId: "lines", index: 0 },
    ]);
    expect(made.picks("check")).toEqual([{ loopId: "orders", index: 0 }]);
    expect(made.picks("after")).toEqual([]);
    // A loop's own step runs once, outside its body.
    expect(made.picks("orders")).toEqual([]);
  });

  it("names the picked run of a step by its instance key", () => {
    const { made } = iterations();
    made.pick("orders", 2);
    expect(made.instanceKey("send")).toBe("send[2][0]");
    expect(isInstanceKey(made.instanceKey("send"))).toBe(true);
    made.pick("lines", 4);
    expect(made.instanceKey("send")).toBe("send[2][4]");
    expect(made.instanceKey("check")).toBe("check[2]");
    expect(made.instanceKey("after")).toBeNull();
    expect(made.instanceKey("orders")).toBeNull();
    expect(made.instanceKey("lines")).toBe("lines[2]");
  });

  it("never names an item past the end of a list the last Execute counted", () => {
    const { made } = iterations();
    made.recordProgress([
      progress("orders", "orders", 3),
      progress("lines", "lines[0]", 5),
      progress("lines", "lines[1]", 2),
    ]);
    made.pick("lines", 4);
    expect(made.instanceKey("send")).toBe("send[0][4]");
    // The second order has two lines: its last one, not a fifth.
    made.pick("orders", 1);
    expect(made.instanceKey("send")).toBe("send[1][1]");
    expect(made.picks("send")).toEqual([
      { loopId: "orders", index: 1 },
      { loopId: "lines", index: 1 },
    ]);
    // Back on the first order, its fifth line is picked again.
    made.pick("orders", 0);
    expect(made.instanceKey("send")).toBe("send[0][4]");
  });

  it("moves to the last item when a new Execute shortens the list", () => {
    const { made } = iterations();
    made.recordProgress([progress("orders", "orders", 5)]);
    made.pick("orders", 4);
    expect(made.instanceKey("check")).toBe("check[4]");
    made.recordProgress([progress("orders", "orders", 2)]);
    expect(made.instanceKey("check")).toBe("check[1]");
    expect(made.counts("lines")).toEqual({ count: 0, known: false });
    made.recordProgress([progress("orders", "orders", 0)]);
    expect(made.instanceKey("check")).toBe("check[0]");
  });

  it("ignores a pick for a loop that isn't there or for something that isn't an item", async () => {
    const { made, asked } = iterations();
    const counted = made.evaluate("orders");
    made.pick("check", 3);
    made.pick("missing", 3);
    made.pick("orders", -1);
    made.pick("orders", 1.5);
    expect(made.instanceKey("send")).toBe("send[0][0]");
    // Nothing was picked, so a count asked before still counts.
    asked[0].answer([1, 2]);
    expect(await counted).toEqual({ count: 2, known: true });
  });

  it("names no run whose key the platform wouldn't accept", () => {
    const { made, edit } = iterations();
    edit((steps) => (bodyOf(orders(steps))[0].id = "check order"));
    expect(made.picks("check order")).toEqual([{ loopId: "orders", index: 0 }]);
    expect(made.instanceKey("check order")).toBeNull();
  });

  it("goes back to the first item when the loop's Items change", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    edit((steps) => (orders(steps)["items"] = { ref: "/input/open" }));
    expect(made.picks("check")).toEqual([{ loopId: "orders", index: 0 }]);
    edit((steps) => (orders(steps)["items"] = { ref: "/input/orders" }));
    expect(made.instanceKey("check")).toBe("check[0]");
  });

  it("goes back to the first item of a loop inside one whose Items change", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    made.pick("lines", 3);
    edit((steps) => (orders(steps)["items"] = { ref: "/input/open" }));
    expect(made.instanceKey("send")).toBe("send[0][0]");
  });

  it("keeps the pick through other edits", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    edit((steps) => steps.push(step("more")));
    expect(made.instanceKey("check")).toBe("check[2]");
  });

  it("forgets a deleted loop's pick, even when a loop of that name comes back", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    const removed = draft().spec.steps[0];
    edit((steps) => steps.splice(0, 1));
    expect(made.picks("after")).toEqual([]);
    edit((steps) => steps.unshift(removed));
    expect(made.instanceKey("check")).toBe("check[0]");
  });

  it("reads none of a loop's picks for a step moved out of it", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    edit((steps) => steps.push(bodyOf(orders(steps)).splice(0, 1)[0]));
    expect(made.picks("check")).toEqual([]);
    expect(made.instanceKey("check")).toBeNull();
    expect(made.instanceKey("send")).toBe("send[2][0]");
  });

  it("keeps a loop's pick when the loop is renamed", () => {
    const { made, edit } = iterations();
    made.pick("orders", 2);
    edit((steps) => (orders(steps).id = "each-order"));
    made.rename("orders", "each-order");
    expect(made.picks("check")).toEqual([{ loopId: "each-order", index: 2 }]);
    expect(made.instanceKey("send")).toBe("send[2][0]");
  });

  it("keeps a loop's count when the loop is renamed", () => {
    const { made, edit } = iterations();
    made.recordProgress([progress("orders", "orders", 20)]);
    edit((steps) => (orders(steps).id = "each-order"));
    made.rename("orders", "each-order");
    expect(made.counts("each-order")).toEqual({ count: 20, known: true });
  });

  it("keeps the pick and the count when the step the loop's Items read is renamed", () => {
    const { made, edit } = iterations();
    edit((steps) => {
      steps.unshift(step("fetch"));
      steps[1]["items"] = { ref: "/steps/fetch/output" };
    });
    made.pick("orders", 2);
    made.recordProgress([progress("orders", "orders", 20)]);
    // The editor's rename rewrites the reference, and Items still read the same list.
    edit((steps) => {
      steps[0].id = "load";
      steps[1]["items"] = { ref: "/steps/load/output" };
    });
    made.rename("fetch", "load");
    expect(made.instanceKey("send")).toBe("send[2][0]");
    expect(made.counts("orders")).toEqual({ count: 20, known: true });
    // Items that changed in another way still start over.
    edit((steps) => (steps[1]["items"] = { ref: "/steps/load/output/open" }));
    made.rename("load", "loader");
    expect(made.instanceKey("send")).toBe("send[0][0]");
  });
});

describe("how many items a loop has", () => {
  it("is what the last Execute counted, for the picked item of the loops around it", () => {
    const { made } = iterations();
    made.recordProgress([
      progress("orders", "orders", 20),
      progress("lines", "lines[0]", 5),
      progress("lines", "lines[2]", 3),
    ]);
    expect(made.counts("orders")).toEqual({ count: 20, known: true });
    expect(made.counts("lines")).toEqual({ count: 5, known: true });
    made.pick("orders", 2);
    expect(made.counts("lines")).toEqual({ count: 3, known: true });
    made.pick("orders", 7);
    expect(made.counts("lines")).toEqual({ count: 0, known: false });
  });

  it("reads a top-level loop's count without its instance key too", () => {
    const { made } = iterations();
    made.recordProgress([progress("orders", "", 4)]);
    expect(made.counts("orders")).toEqual({ count: 4, known: true });
  });

  it("is no longer known once the loop's Items change", () => {
    const { made, edit } = iterations();
    made.recordProgress([progress("orders", "orders", 20)]);
    edit((steps) => (orders(steps)["items"] = { ref: "/input/open" }));
    expect(made.counts("orders")).toEqual({ count: 0, known: false });
  });

  it("is no longer known for a loop inside one whose Items change", () => {
    const { made, edit } = iterations();
    made.recordProgress([progress("lines", "lines[0]", 5)]);
    edit((steps) => (orders(steps)["items"] = { ref: "/input/open" }));
    expect(made.counts("lines")).toEqual({ count: 0, known: false });
  });

  it("comes from the current data when nothing ran yet", async () => {
    const { made, asked } = iterations();
    made.pick("orders", 2);
    const counted = made.evaluate("lines");
    expect(asked.map(({ loopId, picks }) => [loopId, picks])).toEqual([
      ["lines", [{ loopId: "orders", index: 2 }]],
    ]);
    asked[0].answer(["a", "b", "c"]);
    expect(await counted).toEqual({ count: 3, known: true });
    expect(made.counts("lines")).toEqual({ count: 3, known: true });
    made.pick("orders", 1);
    expect(made.counts("lines")).toEqual({ count: 0, known: false });
  });

  it("stays unknown when the data can't say", async () => {
    const { made, asked } = iterations();
    const counted = made.evaluate("orders");
    asked[0].answer(null);
    expect(await counted).toEqual({ count: 0, known: false });
    const again = made.evaluate("orders");
    asked[1].answer({ not: "a list" });
    expect(await again).toEqual({ count: 0, known: false });
  });

  it("drops a count from the data that arrives after an edit or a new pick", async () => {
    const { made, asked, edit } = iterations();
    const counted = made.evaluate("orders");
    edit((steps) => steps.push(step("more")));
    asked[0].answer([1, 2]);
    expect(await counted).toEqual({ count: 0, known: false });
    expect(made.counts("orders")).toEqual({ count: 0, known: false });
    const inner = made.evaluate("lines");
    made.pick("orders", 1);
    asked[1].answer([1, 2, 3]);
    await inner;
    made.pick("orders", 0);
    expect(made.counts("lines")).toEqual({ count: 0, known: false });
  });

  it("prefers what Execute counted to what the data says", async () => {
    const { made, asked } = iterations();
    const counted = made.evaluate("orders");
    asked[0].answer([1, 2]);
    await counted;
    made.recordProgress([progress("orders", "orders", 20)]);
    expect(made.counts("orders")).toEqual({ count: 20, known: true });
  });

  it("reads what the last Execute counted from the editor, inside a computed", () => {
    let workflow = draft();
    let revision = 1;
    const last = signal<LoopProgress[] | null>(null);
    const made = new Iterations({
      definition: () => workflow,
      revision: () => revision,
      items: async () => null,
      progress: () => last(),
    });
    // A computed can't write signals: reading a new Execute must not either.
    const count = computed(() => {
      const { count, known } = made.counts("orders");
      return known ? count : null;
    });
    expect(count()).toBeNull();
    last.set([progress("orders", "orders", 20)]);
    expect(count()).toBe(20);
    last.set([progress("orders", "orders", 7)]);
    expect(count()).toBe(7);
    // Its Items changed after that Execute: the count no longer holds.
    workflow = structuredClone(workflow);
    workflow.spec.steps[0]["items"] = { ref: "/input/other" };
    revision++;
    expect(made.counts("orders")).toEqual({ count: 0, known: false });
    // A new Execute counts again.
    last.set([progress("orders", "orders", 3)]);
    expect(count()).toBe(3);
  });

  it("can be read from a computed, which follows picks and counts", async () => {
    const { made, asked } = iterations();
    const label = computed(() => {
      const { count, known } = made.counts("orders");
      return `${made.instanceKey("check")} ${known ? count : "?"}`;
    });
    expect(label()).toBe("check[0] ?");
    made.pick("orders", 1);
    expect(label()).toBe("check[1] ?");
    const counted = made.evaluate("orders");
    asked[0].answer([1, 2, 3]);
    await counted;
    await settle();
    expect(label()).toBe("check[1] 3");
  });
});

describe("step details inside a loop", () => {
  const SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: notify, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {orders: {type: array}}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {ref: /input/orders}
      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: send, kind: transform, value: {ref: /item}}
        output: {ref: /item}
    - {id: after, kind: transform, value: {literal: 1}}
  output: {literal: null}
`;
  function opened(viewingRuns = false) {
    const model = new StructuredCanvasAdapter();
    model.setSource(SOURCE, "yaml");
    const last = signal<ExecuteResponse | null>(null);
    const host = {
      model,
      perform: (edit: () => void) => edit(),
      notify: () => undefined,
      undo: () => model.undo(),
      undoIfUnchanged: async () => "changed" as const,
      catalogContracts: new Map(),
      decisionContracts: new Map(),
      actionVersions: [],
      label: (kind: string) => kind,
      editingLocked: false,
      viewingRuns,
      profile: null,
      api: { request: async () => ({ features: ["flow.forEach"] }) },
      diagnostics: null,
      diagnosticsDefinition: null,
      cacheDecisionContract: () => undefined,
      can: () => true,
      execution: { results: signal(null), last, running: () => null },
    } as unknown as ControllerHost;
    return { model, last, controller: new StepDetailsController(host) };
  }

  it("gives a step inside a loop the picked item's key, and none outside", () => {
    const { controller } = opened();
    expect(controller.ndvContext("send").instanceKey).toBe("send[0]");
    controller.iterations.pick("orders", 2);
    expect(controller.ndvContext("send").instanceKey).toBe("send[2]");
    expect(controller.ndvContext("after").instanceKey).toBeNull();
    expect(controller.ndvContext("orders").instanceKey).toBeNull();
  });

  it("counts a loop's items from the last Execute", () => {
    const { controller, last } = opened();
    expect(controller.iterations.counts("orders").known).toBe(false);
    last.set({
      loops: [progress("orders", "orders", 20)],
    } as unknown as ExecuteResponse);
    expect(controller.iterations.counts("orders")).toEqual({
      count: 20,
      known: true,
    });
  });

  it("keeps the pick through a rename made in step details", () => {
    const { controller, model } = opened();
    controller.iterations.pick("orders", 3);
    model.renameStep("orders", "each-order");
    controller.renamePending("orders", "each-order");
    expect(controller.ndvContext("send").instanceKey).toBe("send[3]");
  });

  it("forgets the picks when another workflow opens", () => {
    const { controller, model } = opened();
    controller.iterations.pick("orders", 3);
    // The same loop in the next workflow opened starts at its first item.
    model.setSource(SOURCE, "yaml");
    model.clearHistory();
    expect(controller.ndvContext("send").instanceKey).toBe("send[0]");
  });

  it("shows the item the Runs tab's chip picked in run mode, and moves the chip when step details picks another", () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(SOURCE, "yaml");
    const picks = signal<Record<string, number>>({ orders: 3 });
    const moved: [string, number][] = [];
    const canvas = computed(() => ({
      mode: "real",
      status: "failed",
      current: [],
      active: [],
      done: [],
      changed: [],
      added: [],
      failures: {},
      // The run reached four of its five items.
      loops: [{ id: "orders", within: [], count: 5, known: true, items: [] }],
      picks: picks(),
      pick: (loop: string, index: number) => {
        moved.push([loop, index]);
        picks.update((all) => ({ ...all, [loop]: index }));
      },
    }));
    const runData = {
      version: () => 0,
      // The failed item first, as the run lists them.
      iterations: (stepId: string) =>
        stepId === "send"
          ? ["send[3]", "send[0]", "send[1]", "send[2]", "send[4]"]
          : [],
    };
    const host = {
      model,
      label: (kind: string) => kind,
      viewingRuns: true,
      runData,
      runView: () => ({ canvas: () => canvas() }),
      api: { request: async () => ({ features: [] }) },
    } as unknown as ControllerHost;
    const controller = new StepDetailsController(host);
    expect(controller.runKey("send")).toBe("send[3]");
    picks.set({ orders: 2 });
    expect(controller.runKey("send")).toBe("send[2]");
    // Choosing item 5 in step details moves the chip, and step details follows it.
    controller.chooseRunIteration("send", "send[4]");
    expect(moved).toEqual([["orders", 4]]);
    expect(controller.runKey("send")).toBe("send[4]");
    // A step outside the loop keeps its own key.
    expect(controller.runKey("after")).toBe("after");
    // The select offers every item of the loop, the one the run never reached too.
    expect(controller.runItems("send")).toEqual({
      count: 5,
      keys: ["send[0]", "send[1]", "send[2]", "send[3]", "send[4]"],
    });
    expect(controller.runItems("after")).toBeNull();
  });

  it("leaves run mode's iteration alone and never picks for the draft", () => {
    const { controller } = opened(true);
    controller.iteration.set({ send: "send[4]" });
    expect(controller.ndvContext("send").instanceKey).toBeNull();
    expect(controller.iterations.picks("send")).toEqual([
      { loopId: "orders", index: 0 },
    ]);
  });
});
