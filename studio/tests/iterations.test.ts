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
import { computed } from "@angular/core";
import { describe, expect, it } from "vitest";
import type { LoopProgress } from "../src/app/editor/state/execution";
import { isInstanceKey } from "../src/app/editor/state/instance-keys";
import {
  Iterations,
  type IterationsHost,
  type LoopPick,
} from "../src/app/editor/state/iterations";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

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
